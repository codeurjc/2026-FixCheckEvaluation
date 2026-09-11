"""
inventory.py -- what each run actually left on disk, and whether it is coherent.

`rederive.py` answers "do the artifacts support the recorded verdict?". This
answers the prior question: "are these artifacts from a single run at all?"

They may not be. ``Experiment.py`` creates ``results_dir`` with
``exist_ok=True`` and never clears it, and copies FixCheck's output with
``copytree(..., dirs_exist_ok=True)``, which *merges*. A run that was killed
after writing some artifacts leaves them in place; the retry writes its own
alongside, and the directory ends up holding a mixture that nothing on disk
marks as such. The only trace is that the files were written at different times.

Two signals, both cheap:

* **File census** -- present / absent / zero-byte, per artifact. A zero-byte
  ``raw_response.txt`` next to a positive ``output_tokens`` means the model
  answered and the answer was dropped, not that it stayed silent.
* **mtime spread** -- the wall-clock range across a run's artifacts, ignoring
  files a later repair pass legitimately rewrote. A run takes minutes; a spread
  of hours means two runs.

    python -m audit.inventory
    python -m audit.inventory --max-spread 3600
"""

import argparse
import csv
import json
import os

from audit.rederive import walk

# Artifacts Experiment.py writes for every completed run, in pipeline order.
ARTIFACTS = [
    "prompt.txt",
    "raw_response.txt",
    "fix.diff",
    "apply.log",
    "test_before.log",
    "test_after.log",
    "regression_test.log",
    "issue.txt",
    "result.json",
    "run_status.json",
]

# Written by a later repair pass, not by the run, so their mtime says nothing
# about whether the run's own artifacts are coherent.
REWRITTEN_LATER = {"result.json", "run_status.json"}

# The artifacts are written in two bursts, not continuously: FixGenerator
# writes its three as soon as the model answers (FixGenerator.py:401-403), and
# Experiment.py writes the rest in one block at the end of the run
# (Experiment.py:952-958). The gap *between* the groups is just how long the
# apply/compile/test phase took -- legitimately over an hour on Chart -- so it
# is no evidence of anything. Only a spread *within* one group is: those files
# are written milliseconds apart by the same loop, so a gap there means they
# came from different runs.
WRITE_GROUPS = {
    "generation": ["prompt.txt", "raw_response.txt", "fix.diff"],
    "evaluation": ["apply.log", "test_before.log", "test_after.log",
                   "regression_test.log", "issue.txt"],
}

# Generous: the block above is a handful of small writes.
DEFAULT_MAX_SPREAD_SECONDS = 60


def run_inventory(run_dir, max_spread=DEFAULT_MAX_SPREAD_SECONDS):
    """Census and coherence of one run directory."""
    files, mtimes = {}, {}
    for name in ARTIFACTS:
        path = os.path.join(run_dir, name)
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            files[name] = "absent"
            continue
        files[name] = "empty" if size == 0 else "present"
        if name not in REWRITTEN_LATER:
            mtimes[name] = os.path.getmtime(path)

    fixcheck_dir = os.path.join(run_dir, "fixcheck")
    has_fixcheck_dir = os.path.isdir(fixcheck_dir)

    group_spreads = {}
    for group, names in WRITE_GROUPS.items():
        stamps = [mtimes[n] for n in names if n in mtimes]
        group_spreads[group] = round(max(stamps) - min(stamps), 1) \
            if len(stamps) > 1 else 0.0

    worst = max(group_spreads.values()) if group_spreads else 0.0
    return {
        "files": files,
        "group_spreads": group_spreads,
        "worst_group_spread_seconds": worst,
        "mixed_attempts": worst > max_spread,
        "run_span_seconds": round(max(mtimes.values()) - min(mtimes.values()), 1)
        if len(mtimes) > 1 else 0.0,
        "has_fixcheck_dir": has_fixcheck_dir,
    }


def coherence_flags(run_dir, inv, result):
    """Contradictions between what is on disk and what the run recorded."""
    flags = []
    files = inv["files"]
    usage = (result or {}).get("usage_metadata") or {}
    fixcheck = (result or {}).get("fixcheck")

    if files["raw_response.txt"] == "empty" and (usage.get("output_tokens") or 0) > 0:
        # The model generated tokens and none of them reached the file: the
        # response was read from a field the client does not look at, or it was
        # cut off. Recorded downstream as "the model failed to fix this bug".
        flags.append("response_lost")

    if inv["mixed_attempts"]:
        flags.append("mixed_attempts")

    if inv["has_fixcheck_dir"] and not fixcheck:
        # FixCheck artifacts with no FixCheck record: they belong to a previous
        # attempt that this directory was reused for.
        flags.append("orphan_fixcheck_dir")
    if fixcheck and fixcheck.get("analyzed_test_classes", 0) > 0 \
            and not inv["has_fixcheck_dir"]:
        flags.append("missing_fixcheck_dir")

    if files["result.json"] == "absent" and files["prompt.txt"] == "present":
        flags.append("killed_after_generation")

    return flags


def main():
    parser = argparse.ArgumentParser(
        description="Census the campaign's artifacts and flag incoherent runs.",
    )
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--model", nargs="+", default=None)
    parser.add_argument("--csv", default="audit/inventory.csv")
    parser.add_argument("--max-spread", type=float,
                        default=DEFAULT_MAX_SPREAD_SECONDS,
                        help="mtime range (s) above which a run directory is "
                             "considered to hold more than one attempt")
    args = parser.parse_args()

    rows, census, flag_counts = [], {}, {}
    for model, project, bug_id, run_dir in walk(args.results_dir, args.model):
        inv = run_inventory(run_dir, args.max_spread)
        try:
            with open(os.path.join(run_dir, "result.json"), encoding="utf-8") as f:
                result = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            result = None
        flags = coherence_flags(run_dir, inv, result)
        for name, state in inv["files"].items():
            census.setdefault(model, {}).setdefault(name, {"present": 0,
                                                          "empty": 0,
                                                          "absent": 0})
            census[model][name][state] += 1
        for flag in flags:
            flag_counts.setdefault(flag, []).append(f"{model}/{project}/Bug_{bug_id}")
        rows.append({
            "model": model, "project": project, "bug_id": bug_id,
            "worst_group_spread_seconds": inv["worst_group_spread_seconds"],
            "generation_spread": inv["group_spreads"].get("generation"),
            "evaluation_spread": inv["group_spreads"].get("evaluation"),
            "run_span_seconds": inv["run_span_seconds"],
            "has_fixcheck_dir": inv["has_fixcheck_dir"],
            "flags": ",".join(flags),
            **{f"file_{k}": v for k, v in inv["files"].items()},
        })

    os.makedirs(os.path.dirname(args.csv) or ".", exist_ok=True)
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"[inventory] {len(rows)} run(s)\n")
    for model, per_file in census.items():
        print(f"  {model}")
        print(f"    {'artifact':22}{'present':>9}{'empty':>8}{'absent':>8}")
        for name in ARTIFACTS:
            counts = per_file[name]
            print(f"    {name:22}{counts['present']:>9}{counts['empty']:>8}"
                  f"{counts['absent']:>8}")
        print()

    print("Coherence flags:")
    if not flag_counts:
        print("  none")
    for flag, runs in sorted(flag_counts.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  {flag}: {len(runs)} run(s)")
        for run in runs[:8]:
            print(f"      {run}")
        if len(runs) > 8:
            print(f"      ... and {len(runs) - 8} more")
    print(f"\n[inventory] CSV written to {args.csv}")


if __name__ == "__main__":
    main()
