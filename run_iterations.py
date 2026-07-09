"""
run_iterations.py — Run Experiment.py several times for a single Defects4J bug.

LLM fix generation is non-deterministic: even at ``temperature=0`` a large
MoE model served locally (e.g. ``gpt-oss:120b``) can produce a different patch
on each call because of expert-routing/batching and non-associative GPU
floating-point reductions. A single run is therefore not a reliable signal —
the same bug may be fixed one run and missed the next.

This script runs ``Experiment.py`` N times for one ``--project``/``--bug-id``
and stores each run's artifacts under
``results/<model>/<project>/Bug_<bug>/<iteration>/`` so the runs can be told
apart. To keep runs from contaminating each other, the per-bug checkout inside
``--workdir`` is deleted before every run (and once at the end). Finally it
aggregates each run's ``result.json`` into a per-bug
``results/<model>/<project>/Bug_<bug>/summary.json`` and prints how many runs
fixed the bug.

It mirrors Experiment.py's fix-generation flags (``--model``, ``--temperature``,
``--include-test-code``, ``--include-test-log``, ``--include-issue``) and
forwards them to every run, so they behave exactly as they do there:

    python run_iterations.py --project Lang --bug-id 1
    python run_iterations.py --project Lang --bug-id 1 --iterations 10 \
        --model ollama/gpt-oss:120b --include-test-code
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXPERIMENT = os.path.join(HERE, "Experiment.py")

sys.path.insert(0, HERE)
from Experiment import DEFAULT_MODEL, model_dir_name  # noqa: E402


def clean_checkout(workdir, project, bug_id):
    """Remove the per-bug checkout so the next run starts from a clean slate.

    ``Experiment.py`` checks out into ``<workdir>/<project>_<bug_id>``; deleting
    it between runs guarantees no leftover (possibly half-patched) sources leak
    from one iteration into the next.
    """
    checkout = os.path.join(os.path.abspath(workdir), f"{project}_{bug_id}")
    if os.path.isdir(checkout):
        shutil.rmtree(checkout)


def experiment_args(args):
    """Translate this runner's parsed args into Experiment.py CLI flags.

    Only options the user actually set are forwarded, so Experiment.py keeps
    ownership of the defaults (model, temperature) — there is no second copy of
    them to drift out of sync here.
    """
    forwarded = []
    if args.model is not None:
        forwarded += ["--model", args.model]
    if args.temperature is not None:
        forwarded += ["--temperature", str(args.temperature)]
    if args.include_test_code:
        forwarded.append("--include-test-code")
    if args.include_test_log:
        forwarded.append("--include-test-log")
    if args.include_issue:
        forwarded.append("--include-issue")
    return forwarded


def run_once(project, bug_id, workdir, iteration, forwarded):
    """Invoke Experiment.py for a single iteration; return its exit code."""
    cmd = [
        sys.executable, EXPERIMENT,
        "--project", project,
        "--bug-id", str(bug_id),
        "--workdir", workdir,
        "--iteration", str(iteration),
        *forwarded,
    ]
    print(f"\n{'=' * 70}\n[run_iterations] Iteration {iteration}\n{'=' * 70}")
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd).returncode


def load_result(model_dir, project, bug_id, iteration):
    """Load an iteration's result.json, or None if it is missing/unreadable."""
    path = os.path.join(
        "results", model_dir, project, f"Bug_{bug_id}", str(iteration), "result.json"
    )
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Run Experiment.py N times for one Defects4J bug to deal "
                    "with LLM non-determinism.",
    )
    parser.add_argument("--project", required=True, help="Defects4J project name (e.g. Lang).")
    parser.add_argument("--bug-id", required=True, help="Numeric bug id (e.g. 1).")
    parser.add_argument(
        "--workdir", default="./workspace",
        help="Host directory used as the shared checkout volume (default: ./workspace).",
    )
    parser.add_argument(
        "--iterations", type=int, default=5,
        help="Number of times to run the experiment (default: 5).",
    )
    # --- Flags mirrored from Experiment.py and forwarded to each run. ---
    # Defaults live in Experiment.py; here they default to None/off so only
    # user-supplied values are passed through.
    parser.add_argument(
        "--model", default=None,
        help="LLM model identifier (default: Experiment.py's default).",
    )
    parser.add_argument(
        "--temperature", type=float, default=None,
        help="LLM sampling temperature (default: Experiment.py's default).",
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
    args = parser.parse_args()

    project, bug_id = args.project, str(args.bug_id)
    model_dir = model_dir_name(args.model if args.model is not None else DEFAULT_MODEL)
    forwarded = experiment_args(args)
    runs = []
    for i in range(1, args.iterations + 1):
        clean_checkout(args.workdir, project, bug_id)
        rc = run_once(project, bug_id, args.workdir, i, forwarded)
        runs.append(
            {
                "iteration": i,
                "exit_code": rc,
                "result": load_result(model_dir, project, bug_id, i),
            }
        )
    # Leave no stale checkout behind after the final run.
    clean_checkout(args.workdir, project, bug_id)

    # ---- Aggregate & report ------------------------------------------------
    def flag(run, key):
        return bool(run["result"] and run["result"].get(key))

    fixed = sum(flag(r, "fixed") for r in runs)
    triggers = sum(flag(r, "triggers_fixed") for r in runs)
    applied = sum(flag(r, "applied") for r in runs)

    print(f"\n{'=' * 70}\n[run_iterations] Summary for {project} {bug_id}\n{'=' * 70}")
    for r in runs:
        res = r["result"]
        if res is None:
            print(f"  iter {r['iteration']}: no result.json (exit {r['exit_code']})")
        else:
            print(
                f"  iter {r['iteration']}: applied={res.get('applied')} "
                f"triggers_fixed={res.get('triggers_fixed')} fixed={res.get('fixed')}"
            )
    print(f"\n  applied:        {applied}/{args.iterations}")
    print(f"  triggers_fixed: {triggers}/{args.iterations}")
    print(f"  fixed:          {fixed}/{args.iterations}")

    summary = {
        "project": project,
        "bug_id": bug_id,
        "iterations": args.iterations,
        "applied": applied,
        "triggers_fixed": triggers,
        "fixed": fixed,
        "runs": [
            {
                "iteration": r["iteration"],
                "exit_code": r["exit_code"],
                "applied": (r["result"] or {}).get("applied"),
                "triggers_fixed": (r["result"] or {}).get("triggers_fixed"),
                "fixed": (r["result"] or {}).get("fixed"),
            }
            for r in runs
        ],
    }
    summary_dir = os.path.join("results", model_dir, project, f"Bug_{bug_id}")
    os.makedirs(summary_dir, exist_ok=True)
    summary_path = os.path.join(summary_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Aggregate written to: {summary_path}")


if __name__ == "__main__":
    main()
