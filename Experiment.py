"""
Experiment.py — Orchestrator for LLM-based bug fix generation and evaluation.

Given a Defects4J project and bug id, this script:
  1. Starts an ephemeral Docker container from the ``defects4j:3.0.1`` image,
     mounting the host working directory as a shared volume.
  2. Checks out the buggy version of the project.
  3. Compiles it and runs the test suite to confirm the bug is present.
  4. Extracts bug metadata with ``defects4j info``.
  5. Delegates fix generation and evaluation to ``FixGenerator``.

The container is always stopped and removed at the end of the run.

Usage:
    python Experiment.py --project Lang --bug-id 1 --workdir ./workspace
"""

import argparse
import os
import shutil
import sys

import docker

from docker_utils import exec_in_container
from FixGenerator import FixGenerator, parse_failing_tests

DEFECTS4J_IMAGE = "defects4j:3.0.1"
DEFAULT_MODEL = "ollama/gpt-oss:20b"


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

        # 5. Generate, apply and evaluate the fix.
        generator = FixGenerator(
            project=project,
            bug_id=bug_id,
            workdir=workdir,
            container=container,
            model=args.model,
            temperature=args.temperature,
        )
        result = generator.run(
            bug_info=info.output,
            test_before_log=test_before.output,
            failing_before=failing_before,
        )

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
