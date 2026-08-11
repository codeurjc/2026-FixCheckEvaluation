"""
summarize_campaign.py — aggregate a whole benchmark campaign from ``results/``.

Each SLURM job writes its own ``project_summary.json``, but those are only ever
a partial view: a project can be split across several jobs (``--chunks``), a
job can be requeued, and stragglers get re-submitted later. ``results/`` is the
one place that accumulates every run regardless, so that is what this reads.

Two numbers here drive decisions rather than just describing the past. The
median and p90 seconds per bug, measured on the pilot projects, are what size
``--minutes-per-bug`` for the full launch; and ``--list-failures`` is the input
to the re-run pass.

    python summarize_campaign.py
    python summarize_campaign.py --model ollama/gpt-oss:120b --list-failures
    python summarize_campaign.py --csv campaign.csv
"""

import argparse
import csv
import json
import os
import sys

from defects4j_bugs import PROJECT_BUG_COUNTS, PROJECTS


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def collect_project(results_root, project):
    """Every attempted bug of one project, newest artifacts on disk.

    A bug counts as *attempted* when it has either a ``result.json`` (the run
    reached the end) or a ``run_status.json`` (the runner recorded an error or
    a timeout). A bug with neither was never started.
    """
    project_dir = os.path.join(results_root, project)
    if not os.path.isdir(project_dir):
        return []
    records = []
    for entry in sorted(os.listdir(project_dir)):
        if not entry.startswith("Bug_"):
            continue
        bug_id = entry[len("Bug_"):]
        bug_dir = os.path.join(project_dir, entry)
        result = _read_json(os.path.join(bug_dir, "result.json"))
        status = _read_json(os.path.join(bug_dir, "run_status.json"))
        if result is None and status is None:
            continue
        records.append({
            "project": project,
            "bug_id": bug_id,
            "status": (status or {}).get("status", "ok" if result else "unknown"),
            "exit_code": (status or {}).get("exit_code"),
            "seconds": (status or {}).get("seconds"),
            "has_result": result is not None,
            "applied": bool((result or {}).get("applied")),
            "triggers_fixed": bool((result or {}).get("triggers_fixed")),
            "fixed": bool((result or {}).get("fixed")),
            "fixcheck_ran": bool((result or {}).get("fixcheck")),
            "fixcheck_suspicious": bool((result or {}).get("fixcheck_suspicious")),
            "included_issue": bool((result or {}).get("included_issue")),
        })
    return records


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def aggregate(records, project):
    """Roll a project's per-bug records into one row."""
    seconds = [r["seconds"] for r in records if isinstance(r["seconds"], (int, float))]
    return {
        "project": project,
        "bugs_total": PROJECT_BUG_COUNTS.get(project, len(records)),
        "attempted": len(records),
        "no_result": sum(1 for r in records if not r["has_result"]),
        "errored": sum(1 for r in records if r["status"] == "error"),
        "timed_out": sum(1 for r in records if r["status"] == "timeout"),
        "applied": sum(1 for r in records if r["applied"]),
        "triggers_fixed": sum(1 for r in records if r["triggers_fixed"]),
        "fixed": sum(1 for r in records if r["fixed"]),
        "fixcheck_ran": sum(1 for r in records if r["fixcheck_ran"]),
        "fixcheck_suspicious": sum(1 for r in records if r["fixcheck_suspicious"]),
        "median_seconds": _percentile(seconds, 0.5),
        "p90_seconds": _percentile(seconds, 0.9),
    }


COLUMNS = [
    "project", "bugs_total", "attempted", "no_result", "errored", "timed_out",
    "applied", "triggers_fixed", "fixed", "fixcheck_ran", "fixcheck_suspicious",
    "median_seconds", "p90_seconds",
]


def print_table(rows):
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in COLUMNS}
    print("  ".join(c.ljust(widths[c]) for c in COLUMNS))
    print("  ".join("-" * widths[c] for c in COLUMNS))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in COLUMNS))


def main():
    parser = argparse.ArgumentParser(
        description="Aggregate a Defects4J campaign from the results/ tree.",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model identifier whose results to read (e.g. ollama/gpt-oss:120b). "
             "Defaults to the only subdirectory of results/ when there is just one.",
    )
    parser.add_argument(
        "--results-dir", default="results",
        help="Root of the results tree (default: results).",
    )
    parser.add_argument(
        "--project", nargs="+", default=None,
        help="Limit to these projects (default: all 17).",
    )
    parser.add_argument("--csv", default=None, help="Also write the per-project table here.")
    parser.add_argument("--json", default=None, help="Also write the full per-bug data here.")
    parser.add_argument(
        "--list-failures", action="store_true",
        help="List the bugs that errored, timed out or produced no result.json.",
    )
    args = parser.parse_args()

    from Experiment import model_dir_name

    if args.model:
        model_dir = model_dir_name(args.model)
    else:
        candidates = [
            d for d in sorted(os.listdir(args.results_dir))
            if os.path.isdir(os.path.join(args.results_dir, d))
        ] if os.path.isdir(args.results_dir) else []
        # 'old' is where previous, unrelated runs were parked; never pick it blindly.
        candidates = [c for c in candidates if c != "old"]
        if len(candidates) != 1:
            sys.exit(
                f"[summarize] pass --model: {args.results_dir}/ holds {candidates or 'nothing'}"
            )
        model_dir = candidates[0]

    results_root = os.path.join(args.results_dir, model_dir)
    projects = args.project or list(PROJECTS)

    rows, all_records = [], []
    for project in projects:
        records = collect_project(results_root, project)
        if not records:
            continue
        all_records.extend(records)
        rows.append(aggregate(records, project))

    if not rows:
        sys.exit(f"[summarize] no results under {results_root}/")

    total = {c: sum(r[c] for r in rows) for c in COLUMNS[1:-2]}
    every_second = [r["seconds"] for r in all_records if isinstance(r["seconds"], (int, float))]
    total.update({
        "project": "TOTAL",
        "median_seconds": _percentile(every_second, 0.5),
        "p90_seconds": _percentile(every_second, 0.9),
    })

    print(f"[summarize] model={model_dir}  results={results_root}\n")
    print_table(rows + [total])

    if args.list_failures:
        failures = [
            r for r in all_records
            if r["status"] in ("error", "timeout") or not r["has_result"]
        ]
        print(f"\n[summarize] {len(failures)} bug(s) needing attention:")
        for r in failures:
            print(f"  {r['project']:16} Bug_{r['bug_id']:<5} {r['status']:8} "
                  f"exit={r['exit_code']}")
        if failures:
            print("\n  Re-run with, e.g.:")
            for project in sorted({r["project"] for r in failures}):
                ids = ",".join(r["bug_id"] for r in failures if r["project"] == project)
                print(f"    ./scripts/runCampaign.sh --projects {project} "
                      f"--bug-id {ids} --retry-errored")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows + [total])
        print(f"\n[summarize] CSV written to {args.csv}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"model": model_dir, "projects": rows, "bugs": all_records}, f, indent=2)
        print(f"[summarize] JSON written to {args.json}")


if __name__ == "__main__":
    main()
