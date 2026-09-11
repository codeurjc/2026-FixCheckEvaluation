"""
rederive.py -- recompute every campaign verdict from the raw artifacts.

This module exists because the campaign's two known data defects were both of
the same shape: a value that degraded silently into something indistinguishable
from a legitimate value. Reading the pipeline's code cannot find those -- the
code looks reasonable, which is why it was written that way. What finds them is
recomputing the same verdicts from independent evidence and asking where the two
disagree.

So this deliberately **imports nothing from Experiment.py**. It re-reads
``apply.log``, ``test_before.log`` and ``test_after.log`` with its own parsers
and recomputes ``applied``, ``compiled_after``, ``triggers_fixed``,
``new_failures`` and ``fixed``. Every disagreement with ``result.json`` is
either a defect in the pipeline or a defect in this file; none may be discarded
without a reason.

Two design rules follow from the defects being audited:

**Never mutilate an input to make it parse.** ``Experiment.py``'s
``parse_failing_test_names`` uses ``(\\S+)``, which truncates at the first space
and turns ``- broken test input <FQCN><Exception>`` into the token ``broken``.
The trigger test then cannot match, and the bug is reported as fixed.
:func:`classify_entry` therefore *labels* each line it cannot read as
``Class::method`` instead of forcing it into that shape.

**Absence of evidence is its own value.** Where the artifacts cannot settle a
question -- an empty log, a missing file, an unreadable failing-test line that
could be hiding a trigger -- the answer is :data:`UNKNOWN`, never ``False``.
Collapsing "I could not tell" into "no" is precisely the bug that scored 203
non-compiling patches as perfect fixes.

    python -m audit.rederive                 # report + audit/rederived.csv
    python -m audit.rederive --model qwen3.6:35b
    python -m audit.rederive --divergences   # only the disagreements
"""

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict


# A third truth value, for questions the artifacts on disk cannot answer.
# It is deliberately not None: None is what a missing dict key yields, and the
# whole point here is to keep "absent" distinguishable from "measured".
class _Unknown:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self):
        raise TypeError(
            "UNKNOWN has no truth value -- decide explicitly what an "
            "undetermined result means at this call site"
        )

    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = _Unknown()


class _NotApplicable(_Unknown):
    """A question the run never reached, as opposed to one it left unanswered.

    "Did the patched sources compile?" has no answer when no patch was ever
    applied. ``result.json`` records ``False`` there (``bool(applied and
    evaluated)``), which is a defensible convention but makes a patch that broke
    the build indistinguishable from one that was never applied -- the same
    conflation this module exists to find. So the two are kept apart here, and
    :func:`compare` treats N_A as agreeing with a recorded ``False``.
    """

    _instance = None

    def __repr__(self):
        return "N_A"


N_A = _NotApplicable()


# ---------------------------------------------------------------------------
# Failing-test lines
# ---------------------------------------------------------------------------

# `defects4j test` lists each failing test on its own "  - <name>" line. The
# campaign's 7503 such lines come in three shapes, all three seen in the data:
#
#   7491  org.foo.BarTest::testBaz          the documented one
#     11  org.foo.BarTest                   class-level failure, no method
#      1  broken test input org.foo.BarTestorg.mockito...MockitoException
#                                           the class did not even initialise;
#                                           note the exception is concatenated
#                                           with no separator
FAILING_LINE = re.compile(r"^\s*-\s+(.*?)\s*$", re.MULTILINE)
TEST_NAME = re.compile(r"^([\w.$]+)::([\w.$\[\]-]+)$")
CLASS_NAME = re.compile(r"^([\w.$]+)$")
BROKEN_INPUT = re.compile(r"^broken test input\s+(.*)$")

KIND_TEST = "test"                # Class::method, fully identified
KIND_CLASS_ONLY = "class_only"    # a class failed; which method is unstated
KIND_BROKEN_INPUT = "broken_input"  # the class could not be loaded at all
KIND_UNPARSED = "unparsed"        # none of the above


@dataclass
class Entry:
    """One failing-test line, labelled rather than forced into a shape."""

    kind: str
    raw: str
    name: str = ""        # canonical "Class::method", only when kind == test
    class_name: str = ""  # the class, whenever it could be identified

    @property
    def identifies_a_test(self):
        return self.kind == KIND_TEST


def classify_entry(raw):
    """Label one ``  - <...>`` line without discarding what it actually said."""
    raw = raw.strip()
    match = TEST_NAME.match(raw)
    if match:
        return Entry(KIND_TEST, raw, name=raw, class_name=match.group(1))
    if CLASS_NAME.match(raw):
        return Entry(KIND_CLASS_ONLY, raw, class_name=raw)
    match = BROKEN_INPUT.match(raw)
    if match:
        # "<FQCN><ExceptionClass>" with no separator. The class name is the
        # longest prefix that looks like one; recovering it exactly is not
        # possible in general, so only the prefix up to the exception's package
        # is claimed, and callers treat this as "a class we cannot name".
        rest = match.group(1)
        prefix = re.match(r"^([\w.$]+?)(?=org\.|com\.|java\.|junit\.|$)", rest)
        return Entry(KIND_BROKEN_INPUT, raw,
                     class_name=prefix.group(1) if prefix else "")
    return Entry(KIND_UNPARSED, raw)


def parse_failing_entries(text):
    """Every ``  - <...>`` line of a ``defects4j test`` log, labelled."""
    return [classify_entry(m.group(1)) for m in FAILING_LINE.finditer(text)]


# ---------------------------------------------------------------------------
# Test logs
# ---------------------------------------------------------------------------

FAILING_COUNT = re.compile(r"Failing tests:\s*(\d+)")
# Defects4J prints "Running ant (<target>)....... OK|FAIL". This is a second,
# independent witness for whether the patched sources compiled -- one that does
# not depend on the absence of a line, which is what made the original defect
# invisible.
ANT_STEP = re.compile(r"Running ant \((?P<target>[\w.]+)\)\.*\s*(?P<status>OK|FAIL)")
# Experiment.py appends this to test_after.log when the patched suite outruns
# its budget, followed by the output of a separate `defects4j compile`. Kept as
# a literal (this module imports nothing from Experiment.py); a unit test pins
# the two copies together.
POST_FIX_HANG_MARKER = "[experiment] POST-FIX TESTS DID NOT TERMINATE"


@dataclass
class TestLog:
    """What a ``defects4j test`` log actually establishes."""

    present: bool = False
    empty: bool = False
    compile_status: str = ""      # "OK" | "FAIL" | "" (not stated)
    run_status: str = ""
    failing_count: object = UNKNOWN  # int | UNKNOWN
    entries: list = field(default_factory=list)
    interleaved: bool = False     # the "Failing tests:" line got mixed into
                                  # another line (stdout/stderr are merged)
    hung: bool = False            # the suite never terminated (budget ran out)

    @property
    def compiled(self):
        """Tri-state. Two witnesses; they have never disagreed, and if they
        ever do the answer is UNKNOWN rather than a guess."""
        if not self.present or self.empty:
            return UNKNOWN
        by_ant = {"OK": True, "FAIL": False}.get(self.compile_status, UNKNOWN)
        if self.hung:
            # No failure count can exist: the suite never finished. The
            # compile check appended after the marker is the only witness.
            return by_ant
        by_count = self.failing_count is not UNKNOWN
        if by_ant is UNKNOWN:
            # No ant line: fall back to the count line alone, which is the
            # signal Experiment.py uses.
            return True if by_count else UNKNOWN
        if by_ant is True and not by_count:
            # Compiled, yet no failure count was printed: the run was cut off.
            return UNKNOWN
        if by_ant is False and by_count:
            return UNKNOWN  # contradictory witnesses
        return by_ant

    @property
    def failing_names(self):
        return {e.name for e in self.entries if e.identifies_a_test}

    @property
    def unreadable_entries(self):
        return [e for e in self.entries if not e.identifies_a_test]


def parse_test_log(text, present=True):
    """Read a ``defects4j test`` log without assuming it is well formed."""
    log = TestLog(present=present, empty=(present and not text.strip()))
    if not present or log.empty:
        return log
    log.hung = POST_FIX_HANG_MARKER in text
    for match in ANT_STEP.finditer(text):
        if match.group("target").startswith("compile"):
            log.compile_status = match.group("status")
        else:
            log.run_status = match.group("status")
    count = FAILING_COUNT.search(text)
    if count:
        log.failing_count = int(count.group(1))
        # The count normally starts its own line. When it does not, stdout and
        # stderr were interleaved (docker_utils merges them with demux=False),
        # which means any line in this file could have been split mid-token.
        line_start = text.rfind("\n", 0, count.start()) + 1
        log.interleaved = bool(text[line_start:count.start()].strip())
    log.entries = parse_failing_entries(text)
    return log


# ---------------------------------------------------------------------------
# Apply logs
# ---------------------------------------------------------------------------

# apply.log records each strategy as "$ <command>\n(exit <n>)\n<output>".
APPLY_ATTEMPT = re.compile(r"^\(exit (\d+)\)$", re.MULTILINE)


@dataclass
class ApplyLog:
    present: bool = False
    exit_codes: list = field(default_factory=list)

    @property
    def applied(self):
        if not self.present:
            return UNKNOWN
        if not self.exit_codes:
            # The file exists but records no attempt: the diff was empty, so
            # nothing was ever applied. Distinct from "we do not know".
            return False
        return 0 in self.exit_codes


def parse_apply_log(text, present=True):
    log = ApplyLog(present=present)
    if present:
        log.exit_codes = [int(m.group(1)) for m in APPLY_ATTEMPT.finditer(text)]
    return log


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def masked_triggers(trigger_tests, after):
    """Triggers that an unreadable line could be hiding.

    A line naming only a class, or one Defects4J could not even load, may well
    *be* a trigger test's failure. Counting such a run as "the trigger passes"
    is the exact mistake `(\\S+)` makes.
    """
    masked = []
    for trigger in trigger_tests:
        cls = trigger.split("::")[0]
        if trigger in after.failing_names:
            continue
        for entry in after.unreadable_entries:
            if entry.class_name and (
                entry.class_name == cls or cls in entry.raw
            ):
                masked.append(trigger)
                break
    return masked


def rederive_verdict(trigger_tests, apply_log, before, after):
    """Recompute ``(applied, compiled_after, triggers_fixed, new_failures, fixed)``.

    Mirrors ``Experiment.evaluate_fix``'s *intent* -- every trigger passes and
    no previously-passing test now fails -- but resolves to UNKNOWN wherever the
    artifacts do not actually settle the question.
    """
    applied = apply_log.applied
    compiled = after.compiled

    if applied is UNKNOWN:
        # No apply.log at all: the run was killed before it got that far.
        return UNKNOWN, UNKNOWN, UNKNOWN, [], UNKNOWN
    if applied is False:
        # The diff never applied, so "did it compile" and "do the triggers
        # pass" are not unanswered -- they are not applicable. `fixed` is a
        # genuine False: an unapplied patch demonstrably fixes nothing.
        return False, N_A, N_A, [], False

    if compiled is UNKNOWN:
        return applied, UNKNOWN, UNKNOWN, [], UNKNOWN
    if compiled is False:
        return applied, False, False, [], False
    if after.hung:
        # Applied and compiled, but the suite never finished: nothing shows
        # the triggers passing, so this is not a fix.
        return applied, True, False, [], False

    if not trigger_tests:
        # Without a trigger list there is no criterion at all.
        return applied, True, UNKNOWN, [], UNKNOWN

    masked = masked_triggers(trigger_tests, after)
    still_failing = set(trigger_tests) & after.failing_names
    if still_failing:
        triggers_fixed = False
    elif masked:
        triggers_fixed = UNKNOWN
    else:
        triggers_fixed = True

    new_failures = sorted(after.failing_names - before.failing_names)
    if triggers_fixed is UNKNOWN:
        fixed = UNKNOWN
    else:
        fixed = triggers_fixed and not new_failures
    return applied, True, triggers_fixed, new_failures, fixed


# ---------------------------------------------------------------------------
# Walking the results tree
# ---------------------------------------------------------------------------

def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(), True
    except FileNotFoundError:
        return "", False


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as exc:
        # A truncated result.json is NOT the same as an absent one: it means a
        # run was killed mid-write. experiment_runner.load_result maps both to
        # None, so the runner would re-run this bug into the same directory.
        return None, f"corrupt: {exc}"


def rederive_run(run_dir):
    """Recompute one run's verdicts from its artifacts alone."""
    apply_text, apply_present = _read(os.path.join(run_dir, "apply.log"))
    before_text, before_present = _read(os.path.join(run_dir, "test_before.log"))
    after_text, after_present = _read(os.path.join(run_dir, "test_after.log"))
    result, result_error = _read_json(os.path.join(run_dir, "result.json"))

    apply_log = parse_apply_log(apply_text, apply_present)
    before = parse_test_log(before_text, before_present)
    after = parse_test_log(after_text, after_present)

    trigger_tests = (result or {}).get("trigger_tests") or []
    applied, compiled, triggers_fixed, new_failures, fixed = rederive_verdict(
        trigger_tests, apply_log, before, after
    )
    return {
        "run_dir": run_dir,
        "result_error": result_error,
        "result": result,
        "apply_log": apply_log,
        "before": before,
        "after": after,
        "trigger_tests": trigger_tests,
        "applied": applied,
        "compiled_after": compiled,
        "triggers_fixed": triggers_fixed,
        "new_failures": new_failures,
        "fixed": fixed,
    }


COMPARED_FIELDS = ("applied", "compiled_after", "triggers_fixed", "fixed")


def compare(rederived):
    """Disagreements between the re-derivation and what ``result.json`` says."""
    result = rederived["result"]
    if result is None:
        return []
    if result.get("verdict_source", "run") != "run":
        # Reconstructed under the first-verdict-of-record rule from evidence that
        # is not in the run directory (a job log, a re-applied patch). There are
        # no run artifacts to re-derive from, so "UNKNOWN" here would be noise,
        # not a finding. Reported separately by main().
        return []
    divergences = []
    for field_name in COMPARED_FIELDS:
        mine = rederived[field_name]
        theirs = result.get(field_name)
        if mine is N_A:
            # result.json's convention collapses "not applicable" to False.
            if theirs:
                divergences.append((field_name, "N_A", theirs))
        elif mine is UNKNOWN:
            divergences.append((field_name, "UNKNOWN", theirs))
        elif bool(mine) != bool(theirs):
            divergences.append((field_name, mine, theirs))
    return divergences


def walk(results_root, models=None, projects=None):
    """Every run directory under ``results/<model>/<Project>/Bug_<id>``."""
    if not os.path.isdir(results_root):
        sys.exit(f"[rederive] no such directory: {results_root}")
    for model in sorted(os.listdir(results_root)):
        if model == "old" or not os.path.isdir(os.path.join(results_root, model)):
            continue
        if models and model not in models:
            continue
        model_dir = os.path.join(results_root, model)
        for project in sorted(os.listdir(model_dir)):
            if projects and project not in projects:
                continue
            project_dir = os.path.join(model_dir, project)
            if not os.path.isdir(project_dir):
                continue
            for entry in sorted(os.listdir(project_dir)):
                if entry.startswith("Bug_"):
                    yield model, project, entry[4:], os.path.join(project_dir, entry)


def _cell(value):
    if value is N_A:
        return "N_A"
    return "UNKNOWN" if value is UNKNOWN else value


CSV_COLUMNS = [
    "model", "project", "bug_id",
    "applied", "compiled_after", "triggers_fixed", "fixed",
    "recorded_applied", "recorded_compiled_after",
    "recorded_triggers_fixed", "recorded_fixed",
    "diverges", "divergence_fields",
    "apply_exit_codes", "compile_status", "run_status", "failing_count",
    "n_entries", "n_unreadable", "masked_triggers", "interleaved",
    "before_failing_count", "result_error",
]


def main():
    parser = argparse.ArgumentParser(
        description="Recompute campaign verdicts from raw artifacts and "
                    "report every disagreement with result.json.",
    )
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--model", nargs="+", default=None)
    parser.add_argument("--project", nargs="+", default=None)
    parser.add_argument("--csv", default="audit/rederived.csv")
    parser.add_argument("--divergences", action="store_true",
                        help="List only the runs that disagree.")
    args = parser.parse_args()

    rows, divergent, unknowns, reconstructed = [], [], [], []
    for model, project, bug_id, run_dir in walk(
        args.results_dir, args.model, args.project
    ):
        red = rederive_run(run_dir)
        diffs = compare(red)
        result = red["result"] or {}
        after = red["after"]
        masked = masked_triggers(red["trigger_tests"], after) \
            if red["trigger_tests"] else []
        rows.append({
            "model": model, "project": project, "bug_id": bug_id,
            "applied": _cell(red["applied"]),
            "compiled_after": _cell(red["compiled_after"]),
            "triggers_fixed": _cell(red["triggers_fixed"]),
            "fixed": _cell(red["fixed"]),
            "recorded_applied": result.get("applied"),
            "recorded_compiled_after": result.get("compiled_after"),
            "recorded_triggers_fixed": result.get("triggers_fixed"),
            "recorded_fixed": result.get("fixed"),
            "diverges": bool(diffs),
            "divergence_fields": ",".join(d[0] for d in diffs),
            "apply_exit_codes": ",".join(str(c) for c in red["apply_log"].exit_codes),
            "compile_status": after.compile_status,
            "run_status": after.run_status,
            "failing_count": _cell(after.failing_count),
            "n_entries": len(after.entries),
            "n_unreadable": len(after.unreadable_entries),
            "masked_triggers": ",".join(masked),
            "interleaved": after.interleaved,
            "before_failing_count": _cell(red["before"].failing_count),
            "result_error": red["result_error"] or "",
        })
        if diffs:
            divergent.append((model, project, bug_id, diffs))
        source = (red["result"] or {}).get("verdict_source", "run")
        if source != "run":
            reconstructed.append((model, project, bug_id, source))
        elif any(red[f] is UNKNOWN for f in COMPARED_FIELDS):
            unknowns.append((model, project, bug_id))

    os.makedirs(os.path.dirname(args.csv) or ".", exist_ok=True)
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[rederive] {len(rows)} run(s) re-derived from raw artifacts")
    print(f"[rederive] {len(divergent)} disagree with result.json")
    print(f"[rederive] {len(unknowns)} have at least one undetermined field")
    print(f"[rederive] {len(reconstructed)} carry a reconstructed verdict "
          "(not re-derivable from run artifacts; see their record_rule)")
    for model, project, bug_id, source in reconstructed:
        print(f"             {model}/{project}/Bug_{bug_id}  [{source}]")
    print(f"[rederive] CSV written to {args.csv}\n")

    if divergent:
        by_field = {}
        for model, project, bug_id, diffs in divergent:
            for name, mine, theirs in diffs:
                by_field.setdefault((name, str(mine), str(theirs)), []).append(
                    f"{model}/{project}/Bug_{bug_id}"
                )
        print("Disagreements, grouped by field and direction:")
        for (name, mine, theirs), runs in sorted(
            by_field.items(), key=lambda kv: -len(kv[1])
        ):
            print(f"\n  {name}: re-derived={mine}  recorded={theirs}  "
                  f"({len(runs)} run(s))")
            for run in runs[:8]:
                print(f"      {run}")
            if len(runs) > 8:
                print(f"      ... and {len(runs) - 8} more")

    if args.divergences:
        return
    print("\nSanity cross-checks:")
    compiled_false = [r for r in rows if r["compiled_after"] is False]
    print(f"  re-derived compiled_after=False : {len(compiled_false)}")
    print(f"  runs with unreadable failing-test lines : "
          f"{sum(1 for r in rows if r['n_unreadable'])}")
    print(f"  runs with a masked trigger : "
          f"{sum(1 for r in rows if r['masked_triggers'])}")
    print(f"  runs with interleaved output : "
          f"{sum(1 for r in rows if r['interleaved'])}")


if __name__ == "__main__":
    main()
