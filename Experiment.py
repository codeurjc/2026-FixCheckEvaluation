"""
Experiment.py — Orchestrator for LLM-based bug fix generation and evaluation.

Given a Defects4J project and bug id, this script:
  1. Starts an ephemeral Docker container from the ``defects4j:3.0.1`` image,
     mounting the host working directory as a shared volume.
  2. Checks out the buggy version of the project.
  3. Compiles it and runs the test suite to confirm the bug is present.
  4. Extracts bug metadata with ``defects4j info`` (recorded in
     ``result.json``; no longer part of the prompt).
  5. Locates and reads the buggy source file(s).
  6. Delegates *fix generation* to ``FixGenerator`` (dataset/Docker-agnostic).
  7. Applies the generated diff and re-runs the test suite to validate the fix.
  8. Persists all artifacts under ``results/<project>/<bug_id>/`` (or
     ``results/<project>/<bug_id>/<iteration>/`` when ``--iteration`` is given).

The container is always stopped and removed at the end of the run.

Usage:
    python Experiment.py --project Lang --bug-id 1 --workdir ./workspace
"""

import argparse
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import docker
from dotenv import load_dotenv

from docker_utils import exec_in_container
from FixGenerator import FixGenerator, normalize_diff

# Load API keys / OLLAMA_BASE_URL from a project-root .env, if present.
# override=False so an explicitly-set environment variable still wins.
load_dotenv()

DEFECTS4J_IMAGE = "defects4j:3.0.1"
DEFAULT_MODEL = "ollama/gpt-oss:20b"

DIFF_FILENAME = "_llm_fix.diff"  # temp diff written into the mounted workdir


def parse_failing_tests(output: str) -> int:
    """Parse the number of failing tests from ``defects4j test`` output.

    Returns the count, or -1 if the expected line is not present.
    """
    match = re.search(r"Failing tests:\s*(\d+)", output)
    return int(match.group(1)) if match else -1


def parse_failing_test_names(output: str) -> list:
    """Parse the individual failing test names from ``defects4j test`` output.

    ``defects4j test`` lists each failing test on its own ``  - <name>`` line,
    where ``<name>`` is ``Class::method``. Returns them as a list.
    """
    return re.findall(r"^\s*-\s*(\S+)", output, re.MULTILINE)


def evaluate_fix(trigger_tests, failing_before_names, failing_after_names, applied):
    """Decide whether a Defects4J bug is fixed by a candidate patch.

    A bug is fixed when every trigger test passes again and the patch
    introduces no new failures (no test that passed before now fails). Judging
    against the trigger tests rather than a zero total is robust to
    environment-flaky tests (e.g. ``SystemUtils``' user-home test under
    ``HOME=/tmp``) that fail regardless of the patch.

    Args:
        trigger_tests: The bug's trigger tests (``Class::method`` strings).
        failing_before_names: Tests failing before the patch.
        failing_after_names: Tests failing after the patch.
        applied: Whether the patch was applied at all.

    Returns:
        ``(triggers_fixed, new_failures, fixed)``.
    """
    trigger_set = set(trigger_tests)
    before = set(failing_before_names)
    after = set(failing_after_names)
    triggers_fixed = applied and bool(trigger_set) and not (trigger_set & after)
    new_failures = sorted(after - before)
    fixed = triggers_fixed and not new_failures
    return triggers_fixed, new_failures, fixed


def start_container(client, mount_dir):
    """Start an ephemeral Defects4J container with the workdir mounted.

    The host directory is bound to the *same absolute path* inside the container
    so that ``-w <workdir>`` is valid on both sides and the checked-out sources
    are visible on the host through the shared volume.

    The container runs as the host user (same uid:gid) so that files created on
    the shared volume are owned by the host user. This avoids Git's "dubious
    ownership" error and lets us read/write/clean the checkout from the host.
    """
    print(f"[experiment] Starting container from {DEFECTS4J_IMAGE} ...")
    container = client.containers.run(
        DEFECTS4J_IMAGE,
        detach=True,
        tty=True,
        user=f"{os.getuid()}:{os.getgid()}",
        # HOME must be writable by the host uid for `git config --global`.
        environment={"HOME": "/tmp"},
        volumes={mount_dir: {"bind": mount_dir, "mode": "rw"}},
    )
    print(f"[experiment] Container started: {container.short_id}")

    # Trust the mounted directories for Git, as a safety net regardless of
    # ownership, so defects4j's git operations and our later `git apply` work.
    exec_in_container(container, "git config --global --add safe.directory '*'")

    return container


def run_step(container, command, workdir, description):
    """Run a Defects4J command in the container and echo its result."""
    print(f"[experiment] {description}")
    result = exec_in_container(container, command, workdir=workdir)
    status = "ok" if result.ok else f"FAILED (exit {result.exit_code})"
    print(f"[experiment]   -> {status}")
    return result


# ----------------------------------------------------------------- sources

def export_property(container, workdir, prop):
    """Return the value of a Defects4J export property as a string.

    ``defects4j export`` interleaves ant progress messages with the value on the
    combined stream, so we write the value to a file with ``-o`` and read it back
    from the shared volume to get a clean result.
    """
    out_file = os.path.join(workdir, f".export_{prop}")
    result = exec_in_container(
        container,
        f"defects4j export -p {prop} -o {out_file} -w {workdir}",
        workdir=None,
    )
    if not result.ok:
        print(f"[experiment] WARNING: export of '{prop}' failed:\n{result.output}")
        return ""
    try:
        with open(out_file, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def locate_source_files(container, workdir):
    """Map modified classes to their source file paths on the host.

    Returns a list of (relative_path, absolute_path) tuples.
    """
    src_dir = export_property(container, workdir, "dir.src.classes")
    modified = export_property(container, workdir, "classes.modified")
    classes = [c.strip() for c in modified.splitlines() if c.strip()]

    files = []
    for fq_class in classes:
        rel_path = os.path.join(src_dir, fq_class.replace(".", "/") + ".java")
        abs_path = os.path.join(workdir, rel_path)
        files.append((rel_path, abs_path))
    return files


def read_sources(files):
    """Read source files, returning a list of (relative_path, content)."""
    sources = []
    for rel_path, abs_path in files:
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                sources.append((rel_path, f.read()))
        except FileNotFoundError:
            print(f"[experiment] WARNING: source file not found: {abs_path}")
    return sources


# ------------------------------------------------------------ regression test

def get_trigger_tests(container, workdir):
    """Return the trigger (regression) tests for the checked-out bug.

    Returns a list of ``"TestClass::testMethod"`` strings, as reported by
    ``defects4j export -p tests.trigger``.
    """
    trigger = export_property(container, workdir, "tests.trigger")
    return [t.strip() for t in trigger.splitlines() if t.strip()]


def locate_test_files(container, workdir, test_classes):
    """Map fully-qualified test class names to their source file paths.

    Mirrors ``locate_source_files`` but for test sources: takes the classes
    explicitly (already extracted from the trigger tests) instead of
    ``classes.modified``, and reads the test source directory
    (``dir.src.tests``) instead of ``dir.src.classes``.

    Returns a list of (relative_path, absolute_path) tuples.
    """
    src_dir = export_property(container, workdir, "dir.src.tests")

    files = []
    for fq_class in test_classes:
        rel_path = os.path.join(src_dir, fq_class.replace(".", "/") + ".java")
        abs_path = os.path.join(workdir, rel_path)
        files.append((rel_path, abs_path))
    return files


def run_trigger_tests(container, workdir, trigger_tests):
    """Run each trigger test in isolation and return the combined log."""
    logs = []
    for trigger_test in trigger_tests:
        cmd = f"defects4j test -t {trigger_test} -w {workdir}"
        result = exec_in_container(container, cmd, workdir=None)
        logs.append(f"$ {cmd}\n{result.output}")
    return "\n\n".join(logs)


def _prev_nonblank(lines, idx):
    """Return the closest non-blank line before ``idx`` (or None)."""
    j = idx - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    return lines[j] if j >= 0 else None


def extract_java_method(content, method_name):
    """Best-effort extraction of a single Java method from source text.

    Returns the method text — including any immediately preceding annotations
    (``@Test`` …) and Javadoc — or None if it can't be found. Uses brace
    matching, so it can be fooled by braces inside string/char literals, but is
    adequate for typical JUnit test methods.
    """
    lines = content.split("\n")
    name_re = re.compile(rf"\b{re.escape(method_name)}\s*\(")
    for idx, line in enumerate(lines):
        if not name_re.search(line):
            continue
        if line.lstrip().startswith(("//", "*", "/*")):
            continue
        # Treat as a declaration only if it carries a modifier/return type, or
        # the preceding non-blank line is an annotation (e.g. ``@Test``).
        looks_decl = bool(re.search(r"\b(void|public|protected|private|static)\b", line))
        prev = _prev_nonblank(lines, idx)
        if not looks_decl and not (prev and prev.lstrip().startswith("@")):
            continue

        # Extend upward over annotation lines and an adjacent Javadoc block.
        start = idx
        j = idx - 1
        while j >= 0 and lines[j].lstrip().startswith("@"):
            start = j
            j -= 1
        if j >= 0 and lines[j].strip().endswith("*/"):
            k = j
            while k >= 0 and "/**" not in lines[k]:
                k -= 1
            if k >= 0:
                start = k

        # Find the method body by brace matching from the declaration onward.
        depth = 0
        started = False
        for end in range(idx, len(lines)):
            for ch in lines[end]:
                if ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
            if started and depth == 0:
                return "\n".join(lines[start:end + 1])
        return None
    return None


def extract_trigger_test_code(trigger_tests, test_file_sources):
    """Reduce full test files to only the failing trigger method(s).

    Passing the whole test file tends to distract the model with unrelated
    tests and helpers, so we keep only the methods named by the trigger tests.
    Falls back to the full file (with a warning) when a method can't be
    extracted, so the model is never left without any test context.

    Args:
        trigger_tests: List of ``"FQCN::method"`` strings.
        test_file_sources: List of ``(relative_path, content)`` for the test
            file(s), as read from disk.

    Returns:
        List of ``(relative_path, content)`` where content holds only the
        failing method(s).
    """
    methods_by_class = {}
    for trigger in trigger_tests:
        cls, _, method = trigger.partition("::")
        methods_by_class.setdefault(cls, []).append(method)

    reduced = []
    for rel_path, content in test_file_sources:
        methods = next(
            (m for cls, m in methods_by_class.items()
             if rel_path.endswith(cls.replace(".", "/") + ".java")),
            [],
        )
        snippets = []
        for method in methods:
            code = extract_java_method(content, method)
            if code:
                snippets.append(code)
            else:
                print(f"[experiment] WARNING: could not extract test method "
                      f"'{method}' from {rel_path}; falling back to full file.")
        if snippets and len(snippets) == len(methods):
            header = f"// Failing test method(s) from {rel_path}\n"
            reduced.append((rel_path, header + "\n\n".join(snippets)))
        else:
            reduced.append((rel_path, content))
    return reduced


# ------------------------------------------------------------------- issue

def extract_bug_report_url(info_output):
    """Extract the bug report URL from ``defects4j info -b`` output."""
    match = re.search(r"Bug report url:\s*\n(\S+)", info_output)
    return match.group(1) if match else ""


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text extractor for the generic issue-fetch fallback."""

    def __init__(self):
        super().__init__()
        self._skip = False
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.chunks.append(data.strip())


def _http_get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "FixCheckEvaluation"})
    with urllib.request.urlopen(request, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_jira_issue(url):
    """Fetch title/description/comments for an Apache Jira issue URL."""
    key = url.rstrip("/").split("/")[-1]
    api_url = (
        f"https://issues.apache.org/jira/rest/api/2/issue/{key}"
        "?fields=summary,description,comment"
    )
    data = _http_get_json(api_url)
    fields = data.get("fields", {})
    parts = [
        f"Summary: {fields.get('summary', '')}",
        f"Description:\n{fields.get('description', '') or ''}",
    ]
    comments = (fields.get("comment") or {}).get("comments", [])
    for comment in comments:
        parts.append(f"Comment:\n{comment.get('body', '')}")
    return "\n\n".join(parts)


def _fetch_github_issue(url):
    """Fetch title/body/comments for a GitHub issue URL."""
    parsed = urllib.parse.urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    owner, repo, _, number = segments[0], segments[1], segments[2], segments[3]

    issue = _http_get_json(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}")
    parts = [
        f"Title: {issue.get('title', '')}",
        f"Body:\n{issue.get('body', '') or ''}",
    ]
    comments = _http_get_json(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments"
    )
    for comment in comments:
        parts.append(f"Comment:\n{comment.get('body', '')}")
    return "\n\n".join(parts)


def _fetch_generic_issue(url):
    """Fetch and strip HTML tags from any other bug-tracker URL."""
    request = urllib.request.Request(url, headers={"User-Agent": "FixCheckEvaluation"})
    with urllib.request.urlopen(request, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return "\n".join(parser.chunks)


def fetch_issue_text(bug_report_url):
    """Fetch the full issue text for a Defects4J bug report URL.

    Dispatches to the Jira or GitHub REST API when the tracker is
    recognized, otherwise falls back to a generic HTML-to-text fetch. Any
    failure is logged as a warning and results in an empty string, so a
    fetch problem never aborts the run.
    """
    host = urllib.parse.urlparse(bug_report_url).netloc
    try:
        if host == "issues.apache.org":
            return _fetch_jira_issue(bug_report_url)
        if host == "github.com":
            return _fetch_github_issue(bug_report_url)
        return _fetch_generic_issue(bug_report_url)
    except Exception as exc:
        print(f"[experiment] WARNING: failed to fetch issue from {bug_report_url}: {exc}")
        return ""


# ------------------------------------------------------------------- apply

def reset_worktree(container, workdir):
    """Restore the checkout to its pristine (buggy) state.

    ``patch --fuzz`` is not atomic: it applies the hunks it can, leaves the
    rest as ``.rej`` and modifies the target file in place. If a later apply
    strategy then runs on top of that half-applied state, it works against a
    corrupted tree (dangling braces, duplicated methods) and produces the kind
    of failure we cannot diagnose. Restoring the git-tracked files to HEAD (the
    buggy version) and removing ``patch``'s ``.orig``/``.rej`` backups before
    each attempt keeps every strategy starting from the same clean state.

    Untracked files (the written diff, the ``.export_*`` helpers) are left
    alone, so this is safe to call between attempts.
    """
    exec_in_container(container, f"git -C {workdir} checkout -- .", workdir=None)
    exec_in_container(
        container,
        rf"find {workdir} \( -name '*.orig' -o -name '*.rej' \) -delete",
        workdir=None,
    )


def apply_diff(container, workdir, diff_text):
    """Write the diff into the mounted workdir and apply it with git.

    Returns (applied: bool, apply_log: str).
    """
    diff_path = os.path.join(workdir, DIFF_FILENAME)
    normalized = normalize_diff(diff_text)
    with open(diff_path, "w", encoding="utf-8") as f:
        f.write(normalized if normalized.endswith("\n") else normalized + "\n")

    logs = []
    # Try a sequence of increasingly lenient strategies. LLM-generated diffs
    # often have slightly wrong hunk line counts or blank context lines, so
    # we use --recount/--ignore-whitespace and finally fall back to `patch`,
    # which tolerates fuzzy context.
    commands = [
        f"git -C {workdir} apply {diff_path}",
        f"git -C {workdir} apply --recount --ignore-whitespace {diff_path}",
        f"git -C {workdir} apply --recount --ignore-whitespace -p0 {diff_path}",
        f"patch -d {workdir} -p1 --fuzz=3 -i {diff_path}",
        f"patch -d {workdir} -p0 --fuzz=3 -i {diff_path}",
    ]
    for cmd in commands:
        # Start every attempt from a pristine checkout so a partial patch left
        # by a previous (non-atomic) strategy can't contaminate this one.
        reset_worktree(container, workdir)
        result = exec_in_container(container, cmd, workdir=None)
        logs.append(f"$ {result.command}\n(exit {result.exit_code})\n{result.output}")
        if result.ok:
            return True, "\n\n".join(logs)
    # All strategies failed; leave the tree clean rather than half-patched.
    reset_worktree(container, workdir)
    return False, "\n\n".join(logs)


def write_text(results_dir, filename, content):
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def main():
    parser = argparse.ArgumentParser(
        description="Generate and evaluate an LLM bug fix on a Defects4J bug."
    )
    parser.add_argument("--project", required=True, help="Defects4J project name (e.g. Lang).")
    parser.add_argument("--bug-id", required=True, help="Numeric bug id (e.g. 1).")
    parser.add_argument(
        "--workdir", required=True,
        help="Host directory where the buggy source is checked out (shared volume).",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"LLM model identifier (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0,
        help="LLM sampling temperature (default: 0.0).",
    )
    parser.add_argument(
        "--include-test-code", action="store_true",
        help="Include the regression (trigger) test source file(s) in the prompt.",
    )
    parser.add_argument(
        "--include-test-log", action="store_true",
        help="Include the regression (trigger) test's failure log in the prompt.",
    )
    parser.add_argument(
        "--include-issue", action="store_true",
        help="Include the original bug-tracker issue report in the prompt.",
    )
    parser.add_argument(
        "--iteration", default=None,
        help="Iteration index; when set, artifacts go to "
             "results/<project>/<bug>/<iteration>/ instead of results/<project>/<bug>/. "
             "Used by run_iterations.py to keep repeated runs of the same bug apart.",
    )
    args = parser.parse_args()

    project = args.project
    bug_id = str(args.bug_id)
    # Defects4J checkout expects the version with a 'b' (buggy) suffix.
    version = f"{bug_id}b"

    # The mounted directory is the shared volume; the actual checkout goes into a
    # per-bug subdirectory that Defects4J creates (it refuses a pre-existing
    # working directory).
    mount_dir = os.path.abspath(args.workdir)
    os.makedirs(mount_dir, exist_ok=True)
    workdir = os.path.join(mount_dir, f"{project}_{bug_id}")
    if os.path.exists(workdir):
        shutil.rmtree(workdir)

    results_dir = os.path.join("results", project, bug_id)
    if args.iteration is not None:
        results_dir = os.path.join(results_dir, str(args.iteration))
    os.makedirs(results_dir, exist_ok=True)

    client = docker.from_env()
    container = start_container(client, mount_dir)

    try:
        # 1. Checkout the buggy version.
        checkout = run_step(
            container,
            f"defects4j checkout -p {project} -v {version} -w {workdir}",
            workdir=None,
            description=f"Checking out {project} {version} into {workdir}",
        )
        if not checkout.ok:
            print(checkout.output)
            sys.exit("[experiment] Checkout failed; aborting.")

        # 2. Compile the buggy sources.
        compile_res = run_step(
            container, "defects4j compile", workdir,
            description="Compiling buggy sources",
        )
        if not compile_res.ok:
            print(compile_res.output)
            sys.exit("[experiment] Compilation failed; aborting.")

        # 3. Run the test suite (pre-fix) to confirm the bug is present.
        test_before = run_step(
            container, "defects4j test", workdir,
            description="Running test suite (pre-fix)",
        )
        failing_before = parse_failing_tests(test_before.output)
        print(f"[experiment]   Failing tests before fix: {failing_before}")
        if failing_before == 0:
            print(
                "[experiment] WARNING: no failing tests before fix — "
                "the bug may not be reproduced as expected."
            )

        # 4. Extract bug metadata (kept in result.json for reference; no longer
        #    part of the prompt).
        info = run_step(
            container, f"defects4j info -p {project} -b {bug_id}", workdir=None,
            description="Extracting bug metadata (defects4j info)",
        )

        # 4b. Optionally fetch the original bug-tracker issue report.
        issue_text = None
        if args.include_issue:
            bug_report_url = extract_bug_report_url(info.output)
            issue_text = fetch_issue_text(bug_report_url) if bug_report_url else ""

        # 5. Locate and read the buggy sources (Defects4J-specific).
        files = locate_source_files(container, workdir)
        sources = read_sources(files)
        if not sources:
            print("[experiment] WARNING: no buggy source files could be read.")

        # 5b. Fetch the trigger tests. Needed both for the optional prompt
        #     context and — always — to judge whether the bug itself is fixed.
        trigger_tests = get_trigger_tests(container, workdir)
        if not trigger_tests:
            print("[experiment] WARNING: no trigger tests found.")

        test_sources = None
        if args.include_test_code and trigger_tests:
            test_classes = sorted({t.split("::")[0] for t in trigger_tests})
            test_files = locate_test_files(container, workdir, test_classes)
            full_test_sources = read_sources(test_files)
            # Pass only the failing trigger method(s), not the whole test file.
            test_sources = extract_trigger_test_code(trigger_tests, full_test_sources)

        test_log = None
        if args.include_test_log and trigger_tests:
            test_log = run_trigger_tests(container, workdir, trigger_tests)

        # 6. Generate the fix (dataset/Docker-agnostic).
        generator = FixGenerator(model=args.model, temperature=args.temperature)
        gen = generator.generate(
            sources,
            test_sources=test_sources, test_log=test_log, issue_text=issue_text,
            results_dir=results_dir,
        )

        # 7. Apply the diff and validate by re-running the test suite.
        applied, apply_log = apply_diff(container, workdir, gen["diff"])
        print(f"[experiment] Diff applied: {applied}")

        failing_after = -1
        test_after_log = ""
        failing_after_names = []
        if applied:
            test_after = exec_in_container(
                container, "defects4j test", workdir=workdir
            )
            test_after_log = test_after.output
            failing_after = parse_failing_tests(test_after_log)
            failing_after_names = parse_failing_test_names(test_after_log)
            print(f"[experiment] Failing tests after fix: {failing_after}")

        failing_before_names = parse_failing_test_names(test_before.output)
        triggers_fixed, new_failures, fixed = evaluate_fix(
            trigger_tests, failing_before_names, failing_after_names, applied
        )

        # 8. Persist validation artifacts and the combined result.
        write_text(results_dir, "apply.log", apply_log)
        write_text(results_dir, "test_before.log", test_before.output)
        write_text(results_dir, "test_after.log", test_after_log)
        if test_log is not None:
            write_text(results_dir, "regression_test.log", test_log)
        if issue_text is not None:
            write_text(results_dir, "issue.txt", issue_text)

        result = {
            "project": project,
            "bug_id": bug_id,
            "model": gen["model"],
            "temperature": gen["temperature"],
            "timestamp": gen["timestamp"],
            "elapsed_seconds": gen["elapsed_seconds"],
            "applied": applied,
            "fixed": fixed,
            "triggers_fixed": triggers_fixed,
            "trigger_tests": trigger_tests,
            "new_failures": new_failures,
            "failing_tests_before": failing_before,
            "failing_tests_after": failing_after,
            "modified_files": [rel for rel, _ in files],
            "bug_metadata": info.output,
            "usage_metadata": gen["usage_metadata"],
            "raw_response": gen["raw_response"],
            "included_test_code": test_sources is not None,
            "included_test_log": test_log is not None,
            "included_issue": issue_text is not None,
        }
        write_text(results_dir, "result.json", json.dumps(result, indent=2))

        print("\n[experiment] ===== Summary =====")
        print(f"[experiment] Applied: {result['applied']}  Fixed: {result['fixed']}")
        print(
            f"[experiment] Trigger tests: "
            f"{'all pass' if triggers_fixed else 'still failing'}"
        )
        if new_failures:
            print(f"[experiment] Regressions introduced by the patch: {new_failures}")
        print(
            f"[experiment] Failing tests (whole suite): "
            f"{result['failing_tests_before']} -> {result['failing_tests_after']}"
        )
        print(f"[experiment] Results stored under: {results_dir}/")

    finally:
        print("[experiment] Stopping and removing container ...")
        try:
            container.stop()
        finally:
            container.remove()
        print("[experiment] Container removed.")


if __name__ == "__main__":
    main()
