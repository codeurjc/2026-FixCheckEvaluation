"""
run_iterations.py — Run Experiment.py several times for one or more Defects4J bugs.

LLM fix generation is non-deterministic: even at ``temperature=0`` a large
MoE model served locally (e.g. ``gpt-oss:120b``) can produce a different patch
on each call because of expert-routing/batching and non-associative GPU
floating-point reductions. A single run is therefore not a reliable signal —
the same bug may be fixed one run and missed the next.

This script runs ``Experiment.py`` N times (``--iterations``) for each bug in
``--bug-id`` and stores each run's artifacts under
``results/<model>/<project>/Bug_<bug>/<iteration>/`` so the runs can be told
apart. ``--bug-id`` accepts several ids and inclusive ranges, e.g.
``--bug-id 1-5 8`` expands to bugs 1, 2, 3, 4, 5, 8.

Iterations that are already done are skipped: if an iteration's ``result.json``
already exists it is reused as-is (marked ``skipped``) and ``Experiment.py`` is
not invoked again, so an interrupted-and-resubmitted run only computes the
outstanding work. To keep runs from contaminating each other, the per-bug
checkout inside ``--workdir`` is deleted before every (non-skipped) run and once
after each bug. Finally it aggregates each bug's runs into a per-bug
``results/<model>/<project>/Bug_<bug>/summary.json`` and prints, per bug and in
total, how many runs fixed the bug.

It mirrors Experiment.py's fix-generation flags (``--model``, ``--temperature``,
``--include-test-code``, ``--include-test-log``, ``--include-issue``, and the
``--fixcheck``/``--fixcheck-*`` overfitting-check flags) and forwards them to
every run, so they behave exactly as they do there:

    python run_iterations.py --project Lang --bug-id 1
    python run_iterations.py --project Lang --bug-id 1-5 8 --iterations 10 \
        --model ollama/gpt-oss:120b --include-test-code
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXPERIMENT = os.path.join(HERE, "Experiment.py")

sys.path.insert(0, HERE)
from Experiment import DEFAULT_MODEL, model_dir_name  # noqa: E402
from experiment_runner import (  # noqa: E402
    add_experiment_flags,
    clean_checkout,
    experiment_args,
    load_result,
    parse_bug_ids,
)


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


def process_bug(project, bug_id, model_dir, iterations, workdir, forwarded):
    """Run (or reuse) every iteration of one bug and write its summary.json.

    Any iteration whose ``result.json`` already exists is reused as-is and
    marked ``skipped`` instead of re-running ``Experiment.py``. Returns the
    per-bug aggregate dict (also written to disk) for the caller's global report.
    """
    runs = []
    for i in range(1, iterations + 1):
        existing = load_result(model_dir, project, bug_id, i)
        if existing is not None:
            print(
                f"\n{'=' * 70}\n[run_iterations] {project} Bug_{bug_id} "
                f"iteration {i}: skipped (result.json exists)\n{'=' * 70}"
            )
            runs.append({"iteration": i, "skipped": True, "exit_code": None, "result": existing})
            continue
        clean_checkout(workdir, project, bug_id)
        rc = run_once(project, bug_id, workdir, i, forwarded)
        runs.append(
            {
                "iteration": i,
                "skipped": False,
                "exit_code": rc,
                "result": load_result(model_dir, project, bug_id, i),
            }
        )
    # Leave no stale checkout behind after this bug's final run.
    clean_checkout(workdir, project, bug_id)

    def flag(run, key):
        return bool(run["result"] and run["result"].get(key))

    fixed = sum(flag(r, "fixed") for r in runs)
    triggers = sum(flag(r, "triggers_fixed") for r in runs)
    applied = sum(flag(r, "applied") for r in runs)
    fixcheck_suspicious = sum(flag(r, "fixcheck_suspicious") for r in runs)
    skipped = sum(r["skipped"] for r in runs)

    print(f"\n{'=' * 70}\n[run_iterations] Summary for {project} Bug_{bug_id}\n{'=' * 70}")
    for r in runs:
        res = r["result"]
        tag = " (skipped)" if r["skipped"] else ""
        if res is None:
            print(f"  iter {r['iteration']}: no result.json (exit {r['exit_code']})")
        else:
            print(
                f"  iter {r['iteration']}: applied={res.get('applied')} "
                f"triggers_fixed={res.get('triggers_fixed')} fixed={res.get('fixed')} "
                f"fixcheck_suspicious={res.get('fixcheck_suspicious')}{tag}"
            )
    print(f"\n  applied:             {applied}/{iterations}")
    print(f"  triggers_fixed:      {triggers}/{iterations}")
    print(f"  fixed:               {fixed}/{iterations}")
    print(f"  fixcheck_suspicious: {fixcheck_suspicious}/{iterations}")
    print(f"  skipped:             {skipped}/{iterations}")

    summary = {
        "project": project,
        "bug_id": bug_id,
        "iterations": iterations,
        "applied": applied,
        "triggers_fixed": triggers,
        "fixed": fixed,
        "fixcheck_suspicious": fixcheck_suspicious,
        "skipped": skipped,
        "runs": [
            {
                "iteration": r["iteration"],
                "skipped": r["skipped"],
                "exit_code": r["exit_code"],
                "applied": (r["result"] or {}).get("applied"),
                "triggers_fixed": (r["result"] or {}).get("triggers_fixed"),
                "fixed": (r["result"] or {}).get("fixed"),
                "fixcheck_suspicious": (r["result"] or {}).get("fixcheck_suspicious"),
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
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Run Experiment.py N times per Defects4J bug to deal with "
                    "LLM non-determinism; skips iterations already done.",
    )
    parser.add_argument("--project", required=True, help="Defects4J project name (e.g. Lang).")
    parser.add_argument(
        "--bug-id", required=True, nargs="+", metavar="BUG",
        help="One or more numeric bug ids; inclusive ranges are expanded "
             "(e.g. --bug-id 1-5 8 runs bugs 1, 2, 3, 4, 5 and 8).",
    )
    parser.add_argument(
        "--workdir", default="./workspace",
        help="Host directory used as the shared checkout volume (default: ./workspace).",
    )
    parser.add_argument(
        "--iterations", type=int, default=5,
        help="Number of times to run the experiment (default: 5).",
    )
    # The fix/FixCheck flags are registered from experiment_runner so this
    # runner and run_project.py cannot drift apart; they default to None/off
    # there, so only user-supplied values reach Experiment.py.
    add_experiment_flags(parser)
    args = parser.parse_args()

    project = args.project
    bug_ids = parse_bug_ids(args.bug_id)
    model_dir = model_dir_name(args.model if args.model is not None else DEFAULT_MODEL)
    forwarded = experiment_args(args)

    summaries = [
        process_bug(project, bug_id, model_dir, args.iterations, args.workdir, forwarded)
        for bug_id in bug_ids
    ]

    # ---- Global report across all bugs -------------------------------------
    print(f"\n{'=' * 70}\n[run_iterations] Global summary for {project} "
          f"({len(bug_ids)} bug(s), {args.iterations} iteration(s) each)\n{'=' * 70}")
    for s in summaries:
        print(
            f"  Bug_{s['bug_id']}: fixed={s['fixed']}/{s['iterations']} "
            f"triggers_fixed={s['triggers_fixed']}/{s['iterations']} "
            f"applied={s['applied']}/{s['iterations']} "
            f"fixcheck_suspicious={s['fixcheck_suspicious']}/{s['iterations']} "
            f"skipped={s['skipped']}/{s['iterations']}"
        )
    total = args.iterations * len(bug_ids)
    print(
        f"\n  TOTAL: fixed={sum(s['fixed'] for s in summaries)}/{total} "
        f"triggers_fixed={sum(s['triggers_fixed'] for s in summaries)}/{total} "
        f"applied={sum(s['applied'] for s in summaries)}/{total} "
        f"fixcheck_suspicious={sum(s['fixcheck_suspicious'] for s in summaries)}/{total} "
        f"skipped={sum(s['skipped'] for s in summaries)}/{total}"
    )


if __name__ == "__main__":
    main()
