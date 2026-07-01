"""
Experiment.py — Orchestrator for LLM-based bug fix generation and evaluation.

Given a Defects4J project and bug id, this script:
  1. Starts an ephemeral Docker container from the ``defects4j:3.0.1`` image,
     mounting the host working directory as a shared volume.
  2. Checks out the buggy version of the project.
  3. Compiles it and runs the test suite to confirm the bug is present.
  4. Extracts bug metadata with ``defects4j info``.
  5. Locates and reads the buggy source file(s).
  6. Delegates *fix generation* to ``FixGenerator`` (dataset/Docker-agnostic).
  7. Applies the generated diff and re-runs the test suite to validate the fix.
  8. Persists all artifacts under ``results/<project>/<bug_id>/``.

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

import docker

from docker_utils import exec_in_container
from FixGenerator import FixGenerator, normalize_diff

DEFECTS4J_IMAGE = "defects4j:3.0.1"
DEFAULT_MODEL = "ollama/gpt-oss:20b"

DIFF_FILENAME = "_llm_fix.diff"  # temp diff written into the mounted workdir


def parse_failing_tests(output: str) -> int:
    """Parse the number of failing tests from ``defects4j test`` output.

    Returns the count, or -1 if the expected line is not present.
    """
    match = re.search(r"Failing tests:\s*(\d+)", output)
    return int(match.group(1)) if match else -1


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


# ------------------------------------------------------------------- apply

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
        result = exec_in_container(container, cmd, workdir=None)
        logs.append(f"$ {result.command}\n(exit {result.exit_code})\n{result.output}")
        if result.ok:
            return True, "\n\n".join(logs)
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

        # 4. Extract bug metadata.
        info = run_step(
            container, f"defects4j info -p {project} -b {bug_id}", workdir=None,
            description="Extracting bug metadata (defects4j info)",
        )

        # 5. Locate and read the buggy sources (Defects4J-specific).
        files = locate_source_files(container, workdir)
        sources = read_sources(files)
        if not sources:
            print("[experiment] WARNING: no buggy source files could be read.")

        # 6. Generate the fix (dataset/Docker-agnostic).
        generator = FixGenerator(model=args.model, temperature=args.temperature)
        gen = generator.generate(info.output, sources, results_dir=results_dir)

        # 7. Apply the diff and validate by re-running the test suite.
        applied, apply_log = apply_diff(container, workdir, gen["diff"])
        print(f"[experiment] Diff applied: {applied}")

        failing_after = -1
        test_after_log = ""
        if applied:
            test_after = exec_in_container(
                container, "defects4j test", workdir=workdir
            )
            test_after_log = test_after.output
            failing_after = parse_failing_tests(test_after_log)
            print(f"[experiment] Failing tests after fix: {failing_after}")

        fixed = applied and failing_after == 0

        # 8. Persist validation artifacts and the combined result.
        write_text(results_dir, "apply.log", apply_log)
        write_text(results_dir, "test_before.log", test_before.output)
        write_text(results_dir, "test_after.log", test_after_log)

        result = {
            "project": project,
            "bug_id": bug_id,
            "model": gen["model"],
            "temperature": gen["temperature"],
            "timestamp": gen["timestamp"],
            "elapsed_seconds": gen["elapsed_seconds"],
            "applied": applied,
            "fixed": fixed,
            "failing_tests_before": failing_before,
            "failing_tests_after": failing_after,
            "modified_files": [rel for rel, _ in files],
            "bug_metadata": info.output,
            "usage_metadata": gen["usage_metadata"],
            "raw_response": gen["raw_response"],
        }
        write_text(results_dir, "result.json", json.dumps(result, indent=2))

        print("\n[experiment] ===== Summary =====")
        print(f"[experiment] Applied: {result['applied']}  Fixed: {result['fixed']}")
        print(
            f"[experiment] Failing tests: {result['failing_tests_before']} -> "
            f"{result['failing_tests_after']}"
        )
        print(f"[experiment] Results stored under: results/{project}/{bug_id}/")

    finally:
        print("[experiment] Stopping and removing container ...")
        try:
            container.stop()
        finally:
            container.remove()
        print("[experiment] Container removed.")


if __name__ == "__main__":
    main()
