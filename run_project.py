"""
run_project.py — run ``Experiment.py`` once for every bug of a Defects4J project.

This is the unit of work of the full-benchmark campaign: one invocation per
SLURM job, walking one project's active bugs sequentially on that job's GPU.
Parallelism comes from submitting several projects at once (see
``scripts/runCampaign.sh``), not from anything here.

It is deliberately *not* ``run_iterations.py`` with ``--iterations 1``: the
campaign runs each bug exactly once, writes its artifacts straight to
``results/<model>/<Project>/Bug_<id>/`` with no iteration subdirectory, and
needs things a handful of interactive runs never did — a per-bug timeout,
per-bug failure isolation, checkout reclamation, and a resume that survives a
job being requeued. The helpers both share live in ``experiment_runner.py``.

Failure isolation is the core of it. ``Experiment.py`` exits non-zero when a
checkout or compile fails, and over 854 bugs that is a normal, expected outcome
for a fair number of them; a project run must record it and carry on rather
than abort with 100 bugs left.

    python run_project.py --project Lang                       # all 61 active bugs
    python run_project.py --project Lang --bug-id 1,3-5 --dry-run
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; .env is then simply not read
    def load_dotenv(*_args, **_kwargs):
        return False

from d4j.defects4j_bugs import PROJECTS, resolve_bug_ids
from experiment_runner import (
    DEFECTS4J_IMAGE,
    add_experiment_flags,
    clean_checkout,
    experiment_args,
    load_result,
    reap_containers,
    result_dir,
    run_experiment,
)

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_TIMEOUT = 7200      # 2 h per bug
DEFAULT_KILL_GRACE = 120

# Marker that scripts/patchDefects4jImage.sh has made the per-project
# dir-layout.csv files writable inside the image.
LAYOUT_WRITABLE_LABEL = "org.fixcheckeval.layout-writable"

# Set by the signal handler so the bug loop can stop cleanly at the next
# boundary instead of leaving a half-finished checkout behind.
_STOP_REQUESTED = False


def _install_signal_handlers():
    """Stop after the current bug on SIGTERM/SIGINT; die on the second one.

    SLURM sends SIGTERM shortly before the wall-clock limit (the sbatch asks
    for it with ``--signal=B:TERM@300``). Finishing the bug in flight and then
    writing the summary is far more useful than being killed mid-checkout.
    """
    def handler(signum, _frame):
        global _STOP_REQUESTED
        if _STOP_REQUESTED:
            print(f"\n[run_project] Second signal {signum}; exiting now.", flush=True)
            sys.exit(143)
        _STOP_REQUESTED = True
        print(f"\n[run_project] Signal {signum} received: finishing the current bug, "
              "then stopping.", flush=True)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


# ------------------------------------------------------------------ preflight

def _port_of(url_or_spec):
    """The port in an ``http://host:port`` URL or an ``ollama:model@host:port`` spec."""
    if not url_or_spec:
        return None
    if url_or_spec.startswith("http"):
        from urllib.parse import urlparse

        parsed = urlparse(url_or_spec)
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    from FixCheckWrapper import parse_ollama_generator

    try:
        backend = parse_ollama_generator(url_or_spec)
    except ValueError:
        return None
    return backend.port if backend else None


def preflight(args):
    """Fail fast, before any GPU time is spent, and return a list of problems.

    The check that earns its keep is the port comparison. ``.env`` pins
    ``OLLAMA_BASE_URL`` to the historical port 1995, and every campaign job
    serves its own Ollama on a port of its own; if the two disagree, the run
    would quietly generate fixes against *another job's* daemon and nothing
    downstream would ever reveal it.
    """
    problems = []

    if args.project not in PROJECTS:
        problems.append(f"unknown project {args.project!r}; expected one of {', '.join(PROJECTS)}")

    # Experiment.py calls load_dotenv() and so picks up .env's OLLAMA_BASE_URL
    # even when the environment has none. If this process did not, every check
    # below would be skipped for want of a value the children then use anyway
    # -- silently degrading the preflight to "no problems found" in exactly the
    # case it exists to catch. Load the same file, so parent and child agree on
    # which daemon the run will talk to.
    load_dotenv()
    base_url = os.getenv("OLLAMA_BASE_URL")
    if not base_url:
        problems.append(
            "OLLAMA_BASE_URL is not set (neither in the environment nor in "
            ".env), so neither the port-mismatch check nor the model-"
            "availability check can run. Set it to this job's own daemon."
        )

    if args.fixcheck_assertions and base_url:
        fix_port, gen_port = _port_of(base_url), _port_of(args.fixcheck_assertions)
        if gen_port and fix_port and gen_port != fix_port:
            problems.append(
                f"OLLAMA_BASE_URL points at port {fix_port} but --fixcheck-assertions "
                f"targets port {gen_port}. The fix generator and FixCheck would use "
                "different daemons -- on a shared node, possibly another job's."
            )

    if base_url:
        try:
            with urllib.request.urlopen(f"{base_url}/api/tags", timeout=10) as resp:
                tags = [m.get("name", "") for m in json.load(resp).get("models", [])]
            wanted = (args.model or "").split("/", 1)[-1]
            if wanted:
                if wanted not in tags and f"{wanted}:latest" not in tags:
                    problems.append(
                        f"the Ollama daemon at {base_url} does not serve {wanted!r}; "
                        f"it has {tags}"
                    )
        except Exception as exc:
            problems.append(f"no Ollama daemon answering at {base_url}: {exc}")

    if args.fixcheck:
        from FixCheckWrapper import FIXCHECK_JAR

        if not os.path.isfile(FIXCHECK_JAR):
            problems.append(
                f"--fixcheck needs the FixCheck jar, missing at {FIXCHECK_JAR}. "
                "Build it with: bash scripts/buildFixcheck.sh"
            )

    try:
        import docker

        docker.from_env().ping()
    except Exception as exc:
        problems.append(f"the Docker daemon is not reachable: {exc}")

    return problems


def warn_if_image_unpatched():
    """Warn when the Defects4J image cannot cache a missing directory layout.

    Defects4J appends newly-determined layouts to
    ``framework/projects/<P>/dir-layout.csv``, which is root-owned and 644 in
    the stock image while our containers run as the host uid. Exactly one bug
    (Chart 26) hits that path and dies ~5 s in with no result, so this is a
    warning rather than an abort: the other 853 are unaffected.
    """
    try:
        import docker

        labels = docker.from_env().images.get(DEFECTS4J_IMAGE).labels or {}
    except Exception:
        return          # image or daemon problems are already reported by preflight
    if labels.get(LAYOUT_WRITABLE_LABEL) != "1":
        print(
            f"[run_project] WARNING: {DEFECTS4J_IMAGE} lacks the layout-writable "
            "patch, so Chart 26 will fail with 'Permission denied' on "
            "dir-layout.csv. Fix it with: bash scripts/patchDefects4jImage.sh",
            file=sys.stderr, flush=True,
        )


# ------------------------------------------------------------------- manifest

def write_manifest(args, bug_ids, log_dir):
    """Record what produced these results.

    ``results/`` and ``scripts/logs/`` are both gitignored, so without this
    nothing ties a number to the code, model and bug list that produced it.
    Copy these files somewhere durable when the campaign ends.
    """
    try:
        git_sha = subprocess.run(
            ["git", "-C", HERE, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        git_sha = ""

    manifest = {
        "project": args.project,
        "bug_ids": bug_ids,
        "bug_count": len(bug_ids),
        "model": args.model,
        "argv": sys.argv,
        "git_sha": git_sha,
        "hostname": socket.gethostname(),
        "slurm_job_id": os.getenv("SLURM_JOB_ID"),
        "ollama_base_url": os.getenv("OLLAMA_BASE_URL"),
        "fixcheck_assertions": args.fixcheck_assertions,
        "fixcheck_prefixes": args.fixcheck_prefixes,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest


# --------------------------------------------------------------- per-bug work

def run_status_path(model_dir, project, bug_id):
    return os.path.join(result_dir(model_dir, project, bug_id), "run_status.json")


def load_run_status(model_dir, project, bug_id):
    try:
        with open(run_status_path(model_dir, project, bug_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def should_run(args, model_dir, bug_id):
    """Whether this bug still needs work, and why not if it doesn't.

    Returns ``(run, reason)``. A bug with a ``result.json`` is done. A bug that
    previously errored or timed out is *also* skipped unless ``--retry-errored``:
    a resubmitted 174-bug Closure job would otherwise burn an hour re-failing
    the same structurally broken checkouts on every attempt.
    """
    if not args.resume:
        return True, None
    if load_result(model_dir, args.project, bug_id) is not None:
        return False, "result.json exists"
    status = load_run_status(model_dir, args.project, bug_id)
    if status and status.get("status") in ("error", "timeout") and not args.retry_errored:
        return False, f"previously {status['status']} (use --retry-errored to redo)"
    return True, None


def process_bug(args, model_dir, bug_id, forwarded, log_dir, index, total):
    """Run one bug and record what happened. Never raises."""
    print(f"\n{'=' * 70}\n[run_project] {args.project} Bug_{bug_id} "
          f"({index}/{total})\n{'=' * 70}", flush=True)

    clean_checkout(args.workdir, args.project, bug_id)
    log_path = os.path.join(log_dir, "bugs", f"{args.project}_{bug_id}.log")
    outcome = run_experiment(
        args.project, bug_id, args.workdir, forwarded,
        timeout=args.timeout, log_path=log_path, kill_grace=args.kill_grace,
    )
    result = load_result(model_dir, args.project, bug_id)

    status = {
        "project": args.project,
        "bug_id": bug_id,
        "status": outcome.status,
        "exit_code": outcome.exit_code,
        "seconds": outcome.seconds,
        "applied": (result or {}).get("applied"),
        # Surfaced in the status stream so a campaign shows non-compiling
        # patches as they happen, instead of them hiding inside "not fixed".
        "compiled_after": (result or {}).get("compiled_after"),
        "triggers_fixed": (result or {}).get("triggers_fixed"),
        "fixed": (result or {}).get("fixed"),
        "fixcheck_suspicious": (result or {}).get("fixcheck_suspicious"),
        "has_result": result is not None,
        "log": log_path,
        "slurm_job_id": os.getenv("SLURM_JOB_ID"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }

    # Written next to Experiment.py's own artifacts, so a resumed run and the
    # campaign summary can both see it without consulting any job's log dir.
    target = run_status_path(model_dir, args.project, bug_id)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    with open(os.path.join(log_dir, "status.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(status) + "\n")

    print(f"[run_project] Bug_{bug_id}: {outcome.status} in {outcome.seconds}s "
          f"(applied={status['applied']} fixed={status['fixed']} "
          f"fixcheck_suspicious={status['fixcheck_suspicious']}) -> {log_path}",
          flush=True)

    # Reclaim the ~400 MB checkout Experiment.py only clears on its next start.
    clean_checkout(args.workdir, args.project, bug_id)
    if outcome.status != "ok":
        reap_containers(args.workdir)
    return status


# -------------------------------------------------------------------- summary

def print_summary(project, statuses, skipped, log_dir):
    counted = len(statuses)
    done = sum(1 for s in statuses if s["status"] == "ok")
    errored = sum(1 for s in statuses if s["status"] == "error")
    timed_out = sum(1 for s in statuses if s["status"] == "timeout")
    applied = sum(1 for s in statuses if s["applied"])
    not_compiled = sum(1 for s in statuses
                       if s["applied"] and s.get("compiled_after") is False)
    triggers = sum(1 for s in statuses if s["triggers_fixed"])
    fixed = sum(1 for s in statuses if s["fixed"])
    suspicious = sum(1 for s in statuses if s["fixcheck_suspicious"])
    seconds = sorted(s["seconds"] for s in statuses) or [0]
    median = seconds[len(seconds) // 2]

    print(f"\n{'=' * 70}\n[run_project] Summary for {project}\n{'=' * 70}")
    print(f"  bugs run:            {counted} (skipped {skipped})")
    print(f"  completed / error / timeout: {done} / {errored} / {timed_out}")
    print(f"  applied:             {applied}/{counted}")
    print(f"    of which never compiled: {not_compiled}")
    print(f"  triggers_fixed:      {triggers}/{counted}")
    print(f"  fixed:               {fixed}/{counted}")
    print(f"  fixcheck_suspicious: {suspicious}/{counted}")
    print(f"  median seconds/bug:  {median}")

    summary = {
        "project": project, "bugs_run": counted, "skipped": skipped,
        "completed": done, "errored": errored, "timed_out": timed_out,
        "applied": applied, "not_compiled": not_compiled,
        "triggers_fixed": triggers, "fixed": fixed,
        "fixcheck_suspicious": suspicious, "median_seconds": median,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "bugs": statuses,
    }
    path = os.path.join(log_dir, "project_summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Written to: {path}")
    return summary


# ----------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(
        description="Run Experiment.py once for every bug of a Defects4J project.",
    )
    parser.add_argument("--project", required=True, help="Defects4J project (e.g. Lang).")
    parser.add_argument(
        "--bug-id", nargs="+", default=None, metavar="BUG",
        help="Bug selection: 'all' (default), ids and inclusive ranges, "
             "comma- or space-separated (e.g. --bug-id 1,3-5 8). Ids are "
             "validated against the project's active bugs.",
    )
    parser.add_argument(
        "--workdir", default="./workspace",
        help="Host directory used as the shared checkout volume (default: ./workspace).",
    )
    parser.add_argument(
        "--log-dir", default=None,
        help="Where per-bug logs and the summary go (default: ./logs/project/<Project>).",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"Per-bug wall-clock limit in seconds (default: {DEFAULT_TIMEOUT}). "
             "Nothing else bounds a run: the Ollama client has no timeout, so a "
             "wedged daemon would otherwise hang the whole job.",
    )
    parser.add_argument(
        "--kill-grace", type=int, default=DEFAULT_KILL_GRACE,
        help=f"Seconds between SIGTERM and SIGKILL on timeout (default: {DEFAULT_KILL_GRACE}).",
    )
    parser.add_argument(
        "--resume", action="store_true", default=True,
        help="Skip bugs that already have a result.json (default).",
    )
    parser.add_argument(
        "--no-resume", dest="resume", action="store_false",
        help="Re-run every selected bug, even those already done.",
    )
    parser.add_argument(
        "--retry-errored", action="store_true",
        help="With --resume, also retry bugs whose previous run errored or timed out.",
    )
    parser.add_argument(
        "--active-bugs-dir", default=None,
        help="Directory holding <Project>/active-bugs.csv, when Defects4J is "
             "neither vendored in ./defects4j nor at $DEFECTS4J_HOME.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the resolved bug list and the commands that would run, then exit.",
    )
    add_experiment_flags(parser)
    args = parser.parse_args()

    from Experiment import DEFAULT_MODEL, model_dir_name

    model_dir = model_dir_name(args.model if args.model is not None else DEFAULT_MODEL)
    log_dir = args.log_dir or os.path.join("logs", "project", args.project)
    forwarded = experiment_args(args)

    try:
        bug_ids = resolve_bug_ids(args.project, args.bug_id, args.active_bugs_dir)
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"[run_project] {exc}")

    if args.dry_run:
        print(f"[run_project] {args.project}: {len(bug_ids)} bug(s)")
        print(f"[run_project] ids: {','.join(bug_ids)}")
        for bug_id in bug_ids:
            print("$ " + " ".join([
                sys.executable, "Experiment.py", "--project", args.project,
                "--bug-id", bug_id, "--workdir", args.workdir, *forwarded,
            ]))
        return

    problems = preflight(args)
    if problems:
        for problem in problems:
            print(f"[run_project] PREFLIGHT: {problem}", file=sys.stderr)
        sys.exit("[run_project] Aborting before spending GPU time.")

    warn_if_image_unpatched()
    _install_signal_handlers()
    os.makedirs(os.path.join(log_dir, "bugs"), exist_ok=True)
    manifest = write_manifest(args, bug_ids, log_dir)
    print(f"[run_project] {args.project}: {len(bug_ids)} bug(s), model={args.model}, "
          f"ollama={manifest['ollama_base_url']}, logs={log_dir}", flush=True)

    statuses, skipped = [], 0
    started = time.time()
    try:
        for index, bug_id in enumerate(bug_ids, start=1):
            if _STOP_REQUESTED:
                print(f"[run_project] Stopping early: {len(bug_ids) - index + 1} "
                      "bug(s) left unrun.", flush=True)
                break
            run, reason = should_run(args, model_dir, bug_id)
            if not run:
                skipped += 1
                print(f"[run_project] {args.project} Bug_{bug_id} "
                      f"({index}/{len(bug_ids)}): skipped -- {reason}", flush=True)
                continue
            statuses.append(
                process_bug(args, model_dir, bug_id, forwarded, log_dir,
                            index, len(bug_ids))
            )
    finally:
        reap_containers(args.workdir)
        print_summary(args.project, statuses, skipped, log_dir)
        print(f"[run_project] Total wall clock: {round(time.time() - started)}s")
        # Only our own per-job mount root, never a shared ./workspace.
        if os.path.basename(os.path.abspath(args.workdir)).startswith("job_"):
            shutil.rmtree(args.workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
