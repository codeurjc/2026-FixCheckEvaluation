"""
apply_first_verdict_rule.py -- restore the verdict of record for runs that were
re-rolled, and record the one run whose verdict was never written.

The rule (docs/campaign.md, "The verdict of record"):

    The first run whose patch was generated and evaluated is the run of record.
    Only runs the harness broke *before* producing a verdict are re-run.

Re-running a run that already has a verdict does not re-measure it: the model
is not deterministic even at temperature 0, so it draws a new patch. And the
re-runs that broke the rule on 2026-09-10 were all selected *because* they had
succeeded -- FixCheck only runs on plausible patches -- so the re-roll could
only lower a model's count, never raise it. It did: 3 of 8 flipped from fixed to
not fixed.

Three kinds of repair, each backed by evidence on disk:

* RESTORE -- gpt-oss:120b Compress 3, 4, 6, 7. Re-run only because their
  FixCheck assertion generator had timed out (the model was on the wrong GPU at
  0.2 tok/s). The originals were copied aside before the re-run and are put
  back; their FixCheck is flagged as degraded so it counts as "no measurement",
  never as a verdict.
* FROM_LOG -- qwen3.6:35b Math 13. Its first run printed "Failing tests after
  fix: 0" and then hung in FixCheck until the per-bug timeout, so no result.json
  was written; the re-run drew a patch that does not compile. The first run's
  verdict is reconstructed from its job log. Its patch is lost: the re-run's
  Experiment.py cleared the directory.
* HANG -- gpt-oss:120b Closure 74. The patch applied and the post-fix test suite
  did not terminate within the 3 h per-bug timeout. That is an outcome of the
  model's patch, not a harness failure, so it is recorded as not fixed rather
  than re-run (re-running only failures would bias the other way). Compilation
  was measured by re-applying the saved patch: it compiles.

The re-rolled runs are moved to results/old/rerolled-2026-09-10/ as evidence,
never deleted.

    python scripts/apply_first_verdict_rule.py            # dry run
    python scripts/apply_first_verdict_rule.py --apply
"""

import argparse
import json
import os
import shutil

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(HERE, "results")
REROLLED = os.path.join(RESULTS, "old", "rerolled-2026-09-10")
DEGRADED_COPIES = os.path.join(RESULTS, "old", "degraded-fixcheck-2026-09-10")
RULE_ID = "first-verdict-of-record/2026-09-10"

RESTORE = [("gpt-oss:120b", "Compress", b) for b in ("3", "4", "6", "7")]
FROM_LOG = [{
    "model": "qwen3.6:35b", "project": "Math", "bug_id": "13",
    "job": "16208", "seconds": 10800.5,
    "evidence": "scripts/logs/16208/bugs/Math_13.log: 'Failing tests after fix: 0', "
                "then FixCheck hung until the per-bug timeout",
}]
HANG = [{
    "model": "gpt-oss:120b", "project": "Closure", "bug_id": "74",
    "job": "16232",
    "evidence": "scripts/logs/16232/bugs/Closure_74.log: 'Diff applied: True', then the "
                "post-fix `defects4j test` ran for the full 10800 s per-bug timeout; "
                "re-applying the saved fix.diff (normalized, as the pipeline does) "
                "compiles: `defects4j compile` OK, compile.tests OK",
}]

# Fields that describe the bug or the configuration, not the draw: identical
# for every run of the same bug, so they may be taken from a sibling run.
BUG_INVARIANT = (
    "project", "bug_id", "model", "temperature", "trigger_tests",
    "failing_tests_before", "bug_metadata", "modified_files",
    "included_test_code", "included_test_log", "included_issue", "issue_status",
    "max_tokens", "context_length",
)


def run_dir(model, project, bug):
    return os.path.join(RESULTS, model, project, f"Bug_{bug}")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def already_applied(path):
    try:
        return _read(path).get("record_rule", {}).get("id") == RULE_ID
    except FileNotFoundError:
        return False


def move_aside(model, project, bug, apply):
    src = run_dir(model, project, bug)
    dst = os.path.join(REROLLED, model, project, f"Bug_{bug}")
    if not os.path.isdir(src):
        return None
    if apply:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            raise SystemExit(f"refusing to overwrite evidence at {dst}")
        shutil.move(src, dst)
    return dst


def invariant_fields(*candidates):
    for path in candidates:
        if os.path.exists(path):
            data = _read(path)
            return {k: data.get(k) for k in BUG_INVARIANT if k in data}, path
    raise SystemExit(f"no sibling result.json to take bug-invariant fields from: {candidates}")


def restore(model, project, bug, apply):
    target = run_dir(model, project, bug)
    if already_applied(os.path.join(target, "result.json")):
        return f"  {model}/{project}/Bug_{bug}: already restored"
    original = os.path.join(DEGRADED_COPIES, model, project, f"Bug_{bug}")
    if not os.path.isdir(original):
        raise SystemExit(f"original copy missing: {original}")
    was = _read(os.path.join(target, "result.json"))["fixed"]
    now = _read(os.path.join(original, "result.json"))["fixed"]
    moved = move_aside(model, project, bug, apply)
    if apply:
        shutil.copytree(original, target)
        path = os.path.join(target, "result.json")
        data = _read(path)
        data["fixcheck_degraded"] = (
            "FixCheck's Ollama assertion generator timed out "
            "(SocketTimeoutException): the model ran on the wrong GPU at ~0.2 tok/s "
            "(job 16193, audit §1.12). Not a FixCheck verdict."
        )
        data["record_rule"] = {
            "id": RULE_ID, "action": "restored_original",
            "rerolled_run_moved_to": os.path.relpath(moved, HERE),
        }
        _write(path, data)
    return f"  {model}/{project}/Bug_{bug}: restore original (fixed={now}) over re-roll (fixed={was})"


def from_log(case, apply):
    model, project, bug = case["model"], case["project"], case["bug_id"]
    target = run_dir(model, project, bug)
    if already_applied(os.path.join(target, "result.json")):
        return f"  {model}/{project}/Bug_{bug}: already reconstructed"
    rerun = os.path.join(target, "result.json")
    base, source = invariant_fields(rerun)
    was = _read(rerun)["fixed"]
    moved = move_aside(model, project, bug, apply)
    result = dict(base)
    result.update({
        # The first run's verdict, from its job log. Everything that was only
        # ever in its (lost) artifacts is None -- unknown, not zero.
        "applied": True, "compiled_after": True, "failing_tests_after": 0,
        "triggers_fixed": True, "fixed": True, "new_failures": [],
        "unidentified_failing_lines": [], "masked_triggers": [],
        "raw_response": None, "usage_metadata": None, "elapsed_seconds": None,
        "generation_status": None, "timestamp": None,
        "fixcheck": {"ran": True, "ok": False,
                     "error": "FixCheck hung until the per-bug timeout; no FixCheck verdict"},
        "fixcheck_suspicious": False,
        "verdict_source": "job_log",
        "record_rule": {
            "id": RULE_ID, "action": "reconstructed_from_job_log",
            "evidence": case["evidence"],
            "bug_invariant_fields_from": os.path.relpath(source, HERE)
            if not apply else os.path.relpath(os.path.join(moved, "result.json"), HERE),
            "rerolled_run_moved_to": os.path.relpath(moved, HERE),
            "patch": "lost -- the re-run cleared the directory before this rule existed",
        },
    })
    if apply:
        os.makedirs(target, exist_ok=True)
        _write(os.path.join(target, "result.json"), result)
        _write(os.path.join(target, "run_status.json"), {
            "status": "timeout", "seconds": case["seconds"],
            "slurm_job_id": case["job"], "has_result": True,
            "log": os.path.join(HERE, f"scripts/logs/{case['job']}/bugs/{project}_{bug}.log"),
            "note": "first run of record; result.json reconstructed from its job log",
        })
    return f"  {model}/{project}/Bug_{bug}: first run's verdict (fixed=True, from log) over re-roll (fixed={was})"


def hang(case, apply):
    model, project, bug = case["model"], case["project"], case["bug_id"]
    target = run_dir(model, project, bug)
    path = os.path.join(target, "result.json")
    if already_applied(path):
        return f"  {model}/{project}/Bug_{bug}: already recorded"
    if os.path.exists(path):
        raise SystemExit(f"{path} exists; this case assumes the run left none")
    base, source = invariant_fields(
        os.path.join(RESULTS, "old", "9-Sep", model, project, f"Bug_{bug}", "result.json"),
    )
    base["model"] = f"ollama/{model}"
    base["max_tokens"], base["context_length"] = 32768, 131072  # manifest of job 16232
    with open(os.path.join(target, "raw_response.txt"), encoding="utf-8") as f:
        raw = f.read()
    result = dict(base)
    result.update({
        "applied": True, "compiled_after": True,
        "failing_tests_after": None,        # never produced: the suite did not end
        "post_fix_tests": "did_not_terminate",
        "triggers_fixed": False, "fixed": False, "new_failures": [],
        "unidentified_failing_lines": [], "masked_triggers": [],
        "raw_response": raw, "usage_metadata": None, "elapsed_seconds": None,
        "generation_status": None, "timestamp": None,
        "fixcheck": None, "fixcheck_suspicious": False,
        "verdict_source": "job_log+reapply",
        "record_rule": {
            "id": RULE_ID, "action": "recorded_test_hang_as_not_fixed",
            "evidence": case["evidence"],
            "bug_invariant_fields_from": os.path.relpath(source, HERE),
        },
    })
    if apply:
        _write(path, result)
    return f"  {model}/{project}/Bug_{bug}: record as not fixed (patch compiles; tests never terminate)"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    print("[rule] " + ("APPLYING" if args.apply else "dry run -- nothing will be written"))
    for model, project, bug in RESTORE:
        print(restore(model, project, bug, args.apply))
    for case in FROM_LOG:
        print(from_log(case, args.apply))
    for case in HANG:
        print(hang(case, args.apply))
    if not args.apply:
        print("[rule] Re-run with --apply.")


if __name__ == "__main__":
    main()
