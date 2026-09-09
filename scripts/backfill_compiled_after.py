"""
backfill_compiled_after.py — repair results written before the non-compiling
patch guard.

`defects4j test` prints no ``Failing tests:`` line when the patched sources
fail to compile, so the parsed failure list came back empty and the old
``evaluate_fix`` scored the patch as a perfect ``fixed``. 203 runs of the
campaign are in that state (see docs/campaign.md).

``Experiment.py`` now records ``compiled_after`` and refuses to call such a run
fixed, and ``summarize_campaign.collect_project`` re-derives the flag when
reading older results — but the files on disk still said ``fixed: true``, so
anything reading them directly (``jq``, ``run_project.py``'s own summary,
``run_iterations.py``) kept seeing the inflated number. This makes the
artifacts agree with the analysis.

For every ``result.json`` (and its sibling ``run_status.json``):

- add ``compiled_after`` (``applied and failing_tests_after != -1``);
- where it is False and the run claimed success, set ``fixed`` and
  ``triggers_fixed`` to False, preserving the originals as
  ``fixed_as_recorded`` / ``triggers_fixed_as_recorded``.

Idempotent — a file that already carries ``compiled_after`` is left alone — and
a dry run by default:

    python scripts/backfill_compiled_after.py                  # report only
    python scripts/backfill_compiled_after.py --apply          # rewrite
    python scripts/backfill_compiled_after.py --apply --model qwen3.6:35b
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def compiled_after(result):
    """Whether the post-fix test run produced anything to judge by."""
    if not result.get("applied"):
        return False
    return result.get("failing_tests_after", -1) != -1


def plan_for(result):
    """The fields to change in a ``result.json``, or ``{}`` if it is already right."""
    if "compiled_after" in result:
        return {}                       # already backfilled; stay idempotent
    ok = compiled_after(result)
    changes = {"compiled_after": ok}
    if not ok:
        # Only record the "as recorded" values when they were actually wrong,
        # so the audit trail marks real corrections rather than every file.
        if result.get("fixed"):
            changes["fixed_as_recorded"] = True
            changes["fixed"] = False
        if result.get("triggers_fixed"):
            changes["triggers_fixed_as_recorded"] = True
            changes["triggers_fixed"] = False
    return changes


def rewrite(path, changes):
    """Merge ``changes`` into the JSON at ``path``, preserving key order."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.update(changes)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)               # atomic: never leave a half-written result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--results-dir", default=os.path.join(HERE, "results"))
    parser.add_argument("--model", default=None, help="Limit to one model directory.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually rewrite the files (default: report only).")
    args = parser.parse_args()

    models = ([args.model] if args.model else
              sorted(d for d in os.listdir(args.results_dir)
                     if os.path.isdir(os.path.join(args.results_dir, d)) and d != "old"))

    total = flagged = already = 0
    per_model = {}
    for model in models:
        corrected = seen = skipped = 0
        for path in sorted(glob.glob(
                os.path.join(args.results_dir, model, "*", "Bug_*", "result.json"))):
            seen += 1
            with open(path, encoding="utf-8") as f:
                result = json.load(f)
            changes = plan_for(result)
            if not changes:
                skipped += 1
                continue
            if changes.get("fixed_as_recorded"):
                corrected += 1
            if args.apply:
                rewrite(path, changes)
                status_path = os.path.join(os.path.dirname(path), "run_status.json")
                if os.path.exists(status_path):
                    # run_status.json mirrors the outcome flags for the resume
                    # logic and the per-job summary; keep the two in step.
                    rewrite(status_path, {
                        k: v for k, v in changes.items()
                        if k in ("compiled_after", "fixed", "triggers_fixed",
                                 "fixed_as_recorded", "triggers_fixed_as_recorded")
                    })
        total += seen
        flagged += corrected
        already += skipped
        per_model[model] = (seen, corrected, skipped)

    verb = "corrected" if args.apply else "would correct"
    for model, (seen, corrected, skipped) in per_model.items():
        print(f"{model:16} {seen:>5} runs | {verb} {corrected:>4} "
              f"| already backfilled {skipped:>4}")
    print(f"\n{verb} {flagged} run(s) that claimed a fix without compiling.")
    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
