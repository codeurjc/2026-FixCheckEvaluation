"""
Shared helpers for interacting with the Defects4J Docker container.

These utilities wrap the Docker SDK for Python so that both ``Experiment.py``
and ``FixGenerator.py`` run Defects4J commands consistently inside the same
container and capture their output and exit codes.
"""

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
