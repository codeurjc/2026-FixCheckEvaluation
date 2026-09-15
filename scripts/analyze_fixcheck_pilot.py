#!/usr/bin/env python3
"""Check the FixCheck v2 pilot against its acceptance criteria.

Reads every ``fixcheck_v2.json`` under ``results/`` (a model's plausible
patches and ``results/controls/``) plus the ``fixcheck.log`` of each FixCheck
run copied next to it, and reports what docs/fixcheck-v2-protocol.md's
"Acceptance criteria" ask to measure:

1. no shading (stack frames from FixCheck's relocated packages; Cli 35's lines),
2. no ``IllegalAccessError``,
3. no sibling tests (every failure reported belongs to the mutated method),
4. no aborted run, and Mockito 31 compiled,
5. no ``FileNotFoundException``,
6. Math 10 finishing within budget,
7. reproducibility -- see ``--compare``,
8. background noise (failure rate of identity mutations),
9. DefectRepairing ``author`` subjects with a report,
10. cost (seconds per subject, per run and in the assertion generator).

``--compare A B`` compares two records of the same subject (e.g. a record and
its ``.previous``): identical mutations, outcomes and scores.

Usage:
    .venv/bin/python scripts/analyze_fixcheck_pilot.py [--results-root results] [--json out.json]
    .venv/bin/python scripts/analyze_fixcheck_pilot.py --compare path/fixcheck_v2.json.previous path/fixcheck_v2.json
"""

import argparse
import glob
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from FixCheckWrapper import FIXCHECK_FAILING_OUTCOMES, fixcheck_run_subdir  # noqa: E402
from summarize_campaign import FIXCHECK_REPLAY_FILE  # noqa: E402

# JUnit's failure line: "\t<method>(<class>): <message>"
_FAILURE_LINE_RE = re.compile(r"^\t([\w$]+)(?:\[[^\]]*\])?\(([\w.$]+)\)(?::|$)", re.M)
_SHADED_FRAME_RE = re.compile(r"at org\.imdea\.fixcheck\.shaded\.")
_ILLEGAL_ACCESS_RE = re.compile(r"IllegalAccessError")
_FILE_NOT_FOUND_RE = re.compile(r"FileNotFoundException")


def find_records(results_root):
    pattern = os.path.join(results_root, "**", FIXCHECK_REPLAY_FILE)
    return sorted(glob.glob(pattern, recursive=True))


def describe(record):
    target = record.get("target")
    if target == "defectrepairing":
        return f"dr/{record.get('config')}/{record.get('oracle')}/{record['project']}/{record['subject']}"
    if target == "devfix":
        return f"devfix/{record.get('oracle')}/{record['project']}-{record['subject']}"
    return f"plausible/{record.get('oracle')}/{record['project']}-{record['subject']}"


def run_log(record_path, run):
    path = os.path.join(os.path.dirname(record_path), "fixcheck_v2", fixcheck_run_subdir(run), "fixcheck.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def sibling_failures(log_text, method):
    """Failure lines naming a test method other than the mutated one."""
    return sorted({m for m, _cls in _FAILURE_LINE_RE.findall(log_text or "") if m != method})


def mutation_independent(run, log_text):
    """Why a run's failures do not depend on its mutations, or ``None``.

    Two signs: the identity mutations (literal unchanged, on a patch that
    passes the trigger test) fail too, or every prefix failed before any
    assertion was generated with one and the same failure message. Collections
    20 shows both: removing ``assertEquals("A", li.next())`` also removed the
    ``li.next()`` a later ``li.remove()`` needs.
    """
    variations = run.get("variations") or []
    if not variations:
        return None
    identity = [v for v in variations if v.get("identity")]
    failing_identity = [v for v in identity if v.get("outcome") in FIXCHECK_FAILING_OUTCOMES]
    messages = {re.sub(r"SimilarPrefixInputTransformer\d+", "P", m)
                for m in re.findall(r"^\t[\w$]+(?:\[[^\]]*\])?\([\w.$]+\): ?(.*)$", log_text or "", re.M)}
    all_failed_early = all(v.get("outcome") in FIXCHECK_FAILING_OUTCOMES and not v.get("assertions_generated")
                           for v in variations)
    reasons = []
    if identity and len(failing_identity) == len(identity):
        reasons.append(f"all {len(identity)} identity mutations fail")
    if all_failed_early and len(messages) == 1:
        reasons.append(f"all {len(variations)} prefixes fail before assertion generation with one message "
                       f"{next(iter(messages))[:80]!r}")
    return "; ".join(reasons) or None


def analyze_record(record_path):
    with open(record_path, encoding="utf-8") as f:
        record = json.load(f)
    fc = record.get("fixcheck") or {}
    row = {
        "id": describe(record),
        "path": record_path,
        "target": record.get("target"),
        "config": record.get("config"),
        "oracle": record.get("oracle"),
        "project": record.get("project"),
        "subject": record.get("subject"),
        "correctness": record.get("correctness"),
        "patch_fixed": record.get("patch_fixed"),
        "status": record.get("replay_status"),
        "reason": record.get("reason"),
        "seconds": record.get("seconds"),
        "ok": fc.get("ok"),
        "error": fc.get("error"),
        "planned_runs": fc.get("planned_runs"),
        "analyzed_runs": fc.get("analyzed_runs"),
        "timed_out_runs": fc.get("timed_out_runs"),
        "generated": fc.get("generated_prefixes"),
        "failing": fc.get("failing_prefixes"),
        "scored": fc.get("scored_prefixes"),
        "non_compiling": fc.get("non_compiling_prefixes"),
        "timed_out_prefixes": fc.get("timed_out_prefixes"),
        "generation_failed": fc.get("assertion_generation_failed_prefixes"),
        "identity": fc.get("identity_prefixes"),
        "identity_failing": fc.get("identity_failing_prefixes"),
        "max_similarity": fc.get("max_failure_similarity"),
        "suspicious": fc.get("suspicious") if fc.get("analyzed_runs") else None,
        "compiled_test_classes": fc.get("compiled_test_classes"),
        "illegal_access": 0,
        "file_not_found": 0,
        "shaded_frames": 0,
        "sibling_failures": [],
        "mutation_independent": [],
        "failed_runs": [],
        "missing_logs": 0,
        "run_seconds": [],
        "assertions_ms": 0,
        "running_ms": 0,
        "llm_prefixes": 0,
        "cli35_checkout_lines": None,
    }
    for run in fc.get("runs", []):
        row["run_seconds"].append(run.get("seconds"))
        if not run.get("ok"):
            row["failed_runs"].append(
                f"{run['test_class'].rsplit('.', 1)[-1]}::{run['method']}/{run['inputs_class']}: "
                f"{(run.get('error') or '').splitlines()[0] if run.get('error') else 'not ok'}"
                f"{' (timed out)' if run.get('timed_out') else ''}"
            )
        report = run.get("report") or {}
        row["assertions_ms"] += report.get("assertions_gen_time_ms") or 0
        row["running_ms"] += report.get("prefixes_running_time_ms") or 0
        row["llm_prefixes"] += sum(1 for v in run.get("variations", []) if v.get("assertions_generated"))
        text = run_log(record_path, run)
        if text is None:
            row["missing_logs"] += 1
            continue
        row["illegal_access"] += len(_ILLEGAL_ACCESS_RE.findall(text))
        row["file_not_found"] += len(_FILE_NOT_FOUND_RE.findall(text))
        row["shaded_frames"] += len(_SHADED_FRAME_RE.findall(text))
        row["sibling_failures"] += sibling_failures(text, run["method"])
        independent = mutation_independent(run, text)
        if independent:
            row["mutation_independent"].append(
                f"{run['test_class'].rsplit('.', 1)[-1]}::{run['method']}/{run['inputs_class']}: {independent}")
        if record.get("project") == "Cli" and str(record.get("subject")) == "35":
            lines = set(re.findall(r"DefaultParser\.java:(\d+)", text))
            row["cli35_checkout_lines"] = sorted(lines | set(row["cli35_checkout_lines"] or []), key=int)
    return row, record


def pct(num, den):
    return f"{100 * num / den:.1f}%" if den else "n/a"


def report(rows):
    out = []
    p = out.append
    by_target = Counter((r["target"], r["config"], r["oracle"]) for r in rows)
    p(f"Records: {len(rows)}")
    for key, n in sorted(by_target.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        statuses = Counter(r["status"] for r in rows if (r["target"], r["config"], r["oracle"]) == key)
        p(f"  {'/'.join(str(k) for k in key if k)}: {n} {dict(statuses)}")

    not_ok = [r for r in rows if r["status"] != "ok"]
    if not_ok:
        p("\nNot replayed (status != ok):")
        for r in not_ok:
            p(f"  {r['id']}: {r['status']} -- {r['reason']}")

    ok = [r for r in rows if r["status"] == "ok"]
    p("\n1. Shading")
    p(f"   stack frames from relocated FixCheck packages: {sum(r['shaded_frames'] for r in ok)}")
    for r in ok:
        if r["cli35_checkout_lines"] is not None:
            p(f"   {r['id']}: DefaultParser.java lines seen {r['cli35_checkout_lines']}")

    p("\n2. IllegalAccessError")
    hits = [r for r in ok if r["illegal_access"]]
    p(f"   occurrences: {sum(r['illegal_access'] for r in ok)} in {len(hits)} record(s)")
    for r in hits:
        p(f"   {r['id']}: {r['illegal_access']}")

    p("\n3. Sibling tests")
    hits = [r for r in ok if r["sibling_failures"]]
    p(f"   records with a failure outside the mutated method: {len(hits)}")
    for r in hits:
        p(f"   {r['id']}: {sorted(set(r['sibling_failures']))}")

    p("\n4. Aborted runs / Mockito 31")
    p(f"   FixCheck runs planned {sum(r['planned_runs'] or 0 for r in ok)}, "
      f"analysed {sum(r['analyzed_runs'] or 0 for r in ok)}, "
      f"timed out {sum(r['timed_out_runs'] or 0 for r in ok)}")
    for r in ok:
        for failure in r["failed_runs"]:
            p(f"   {r['id']}: {failure}")
        if r["missing_logs"]:
            p(f"   {r['id']}: {r['missing_logs']} run log(s) missing")
        if r["project"] == "Mockito":
            p(f"   {r['id']}: analysed {r['analyzed_runs']}/{r['planned_runs']}, "
              f"compiled_test_classes={r['compiled_test_classes']}, non_compiling={r['non_compiling']}")
    empty = [r for r in ok if not r["analyzed_runs"]]
    p(f"   records with nothing analysed: {len(empty)}")
    for r in empty:
        p(f"   {r['id']}: planned {r['planned_runs']} -- {r['error']}")

    p("\n5. FileNotFoundException")
    hits = [r for r in ok if r["file_not_found"]]
    p(f"   occurrences: {sum(r['file_not_found'] for r in ok)} in {len(hits)} record(s)")
    for r in hits:
        p(f"   {r['id']}: {r['file_not_found']}")

    p("\n6. Math 10")
    for r in rows:
        if r["project"] == "Math" and str(r["subject"]) == "10":
            p(f"   {r['id']}: status={r['status']} seconds={r['seconds']} runs timed out={r['timed_out_runs']} "
              f"prefixes timed out={r['timed_out_prefixes']} generated={r['generated']}")

    p("\n8. Background noise (identity mutations)")
    ident = sum(r["identity"] or 0 for r in ok)
    ident_fail = sum(r["identity_failing"] or 0 for r in ok)
    p(f"   identity prefixes {ident}, failing {ident_fail} ({pct(ident_fail, ident)}); archived campaign: 37.7%")
    noisy = [r for r in ok if r["identity_failing"]]
    for r in noisy:
        p(f"   {r['id']}: {r['identity_failing']}/{r['identity']}")
    independent = [r for r in ok if r["mutation_independent"]]
    analysed = [r for r in ok if r["analyzed_runs"]]
    p(f"   records with a mutation-independent run: {len(independent)}/{len(analysed)} "
      f"({pct(len(independent), len(analysed))}); flagged among them: "
      f"{sum(1 for r in independent if r['suspicious'])}")
    for r in independent:
        for why in r["mutation_independent"]:
            p(f"   {r['id']}: {why}")
    gen = sum(r["generated"] or 0 for r in ok)
    fail = sum(r["failing"] or 0 for r in ok)
    p(f"   all prefixes: {gen}, failing {fail} ({pct(fail, gen)}), "
      f"non-compiling {sum(r['non_compiling'] or 0 for r in ok)}, timed out {sum(r['timed_out_prefixes'] or 0 for r in ok)}, "
      f"assertion generation failed {sum(r['generation_failed'] or 0 for r in ok)} "
      f"(unmeasured in {sum(1 for r in ok if r['analyzed_runs'] and r['generation_failed'] is None)} record(s))")

    p("\n9. DefectRepairing, author configuration")
    for oracle in sorted({r["oracle"] for r in rows if r["target"] == "defectrepairing"}):
        subset = [r for r in rows if r["target"] == "defectrepairing" and r["config"] == "author" and r["oracle"] == oracle]
        with_report = [r for r in subset if r["status"] == "ok" and r["analyzed_runs"]]
        p(f"   {oracle}: {len(with_report)}/{len(subset)} with a report ({pct(len(with_report), len(subset))})")

    p("\n10. Cost")
    secs = [r["seconds"] for r in ok if r["seconds"]]
    run_secs = [s for r in ok for s in r["run_seconds"] if s]
    if secs:
        p(f"   per subject: n={len(secs)} median {statistics.median(secs):.0f}s mean {statistics.mean(secs):.0f}s "
          f"max {max(secs):.0f}s total {sum(secs) / 3600:.1f}h")
    if run_secs:
        p(f"   per FixCheck run: n={len(run_secs)} median {statistics.median(run_secs):.0f}s max {max(run_secs):.0f}s")
    for oracle in sorted({r["oracle"] for r in ok}):
        subset = [r for r in ok if r["oracle"] == oracle]
        llm = sum(r["llm_prefixes"] for r in subset)
        ms = sum(r["assertions_ms"] for r in subset)
        p(f"   {oracle}: {sum(r['seconds'] or 0 for r in subset) / 3600:.1f}h over {len(subset)} subjects; "
          f"assertion generation {ms / 3.6e6:.1f}h for {llm} prefixes "
          f"({ms / llm / 1000:.1f}s each)" if llm else f"   {oracle}: no generated assertions")

    p("\nVerdicts (analysed records only)")
    groups = defaultdict(list)
    for r in ok:
        if not r["analyzed_runs"]:
            continue
        label = r["target"] if r["target"] != "defectrepairing" else f"dr-{r['config']}-{r['correctness']}"
        if r["target"] == "plausible":
            label += "-fixed" if r["patch_fixed"] else "-regressing"
        groups[(label, r["oracle"])].append(r)
    for (label, oracle), subset in sorted(groups.items()):
        flagged = sum(1 for r in subset if r["suspicious"])
        p(f"   {label} [{oracle}]: {flagged}/{len(subset)} flagged ({pct(flagged, len(subset))})")
    p("\nPer record")
    for r in sorted(rows, key=lambda r: r["id"]):
        sim = f"{r['max_similarity']:.3f}" if isinstance(r["max_similarity"], float) else "-"
        p(f"   {r['id']:<60} {r['status']:<14} runs {r['analyzed_runs']}/{r['planned_runs']} "
          f"gen {r['generated']} fail {r['failing']} scored {r['scored']} max {sim} susp {r['suspicious']} "
          f"{(r['seconds'] or 0) / 60:.0f}min")
    return "\n".join(out)


def compare(path_a, path_b):
    def load(path):
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        return {(run["test_class"], run["method"], run["inputs_class"]): run
                for run in (record.get("fixcheck") or {}).get("runs", [])}

    a, b = load(path_a), load(path_b)
    lines = []
    same_mut = same_out = total = 0
    for key in sorted(set(a) & set(b)):
        va, vb = a[key].get("variations", []), b[key].get("variations", [])
        if a[key].get("seed") != b[key].get("seed"):
            lines.append(f"{key}: different seeds")
        for x, y in zip(va, vb):
            total += 1
            same_mut += (x["original"], x["replacement"]) == (y["original"], y["replacement"])
            same_out += (x["outcome"], x["score"]) == (y["outcome"], y["score"])
        if len(va) != len(vb):
            lines.append(f"{key}: {len(va)} vs {len(vb)} prefixes")
    for key in sorted(set(a) ^ set(b)):
        lines.append(f"{key}: only in one record")
    lines.append(f"prefixes compared {total}: identical mutations {same_mut} ({pct(same_mut, total)}), "
                 f"identical outcome and score {same_out} ({pct(same_out, total)})")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-root", default=os.path.join(ROOT, "results"))
    parser.add_argument("--json", default=None, help="Also write the per-record rows here.")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None)
    args = parser.parse_args(argv)
    if args.compare:
        print(compare(*args.compare))
        return 0
    rows = [analyze_record(path)[0] for path in find_records(args.results_root)]
    print(report(rows))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
