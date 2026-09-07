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
  7b. Optionally (``--fixcheck``), when the patch is plausible (applied and
      every trigger test passing), runs FixCheck (vendored in ``fixcheck/``)
      to flag likely-overfitting patches. Advisory only; never changes
      ``fixed``.
  8. Persists all artifacts under ``results/<model>/<project>/Bug_<bug_id>/``
     (or ``results/<model>/<project>/Bug_<bug_id>/<iteration>/`` when
     ``--iteration`` is given), where ``<model>`` is ``--model`` with any
     ``<provider>/`` prefix stripped (see ``model_dir_name``).

The container is always stopped and removed at the end of the run.

Usage:
    python Experiment.py --project Lang --bug-id 1 --workdir ./workspace
"""

import argparse
import json
import os
import re
import shutil
import signal
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import docker
from dotenv import load_dotenv

from d4j.defects4j_bugs import (
    ISSUE_AVAILABLE,
    ISSUE_EMPTY,
    ISSUE_UNCACHED,
    ISSUE_UNUSABLE,
    issue_text,
)
from docker_utils import exec_in_container, export_property, run_step
from FixCheckWrapper import (
    DEFAULT_FIXCHECK_ASSERTIONS,
    DEFAULT_FIXCHECK_PREFIXES,
    DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD,
    FIXCHECK_ASSERTION_GENERATORS,
    FIXCHECK_DIR,
    FIXCHECK_JAR,
    FixCheckWrapper,
    group_triggers_by_class,
    needs_host_network,
    validate_assertion_generator,
    write_fixcheck_failure_logs,
)
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


def evaluate_fix(trigger_tests, failing_before_names, failing_after_names, applied,
                 evaluated=True):
    """Decide whether a Defects4J bug is fixed by a candidate patch.

    A bug is fixed when every trigger test passes again and the patch
    introduces no new failures (no test that passed before now fails). Judging
    against the trigger tests rather than a zero total is robust to
    environment-flaky tests (e.g. ``SystemUtils``' user-home test under
    ``HOME=/tmp``) that fail regardless of the patch.

    ``evaluated`` says whether the post-fix test run actually produced results.
    It has to be passed explicitly because "no test is reported failing" is
    ambiguous: it is also what a run that **never compiled** looks like, since
    ``defects4j test`` then prints no ``Failing tests:`` line at all and the
    parsed failure list comes back empty. Without this guard a patch that does
    not compile scores as a perfect fix -- 203 of the campaign's runs did
    exactly that.

    Args:
        trigger_tests: The bug's trigger tests (``Class::method`` strings).
        failing_before_names: Tests failing before the patch.
        failing_after_names: Tests failing after the patch.
        applied: Whether the patch was applied at all.
        evaluated: Whether the post-fix test run produced a usable result
            (``parse_failing_tests`` returned something other than ``-1``).

    Returns:
        ``(triggers_fixed, new_failures, fixed)``.
    """
    trigger_set = set(trigger_tests)
    before = set(failing_before_names)
    after = set(failing_after_names)
    triggers_fixed = (
        applied and evaluated and bool(trigger_set) and not (trigger_set & after)
    )
    new_failures = sorted(after - before)
    fixed = triggers_fixed and not new_failures
    return triggers_fixed, new_failures, fixed


def start_container(client, mount_dir, extra_mounts=None, network_mode=None):
    """Start an ephemeral Defects4J container with the workdir mounted.

    The host directory is bound to the *same absolute path* inside the container
    so that ``-w <workdir>`` is valid on both sides and the checked-out sources
    are visible on the host through the shared volume.

    The container runs as the host user (same uid:gid) so that files created on
    the shared volume are owned by the host user. This avoids Git's "dubious
    ownership" error and lets us read/write/clean the checkout from the host.

    Args:
        client: Docker SDK client.
        mount_dir: Host directory bound read-write at the same absolute path.
        extra_mounts: Optional dict of additional ``{host_path: {"bind": ...,
            "mode": ...}}`` volume entries merged into the container's
            ``volumes`` -- used to mount FIXCHECK_DIR read-only for
            ``--fixcheck``.
        network_mode: Optional Docker network mode. Defaults to the usual
            bridge network; ``"host"`` is needed when something inside the
            container has to reach a daemon on the host's ``localhost``, as
            FixCheck's Ollama-backed assertion generators do (see
            ``FixCheckWrapper.needs_host_network``).
    """
    print(f"[experiment] Starting container from {DEFECTS4J_IMAGE} ...")
    volumes = {mount_dir: {"bind": mount_dir, "mode": "rw"}}
    if extra_mounts:
        volumes.update(extra_mounts)
    container = client.containers.run(
        DEFECTS4J_IMAGE,
        detach=True,
        tty=True,
        user=f"{os.getuid()}:{os.getgid()}",
        environment={
            # HOME must be writable by the host uid for `git config --global`.
            "HOME": "/tmp",
            # The image ships no locale, so the JVM picks file.encoding=
            # ANSI_X3.4-1968 (US-ASCII) and javac rejects any source holding a
            # non-ASCII byte -- Math 69's RandomKey.java has an en dash in a
            # Javadoc citation, which is enough to fail `defects4j compile`
            # outright. Defects4J's ant targets set no -encoding, so fixing the
            # locale is the only lever we have.
            "LANG": "C.UTF-8",
        },
        volumes=volumes,
        network_mode=network_mode,
    )
    print(f"[experiment] Container started: {container.short_id}")

    # Trust the mounted directories for Git, as a safety net regardless of
    # ownership, so defects4j's git operations and our later `git apply` work.
    exec_in_container(container, "git config --global --add safe.directory '*'")

    return container


# ----------------------------------------------------------------- sources

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


def run_trigger_tests_raw(container, workdir, trigger_tests):
    """Run each trigger test in isolation and return its raw failure content.

    Uses the ``failing_tests`` file that Defects4J writes to ``workdir``
    rather than the ant console output, because the Formatter writes
    exception types and full stack traces directly to that file — they never
    appear on stdout.

    Returns a dict mapping each ``"FQCN::method"`` trigger to ``(cmd,
    content)``, where ``content`` is the raw file content with no header —
    the shape FixCheck's ``test-failure-trace-log`` property needs (see
    ``write_fixcheck_failure_logs``). ``run_trigger_tests`` rebuilds the
    historical ``"$ cmd\\n<content>"`` log format on top of this for
    ``--include-test-log``.
    """
    raw = {}
    for trigger_test in trigger_tests:
        cmd = f"defects4j test -t {trigger_test} -w {workdir}"
        exec_in_container(container, cmd, workdir=None)
        failing_tests_path = os.path.join(workdir, "failing_tests")
        try:
            with open(failing_tests_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except FileNotFoundError:
            content = ""
        raw[trigger_test] = (cmd, content)
    return raw


def run_trigger_tests(container, workdir, trigger_tests):
    """Run each trigger test in isolation and return the combined log."""
    raw = run_trigger_tests_raw(container, workdir, trigger_tests)
    logs = [f"$ {cmd}\n{content}" for cmd, content in raw.values()]
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


def extract_trigger_method_sources_by_class(trigger_tests, test_file_sources):
    """Map each trigger class to the source of its trigger method(s).

    Like :func:`extract_trigger_test_code`, but keyed by fully-qualified
    class name instead of file path — what
    :class:`FixCheckWrapper.FixCheckWrapper`'s per-class orchestration needs
    to run ``select_fixcheck_inputs`` on.

    Args:
        trigger_tests: List of ``"FQCN::method"`` strings.
        test_file_sources: List of ``(relative_path, content)`` for the test
            file(s), as read from disk.

    Returns:
        Dict mapping each trigger FQCN to a ``{method: source}`` dict. A
        method missing from the inner dict could not be located in the
        class's own source file -- typically because it is *inherited* (Lang
        10's ``FastDateFormat_ParserTest`` extends ``FastDateParserTest`` and
        inherits ``testLANG_831``). FixCheck parses only the named class's
        file, so it cannot analyze those either. Classes whose source file is
        missing from ``test_file_sources`` are omitted entirely.
    """
    sources_by_class = {}
    for cls, methods in group_triggers_by_class(trigger_tests).items():
        content = next(
            (c for rel, c in test_file_sources
             if rel.endswith(cls.replace(".", "/") + ".java")),
            None,
        )
        if content is None:
            continue
        found = {}
        for method in methods:
            code = extract_java_method(content, method)
            if code:
                found[method] = code
        sources_by_class[cls] = found
    return sources_by_class


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
    headers = {"User-Agent": "FixCheckEvaluation"}
    # Unauthenticated api.github.com allows 60 requests/hour per IP. A full
    # benchmark run touches 280 GitHub-tracked bugs at two calls each, and
    # several jobs share the node's IP, so without a token nearly all of those
    # issue fetches fail -- silently, since fetch_issue_text swallows the error.
    token = os.getenv("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_jira_issue(url):
    """Fetch title/description for an Apache Jira issue URL."""
    key = url.rstrip("/").split("/")[-1]
    api_url = (
        f"https://issues.apache.org/jira/rest/api/2/issue/{key}"
        "?fields=summary,description"
    )
    data = _http_get_json(api_url)
    fields = data.get("fields", {})
    parts = [
        f"Summary: {fields.get('summary', '')}",
        f"Description:\n{fields.get('description', '') or ''}",
    ]
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

    Prefer :func:`get_issue_text`, which reads the pre-downloaded cache first.
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


def get_issue_text(project, bug_id, bug_report_url):
    """The bug's issue report as prompt context, plus *why* when there is none.

    Returns ``(text, status)``; see ``d4j.defects4j_bugs.issue_text`` for the
    statuses. 40 of the 854 bugs have no usable issue -- 18 Chart bugs have no
    URL at all, one Jsoup issue was deleted from GitHub, and the 22 SourceForge
    ones scrape to a navigation menu rather than the ticket -- so a bare empty
    string would be ambiguous. The status is printed and stored in
    ``result.json`` so a run says which case it was.

    ``d4j/fetch_issues.py`` downloads all 854 issues once; only a bug that is
    not cached falls back to the network, which keeps a one-off run working on
    a machine without the cache.
    """
    text, status = issue_text(project, bug_id)
    if status == ISSUE_AVAILABLE:
        print(f"[experiment] Issue report: {len(text)} chars (cached)")
        return text, status
    if status == ISSUE_UNCACHED:
        if not bug_report_url:
            print("[experiment] Issue report: none (Defects4J lists no URL for this bug)")
            return "", ISSUE_EMPTY
        print(f"[experiment] Issue report not cached; fetching {bug_report_url} "
              "(run `python -m d4j.fetch_issues` to avoid this)")
        fetched = fetch_issue_text(bug_report_url)
        return (fetched, ISSUE_AVAILABLE) if fetched.strip() else ("", ISSUE_EMPTY)
    reason = {
        ISSUE_EMPTY: "the tracker has no text for it",
        ISSUE_UNUSABLE: "its tracker does not scrape into usable context",
    }[status]
    print(f"[experiment] Issue report: none ({status}) -- {reason}; "
          "the prompt will carry no issue")
    return "", status


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


def model_dir_name(model):
    """Strip the provider prefix (e.g. ``ollama/``) for use as a directory name.

    Model identifiers are ``<provider>/<model>`` (see FixGenerator's provider
    dispatch); results are grouped by the bare model name, so
    ``ollama/qwen3.6:35b`` becomes ``qwen3.6:35b``.
    """
    return model.split("/", 1)[-1]


def write_text(results_dir, filename, content):
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def _exit_on_sigterm(signum, _frame):
    """Turn SIGTERM into SystemExit so the container teardown still runs.

    Python does not run ``finally`` blocks when the default SIGTERM handler
    fires, so a run killed by a bulk runner's timeout or by ``scancel`` would
    leak its Docker container -- exactly how a stray ``defects4j:3.0.1``
    container ends up running for weeks. Raising instead lets ``main()``'s
    ``finally`` stop and remove it.
    """
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, _exit_on_sigterm)

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
        "--fixcheck", action="store_true",
        help="After a plausible fix (applied and every trigger test passing), "
             "run FixCheck (vendored in fixcheck/) to check for overfitting: "
             "it mutates the trigger test's inputs, reruns the variations "
             "against the patched program, and flags the patch as suspicious "
             "when a variation fails the same way as the original bug. "
             "Advisory only -- never changes 'fixed'. Requires the jar built "
             "by scripts/buildFixcheck.sh.",
    )
    parser.add_argument(
        "--fixcheck-prefixes", type=int, default=DEFAULT_FIXCHECK_PREFIXES,
        help="Number of input variations ('prefixes') FixCheck generates per "
             f"trigger method (default: {DEFAULT_FIXCHECK_PREFIXES}).",
    )
    parser.add_argument(
        "--fixcheck-assertions", default=DEFAULT_FIXCHECK_ASSERTIONS,
        type=validate_assertion_generator,
        metavar="GENERATOR",
        help="FixCheck's assertion-generation strategy (default: "
             f"{DEFAULT_FIXCHECK_ASSERTIONS!r}). One of "
             f"{FIXCHECK_ASSERTION_GENERATORS}, or "
             "'ollama:<model>[@[<host>:]<port>]' to use any model served by an "
             "Ollama daemon (e.g. 'ollama:gpt-oss:120b@1995'). The Ollama-backed "
             "ones ask the daemon to write the assertions, so the container runs "
             "with host networking when the daemon is on localhost; the legacy "
             "'codellama' and 'llama3.1' additionally require the model to be "
             "pulled under the exact tag FixCheck hardcodes ('codellama:latest', "
             "'llama3.1:latest'). 'gpt-3.5' and 'replit-code-llm' are not "
             "wired up for this project's container/network setup yet.",
    )
    parser.add_argument(
        "--fixcheck-inputs-class", default=None,
        help="Force FixCheck's inputs-class (e.g. 'int', 'java.lang.String') "
             "instead of inferring it from the trigger test source.",
    )
    parser.add_argument(
        "--fixcheck-similarity-threshold", type=float,
        default=DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD,
        help="Minimum failure-similarity score (0-1) a FixCheck failing "
             "variation needs to mark the patch suspicious (default: "
             f"{DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD}).",
    )
    parser.add_argument(
        "--iteration", default=None,
        help="Iteration index; when set, artifacts go to "
             "results/<model>/<project>/Bug_<bug>/<iteration>/ instead of "
             "results/<model>/<project>/Bug_<bug>/. "
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

    results_dir = os.path.join("results", model_dir_name(args.model), project, f"Bug_{bug_id}")
    if args.iteration is not None:
        results_dir = os.path.join(results_dir, str(args.iteration))
    os.makedirs(results_dir, exist_ok=True)

    if args.fixcheck and not os.path.isfile(FIXCHECK_JAR):
        sys.exit(
            "[experiment] --fixcheck requires the FixCheck jar, not found at "
            f"{FIXCHECK_JAR}. Build it first with: bash scripts/buildFixcheck.sh"
        )

    client = docker.from_env()
    extra_mounts = {FIXCHECK_DIR: {"bind": FIXCHECK_DIR, "mode": "ro"}} if args.fixcheck else None
    # The network mode is fixed when the container is created, so an
    # Ollama-backed assertion generator has to be accounted for up front.
    network_mode = (
        "host" if args.fixcheck and needs_host_network(args.fixcheck_assertions) else None
    )
    if network_mode == "host":
        print(f"[experiment] Using host networking so FixCheck's "
              f"'{args.fixcheck_assertions}' generator can reach Ollama.")
    container = start_container(
        client, mount_dir, extra_mounts=extra_mounts, network_mode=network_mode
    )

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
        issue_report = None
        issue_status = "not-requested"
        if args.include_issue:
            issue_report, issue_status = get_issue_text(
                project, bug_id, extract_bug_report_url(info.output)
            )

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

        # 5c. Read the trigger test source(s): --include-test-code wants only
        #     the failing method(s) per file; --fixcheck wants the same
        #     content keyed by class instead (for select_fixcheck_inputs).
        test_sources = None
        trigger_method_sources = None
        if trigger_tests and (args.include_test_code or args.fixcheck):
            test_classes = sorted({t.split("::")[0] for t in trigger_tests})
            test_files = locate_test_files(container, workdir, test_classes)
            full_test_sources = read_sources(test_files)
            if args.include_test_code:
                # Pass only the failing trigger method(s), not the whole file.
                test_sources = extract_trigger_test_code(trigger_tests, full_test_sources)
            if args.fixcheck:
                trigger_method_sources = extract_trigger_method_sources_by_class(
                    trigger_tests, full_test_sources
                )

        # 5d. Run each trigger test in isolation to capture its raw failure.
        #     --include-test-log wants it for the prompt; --fixcheck needs the
        #     *pre-fix* trace written to disk now, before fix generation --
        #     by the time FixCheck runs, the patch is applied and Defects4J's
        #     `failing_tests` file has been overwritten. Both flags share the
        #     same isolated test runs so neither doubles the work.
        test_log = None
        if trigger_tests and (args.include_test_log or args.fixcheck):
            trigger_raw = run_trigger_tests_raw(container, workdir, trigger_tests)
            if args.include_test_log:
                test_log = "\n\n".join(
                    f"$ {cmd}\n{content}" for cmd, content in trigger_raw.values()
                )
            if args.fixcheck:
                write_fixcheck_failure_logs(workdir, trigger_tests, trigger_raw)

        # 6. Generate the fix (dataset/Docker-agnostic).
        generator = FixGenerator(model=args.model, temperature=args.temperature)
        gen = generator.generate(
            sources,
            test_sources=test_sources, test_log=test_log, issue_text=issue_report,
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
        # -1 means `defects4j test` printed no "Failing tests:" line, i.e. the
        # patched sources never compiled. An empty failure list then means "we
        # learnt nothing", not "nothing fails".
        evaluated = failing_after != -1
        if applied and not evaluated:
            print("[experiment] WARNING: the patched sources did not compile, so "
                  "the post-fix test run produced no results; not counted as fixed.")
        triggers_fixed, new_failures, fixed = evaluate_fix(
            trigger_tests, failing_before_names, failing_after_names, applied,
            evaluated=evaluated,
        )

        # 7b. FixCheck overfitting check. Only worth running on a plausible
        #     patch (applied and every trigger test passing) -- otherwise
        #     there is nothing to validate. Advisory: never changes `fixed`.
        fixcheck_result = None
        if args.fixcheck and applied and triggers_fixed:
            fixcheck = FixCheckWrapper(
                num_prefixes=args.fixcheck_prefixes,
                assertion_generator=args.fixcheck_assertions,
                similarity_threshold=args.fixcheck_similarity_threshold,
                inputs_class=args.fixcheck_inputs_class,
            )
            fixcheck_result = fixcheck.run(
                container, workdir, trigger_tests, trigger_method_sources
            )
            for record in fixcheck_result["per_test_class"]:
                run_dir = record.get("run_dir")
                if not run_dir:
                    continue
                simple_name = record["test_class"].rsplit(".", 1)[-1]
                dest = os.path.join(results_dir, "fixcheck", simple_name)
                src_output = os.path.join(run_dir, "fixcheck-output")
                if os.path.isdir(src_output):
                    os.makedirs(dest, exist_ok=True)
                    shutil.copytree(src_output, dest, dirs_exist_ok=True)
                log_src = os.path.join(run_dir, "fixcheck.log")
                if os.path.exists(log_src):
                    os.makedirs(dest, exist_ok=True)
                    shutil.copy(log_src, os.path.join(dest, "fixcheck.log"))
        fixcheck_suspicious = bool(fixcheck_result and fixcheck_result.get("suspicious"))

        # 8. Persist validation artifacts and the combined result.
        write_text(results_dir, "apply.log", apply_log)
        write_text(results_dir, "test_before.log", test_before.output)
        write_text(results_dir, "test_after.log", test_after_log)
        if test_log is not None:
            write_text(results_dir, "regression_test.log", test_log)
        if issue_report is not None:
            write_text(results_dir, "issue.txt", issue_report)

        result = {
            "project": project,
            "bug_id": bug_id,
            "model": gen["model"],
            "temperature": gen["temperature"],
            "timestamp": gen["timestamp"],
            "elapsed_seconds": gen["elapsed_seconds"],
            "applied": applied,
            # False when the patch applied but the sources failed to compile,
            # so the post-fix test run yielded nothing to judge by.
            "compiled_after": bool(applied and evaluated),
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
            # bool(), not "is not None": an issue can be requested and still
            # be absent -- no URL, deleted upstream, or a tracker that does not
            # scrape into usable context -- and reporting that as "the issue was
            # in the prompt" would misdescribe the run. issue_status says which.
            "included_issue": bool(issue_report),
            "issue_status": issue_status,
            "fixcheck": fixcheck_result,
            "fixcheck_suspicious": fixcheck_suspicious,
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
        if fixcheck_result is None:
            if args.fixcheck:
                print("[experiment] FixCheck: not run (patch not plausible)")
        elif not fixcheck_result.get("ok", True):
            print(f"[experiment] FixCheck: not run cleanly ({fixcheck_result.get('error')})")
        elif not fixcheck_result["analyzed_test_classes"]:
            reasons = "; ".join(
                r["error"] for r in fixcheck_result["per_test_class"] if r.get("error")
            )
            print(f"[experiment] FixCheck: no verdict -- nothing analyzed ({reasons})")
        else:
            verdict = "SUSPICIOUS" if fixcheck_result["suspicious"] else "supported"
            print(
                f"[experiment] FixCheck: {fixcheck_result['failing_prefixes']} "
                f"variation(s) failing across "
                f"{fixcheck_result['analyzed_test_classes']} test class(es), "
                f"max similarity "
                f"{fixcheck_result['max_failure_similarity']:.2f} -> {verdict}"
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
