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

from d4j.defects4j_bugs import PROJECT_BUG_COUNTS, PROJECTS


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
        usage = (result or {}).get("usage_metadata") or {}
        fixcheck = (result or {}).get("fixcheck") or {}
        # A patch that applied but never compiled makes `defects4j test` print
        # no "Failing tests:" line, so failing_tests_after is -1 and the parsed
        # failure list is empty -- which older runs scored as a perfect fix.
        # Recomputed here rather than trusted from the file so results written
        # before the Experiment.py guard are corrected too; `compiled_after` is
        # authoritative when present.
        applied = bool((result or {}).get("applied"))
        compiled_after = (result or {}).get("compiled_after")
        if compiled_after is None:
            compiled_after = applied and (result or {}).get("failing_tests_after", -1) != -1
        compiled_after = bool(compiled_after)
        triggers_fixed = bool((result or {}).get("triggers_fixed")) and compiled_after
        fixed = bool((result or {}).get("fixed")) and compiled_after
        records.append({
            "project": project,
            "bug_id": bug_id,
            "status": (status or {}).get("status", "ok" if result else "unknown"),
            "exit_code": (status or {}).get("exit_code"),
            "seconds": (status or {}).get("seconds"),
            "has_result": result is not None,
            "applied": applied,
            "compiled_after": compiled_after,
            "triggers_fixed": triggers_fixed,
            "fixed": fixed,
            # What the run recorded before the non-compiling-patch guard, so the
            # size of the correction stays auditable instead of silently applied.
            # Prefer the value preserved by scripts/backfill_compiled_after.py:
            # once the file has been repaired, its `fixed` field is the corrected
            # one and would otherwise report the correction as zero.
            "fixed_as_recorded": bool(
                (result or {}).get("fixed_as_recorded", (result or {}).get("fixed"))
            ),
            "new_failures": len((result or {}).get("new_failures") or []),
            # Three different things, kept apart because they were conflated:
            #   invoked -- Experiment.py called FixCheck at all
            #   ok      -- FixCheck got as far as running (its own `ok` flag is
            #              False when it aborted: no daemon, exports empty, or
            #              `defects4j compile` failed -- which is the case for
            #              all 203 non-compiling patches, since FixCheck was
            #              gated on the pre-guard triggers_fixed)
            #   ran     -- both of the above
            # `bool(fixcheck)` alone counted those 203 as FixCheck runs, which
            # is why the CLI table and the notebook disagreed by exactly 203.
            "fixcheck_invoked": bool(fixcheck),
            "fixcheck_ok": bool(fixcheck) and bool(fixcheck.get("ok")),
            "fixcheck_ran": bool(fixcheck) and bool(fixcheck.get("ok")),
            # analyzed_test_classes > 0 is what separates a real "not
            # suspicious" from a vacuous one (nothing was analyzed at all) --
            # the analysis must never lump the two together. None, not 0, when
            # FixCheck never ran: a measured zero and an absent measurement
            # must not average together.
            "fixcheck_analyzed": fixcheck.get("analyzed_test_classes")
            if fixcheck else None,
            # The two inputs to the suspicious verdict, kept separately: most
            # analysed runs *do* have a failing variation, and it is the
            # similarity score that decides. Collapsing them into the boolean
            # hides where the detection power actually goes.
            "failing_prefixes": fixcheck.get("failing_prefixes") if fixcheck else None,
            "max_failure_similarity": fixcheck.get("max_failure_similarity")
            if fixcheck else None,
            "fixcheck_suspicious": bool((result or {}).get("fixcheck_suspicious")),
            "included_issue": bool((result or {}).get("included_issue")),
            "issue_status": (result or {}).get("issue_status"),
            # LLM generation time only; "seconds" above is the whole run's
            # wall clock including checkout/compile/tests.
            "llm_seconds": (result or {}).get("elapsed_seconds"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
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
        # Anything that is neither ok, error nor timeout. Counted in
        # `attempted` but in none of the outcome columns, so without this it
        # would silently break the reconciliation attempted = ok + errored +
        # timed_out.
        "unknown_status": sum(
            1 for r in records
            if r["status"] not in ("ok", "error", "timeout")
        ),
        "applied": sum(1 for r in records if r["applied"]),
        "not_compiled": sum(1 for r in records if r["applied"] and not r["compiled_after"]),
        "triggers_fixed": sum(1 for r in records if r["triggers_fixed"]),
        "fixed": sum(1 for r in records if r["fixed"]),
        # Invoked vs actually ran. The gap is FixCheck aborting before it could
        # analyse anything -- 203 times over the campaign, all of them patches
        # that did not compile.
        "fixcheck_invoked": sum(1 for r in records if r["fixcheck_invoked"]),
        "fixcheck_ran": sum(1 for r in records if r["fixcheck_ran"]),
        "fixcheck_analyzed": sum(
            1 for r in records if (r["fixcheck_analyzed"] or 0) > 0
        ),
        "fixcheck_suspicious": sum(1 for r in records if r["fixcheck_suspicious"]),
        "median_seconds": _percentile(seconds, 0.5),
        "p90_seconds": _percentile(seconds, 0.9),
    }


COLUMNS = [
    "project", "bugs_total", "attempted", "no_result", "errored", "timed_out",
    "unknown_status",
    "applied", "not_compiled", "triggers_fixed", "fixed",
    "fixcheck_invoked", "fixcheck_ran", "fixcheck_analyzed",
    "fixcheck_suspicious",
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
