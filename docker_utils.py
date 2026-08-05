"""
Shared helpers for interacting with the Defects4J Docker container.

These utilities wrap the Docker SDK for Python so that ``Experiment.py``,
``FixGenerator.py`` and ``FixCheckWrapper.py`` run Defects4J commands
consistently inside the same container and capture their output and exit
codes.
"""

import os
from dataclasses import dataclass


@dataclass
class ExecResult:
    """Result of a command executed inside the Docker container."""
    command: str
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        """True when the command exited successfully (exit code 0)."""
        return self.exit_code == 0


def exec_in_container(container, command, workdir=None) -> ExecResult:
    """
    Execute a command inside a running container and capture its output.

    Args:
        container: A running docker container object (docker SDK).
        command: Command to run, either a string or a list of arguments.
        workdir: Optional working directory inside the container.

    Returns:
        ExecResult with the command, exit code and decoded combined output.
    """
    exit_code, output = container.exec_run(command, workdir=workdir, demux=False)

    if isinstance(output, (bytes, bytearray)):
        output = output.decode("utf-8", errors="replace")
    elif output is None:
        output = ""

    command_str = command if isinstance(command, str) else " ".join(command)
    return ExecResult(command=command_str, exit_code=exit_code, output=output)


def run_step(container, command, workdir, description):
    """Run a Defects4J command in the container and echo its result."""
    print(f"[experiment] {description}")
    result = exec_in_container(container, command, workdir=workdir)
    status = "ok" if result.ok else f"FAILED (exit {result.exit_code})"
    print(f"[experiment]   -> {status}")
    return result


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
