"""
experiment_runner.py — the machinery shared by everything that drives
``Experiment.py`` in bulk.

``run_iterations.py`` (N runs of one bug, to average out LLM non-determinism)
and ``run_project.py`` (one run of every bug in a project, for the full
benchmark campaign) need the same things: expand a ``--bug-id`` selection,
forward the fix/FixCheck flags without duplicating their defaults, delete a
bug's checkout, read back its ``result.json``. Those helpers started in
``run_iterations.py``; they live here so there is exactly one copy of each.

On top of that this module owns the two things a long unattended campaign
needs and a handful of interactive runs never did:

- :func:`run_experiment` enforces a real per-bug timeout. ``llms/ollama_llm.py``
  passes no client timeout, so a wedged Ollama daemon would otherwise hang a
  SLURM job for its entire wall-clock allocation.
- :func:`reap_containers` removes containers left behind by a run that was
  killed. ``Experiment.py`` stops its container in a ``finally``, but Python
  does not run ``finally`` blocks when the process is SIGKILLed, so a timed-out
  or cancelled run leaks one.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
EXPERIMENT = os.path.join(HERE, "Experiment.py")

DEFECTS4J_IMAGE = "defects4j:3.0.1"


# ------------------------------------------------------------- bug selection

def parse_bug_ids(tokens):
    """Expand ``--bug-id`` tokens (single ids and ``lo-hi`` ranges) into ids.

    Each token is either a plain id (``"7"`` -> ``[7]``) or an inclusive range
    (``"1-5"`` -> ``[1, 2, 3, 4, 5]``). Order is preserved and duplicates are
    dropped, so ``["1-3", "5", "5"]`` yields ``[1, 2, 3, 5]``. Ids are returned
    as strings to match the ``results/.../Bug_<bug>`` path convention.

    Tokens may also be comma-separated (``["1-5,8"]``), which is how a
    selection survives ``sbatch --export`` — that mechanism cannot carry a
    space without quoting gymnastics, so the campaign wire format is commas
    while ``--bug-id 1-5 8`` keeps working interactively.
    """
    ids, seen = [], set()
    for raw in tokens:
        for token in str(raw).replace(",", " ").split():
            if "-" in token:
                lo, hi = token.split("-", 1)
                span = range(int(lo), int(hi) + 1)
            else:
                span = [int(token)]
            for n in span:
                if n not in seen:
                    seen.add(n)
                    ids.append(str(n))
    return ids


# --------------------------------------------------- Experiment.py CLI flags

def add_experiment_flags(parser):
    """Register the ``Experiment.py`` flags every bulk runner forwards.

    Every option defaults to ``None``/off so that :func:`experiment_args`
    forwards only what the caller actually set and ``Experiment.py`` keeps
    ownership of the real defaults. Registering them from one place is what
    stops ``run_iterations.py`` and ``run_project.py`` from drifting apart.
    """
    parser.add_argument(
        "--model", default=None,
        help="LLM model identifier (default: Experiment.py's default).",
    )
    parser.add_argument(
        "--temperature", type=float, default=None,
        help="LLM sampling temperature (default: Experiment.py's default).",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=None,
        help="Maximum tokens to generate (default: Experiment.py's default). "
             "For a reasoning model this budget covers the chain of thought too.",
    )
    parser.add_argument(
        "--context-length", type=int, default=None,
        help="Context window for prompt + generation (default: Experiment.py's "
             "default). The Ollama daemon must serve at least this much.",
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
        help="Run FixCheck on plausible patches (forwarded to Experiment.py).",
    )
    parser.add_argument(
        "--fixcheck-prefixes", type=int, default=None,
        help="FixCheck number-of-prefixes (default: Experiment.py's default).",
    )
    parser.add_argument(
        "--fixcheck-assertions", default=None,
        help="FixCheck assertion-generator option (default: Experiment.py's default).",
    )
    parser.add_argument(
        "--fixcheck-inputs-class", default=None,
        help="Force FixCheck's inputs-class (default: Experiment.py's heuristic).",
    )
    parser.add_argument(
        "--fixcheck-similarity-threshold", type=float, default=None,
        help="FixCheck suspicious-verdict similarity threshold "
             "(default: Experiment.py's default).",
    )
    return parser


def experiment_args(args):
    """Translate a bulk runner's parsed args into Experiment.py CLI flags.

    Only options the caller actually set are forwarded, so Experiment.py keeps
    ownership of the defaults (model, temperature) — there is no second copy of
    them to drift out of sync here.
    """
    forwarded = []
    if args.model is not None:
        forwarded += ["--model", args.model]
    if args.temperature is not None:
        forwarded += ["--temperature", str(args.temperature)]
    if getattr(args, "max_tokens", None) is not None:
        forwarded += ["--max-tokens", str(args.max_tokens)]
    if getattr(args, "context_length", None) is not None:
        forwarded += ["--context-length", str(args.context_length)]
    if args.include_test_code:
        forwarded.append("--include-test-code")
    if args.include_test_log:
        forwarded.append("--include-test-log")
    if args.include_issue:
        forwarded.append("--include-issue")
    if args.fixcheck:
        forwarded.append("--fixcheck")
    if args.fixcheck_prefixes is not None:
        forwarded += ["--fixcheck-prefixes", str(args.fixcheck_prefixes)]
    if args.fixcheck_assertions is not None:
        forwarded += ["--fixcheck-assertions", args.fixcheck_assertions]
    if args.fixcheck_inputs_class is not None:
        forwarded += ["--fixcheck-inputs-class", args.fixcheck_inputs_class]
    if args.fixcheck_similarity_threshold is not None:
        forwarded += ["--fixcheck-similarity-threshold",
                      str(args.fixcheck_similarity_threshold)]
    return forwarded


# ------------------------------------------------------------ results layout

def result_dir(model_dir, project, bug_id, iteration=None):
    """Directory ``Experiment.py`` writes a run's artifacts to.

    ``iteration`` is ``None`` for the one-run-per-bug campaign and an index
    for ``run_iterations.py``, mirroring ``Experiment.py``'s ``--iteration``.
    """
    path = os.path.join("results", model_dir, project, f"Bug_{bug_id}")
    return path if iteration is None else os.path.join(path, str(iteration))


def load_result(model_dir, project, bug_id, iteration=None):
    """Load a run's result.json, or None if it is missing/unreadable."""
    path = os.path.join(result_dir(model_dir, project, bug_id, iteration), "result.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clean_checkout(workdir, project, bug_id):
    """Remove the per-bug checkout so the next run starts from a clean slate.

    ``Experiment.py`` checks out into ``<workdir>/<project>_<bug_id>`` and
    deletes it at the *start* of a run but never at the end. Over a whole
    project that leaves one ~400 MB tree per bug behind, so a bulk runner has
    to reclaim it itself.
    """
    checkout = os.path.join(os.path.abspath(workdir), f"{project}_{bug_id}")
    if os.path.isdir(checkout):
        shutil.rmtree(checkout, ignore_errors=True)


# --------------------------------------------------------- running one bug

@dataclass
class RunOutcome:
    """What happened to one ``Experiment.py`` invocation.

    ``status`` is ``"ok"`` (exit 0), ``"error"`` (non-zero exit — an expected
    outcome for bugs whose checkout or compile fails under this image) or
    ``"timeout"``.
    """

    status: str
    exit_code: int
    seconds: float

    @property
    def ok(self):
        return self.status == "ok"


def run_experiment(project, bug_id, workdir, forwarded, iteration=None,
                   timeout=None, log_path=None, echo=True, kill_grace=120):
    """Invoke ``Experiment.py`` for a single bug, with a real timeout.

    The child is put in its own process group so that a timeout kills the whole
    tree, not just the Python parent. It is first sent SIGTERM — ``Experiment.py``
    turns that into a ``SystemExit`` so its ``finally`` can stop and remove the
    Docker container — and only SIGKILLed if it has not exited after
    ``kill_grace`` seconds.

    Output is streamed to ``log_path`` when given (one file per bug: a single
    ``defects4j test`` on Closure produces enough output to make a shared log
    unusable), and echoed to stdout otherwise.
    """
    cmd = [
        sys.executable, EXPERIMENT,
        "--project", project,
        "--bug-id", str(bug_id),
        "--workdir", workdir,
        *(["--iteration", str(iteration)] if iteration is not None else []),
        *forwarded,
    ]
    if echo:
        print("$ " + " ".join(cmd), flush=True)

    started = time.time()
    log_file = None
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_file = open(log_path, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=log_file or None,
            stderr=subprocess.STDOUT if log_file else None,
            start_new_session=True,
        )
        try:
            exit_code = process.wait(timeout=timeout)
            status = "ok" if exit_code == 0 else "error"
        except subprocess.TimeoutExpired:
            _terminate_group(process, kill_grace)
            exit_code = process.returncode if process.returncode is not None else -1
            status = "timeout"
    finally:
        if log_file:
            log_file.close()
    return RunOutcome(status=status, exit_code=exit_code, seconds=round(time.time() - started, 1))


def _terminate_group(process, kill_grace=120):
    """SIGTERM the child's process group, then SIGKILL what survives."""
    for sig, grace in ((signal.SIGTERM, kill_grace), (signal.SIGKILL, 10)):
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


# ------------------------------------------------------- container cleanup

def reap_containers(mount_root):
    """Remove Defects4J containers still mounting ``mount_root``.

    A run killed hard (timeout, ``scancel``) leaves its container running: the
    node currently hosts one that has been up for four weeks for exactly this
    reason. Filtering by the mount root is only safe because each job gets its
    own (``workspace/job_<slurm_id>``), so this can never touch a sibling job's
    container.

    Returns the ids removed; never raises, since cleanup failing must not
    abort a campaign.
    """
    mount_root = os.path.abspath(mount_root)
    try:
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"volume={mount_root}"],
            capture_output=True, text=True, timeout=60,
        )
        ids = [c for c in listed.stdout.split() if c]
        if not ids:
            return []
        subprocess.run(["docker", "rm", "-f", *ids],
                       capture_output=True, text=True, timeout=120)
        print(f"[runner] Reaped {len(ids)} leftover container(s): {' '.join(ids)}",
              flush=True)
        return ids
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[runner] WARNING: could not reap containers: {exc}", flush=True)
        return []
