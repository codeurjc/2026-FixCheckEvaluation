"""Tests for the audit verifier's pure parsers.

Every conclusion of the campaign audit rests on `audit/rederive.py` reading the
raw logs correctly, so its parsers are pinned here against the real shapes seen
in the campaign -- including the malformed ones that motivated it.
"""

import pytest

from audit.rederive import (
    KIND_BROKEN_INPUT,
    KIND_CLASS_ONLY,
    KIND_TEST,
    KIND_UNPARSED,
    N_A,
    UNKNOWN,
    classify_entry,
    masked_triggers,
    parse_apply_log,
    parse_failing_entries,
    parse_test_log,
    rederive_verdict,
)


# --------------------------------------------------------------------------
# The three real shapes of a failing-test line
# --------------------------------------------------------------------------

def test_well_formed_name_is_identified():
    entry = classify_entry("org.apache.commons.lang3.SystemUtilsTest::testGetUserHome")
    assert entry.kind == KIND_TEST
    assert entry.identifies_a_test
    assert entry.class_name == "org.apache.commons.lang3.SystemUtilsTest"


def test_bare_class_name_is_not_mistaken_for_a_test():
    """11 lines in the campaign name a class with no ``::method``."""
    entry = classify_entry("org.apache.commons.lang3.time.DateUtilsRoundingTest")
    assert entry.kind == KIND_CLASS_ONLY
    assert not entry.identifies_a_test
    assert entry.class_name == "org.apache.commons.lang3.time.DateUtilsRoundingTest"


def test_broken_test_input_keeps_its_class():
    """The line that broke ``(\\S+)``: the exception is concatenated to the FQCN."""
    entry = classify_entry(
        "broken test input org.mockitousage.bugs.InjectMocksShouldTryPropertySetters"
        "FirstBeforeFieldAccessTestorg.mockito.exceptions.base.MockitoException"
    )
    assert entry.kind == KIND_BROKEN_INPUT
    assert not entry.identifies_a_test
    assert entry.class_name.startswith("org.mockitousage.bugs.InjectMocks")


def test_entry_is_never_truncated_at_whitespace():
    """The defect being audited: ``(\\S+)`` turned this line into ``"broken"``."""
    raw = "broken test input org.example.FooTestjava.lang.Error"
    entry = classify_entry(raw)
    assert entry.raw == raw
    assert entry.raw != "broken"


def test_unrecognised_line_is_labelled_not_guessed():
    entry = classify_entry("something entirely unexpected !!")
    assert entry.kind == KIND_UNPARSED
    assert not entry.identifies_a_test


def test_parse_failing_entries_reads_every_line():
    log = """Failing tests: 3
  - org.foo.ATest::testOne
  - org.foo.BTest
  - broken test input org.foo.CTestorg.junit.Error
"""
    kinds = [e.kind for e in parse_failing_entries(log)]
    assert kinds == [KIND_TEST, KIND_CLASS_ONLY, KIND_BROKEN_INPUT]


# --------------------------------------------------------------------------
# Test logs: compiled / not compiled / undetermined
# --------------------------------------------------------------------------

PASSING_LOG = """Running ant (compile.tests)........ OK
Running ant (run.dev.tests)........ OK
Failing tests: 1
  - org.foo.ATest::testOne
"""

COMPILE_FAILED_LOG = """Running ant (compile.tests)........ FAIL
Executed command: ...
BUILD FAILED
"""


def test_compiled_true_needs_both_witnesses():
    log = parse_test_log(PASSING_LOG)
    assert log.compiled is True
    assert log.failing_count == 1
    assert log.failing_names == {"org.foo.ATest::testOne"}


def test_compile_failure_is_detected_positively_not_by_absence():
    log = parse_test_log(COMPILE_FAILED_LOG)
    assert log.compiled is False
    assert log.failing_count is UNKNOWN


def test_empty_log_is_undetermined_not_false():
    """A 0-byte test_after.log says nothing; 154 runs have one."""
    assert parse_test_log("").compiled is UNKNOWN
    assert parse_test_log("", present=False).compiled is UNKNOWN


def test_compiled_but_cut_off_before_results_is_undetermined():
    log = parse_test_log("Running ant (compile.tests)........ OK\n")
    assert log.compiled is UNKNOWN


def test_count_without_ant_lines_falls_back_to_the_count():
    log = parse_test_log("Failing tests: 0\n")
    assert log.compiled is True
    assert log.failing_count == 0


def test_contradictory_witnesses_are_undetermined():
    log = parse_test_log("Running ant (compile.tests)... FAIL\nFailing tests: 2\n")
    assert log.compiled is UNKNOWN


def test_interleaved_output_is_flagged():
    """stdout and stderr are merged (demux=False), so the count line can be
    spliced into another one -- seen once, in JacksonDatabind/Bug_33."""
    log = parse_test_log(
        "Running ant (compile.tests)... OK\n"
        "Running ant (run.dev.tests)... Failing tests: 0\nOK\n"
    )
    assert log.interleaved is True
    assert log.failing_count == 0


def test_clean_count_line_is_not_flagged_as_interleaved():
    assert parse_test_log(PASSING_LOG).interleaved is False


# --------------------------------------------------------------------------
# Apply logs
# --------------------------------------------------------------------------

def test_apply_succeeds_when_any_strategy_exits_zero():
    log = parse_apply_log("$ git apply x\n(exit 128)\nerror\n\n$ patch -p1\n(exit 0)\n")
    assert log.applied is True
    assert log.exit_codes == [128, 0]


def test_apply_fails_when_every_strategy_fails():
    assert parse_apply_log("$ git apply x\n(exit 128)\n").applied is False


def test_missing_apply_log_is_undetermined():
    assert parse_apply_log("", present=False).applied is UNKNOWN


def test_apply_log_with_no_attempt_means_nothing_was_applied():
    assert parse_apply_log("").applied is False


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------

TRIGGERS = ["org.foo.ATest::testOne"]


def _verdict(after_text, apply_text="$ git apply\n(exit 0)\n", before_text=PASSING_LOG):
    return rederive_verdict(
        TRIGGERS,
        parse_apply_log(apply_text),
        parse_test_log(before_text),
        parse_test_log(after_text),
    )


def test_trigger_passing_and_no_regression_is_fixed():
    applied, compiled, triggers, new, fixed = _verdict(
        "Running ant (compile.tests)... OK\nRunning ant (run.dev.tests)... OK\n"
        "Failing tests: 0\n"
    )
    assert (applied, compiled, triggers, new, fixed) == (True, True, True, [], True)


def test_trigger_still_failing_is_not_fixed():
    _, _, triggers, _, fixed = _verdict(PASSING_LOG)
    assert triggers is False and fixed is False


def test_non_compiling_patch_is_never_fixed():
    applied, compiled, triggers, _, fixed = _verdict(COMPILE_FAILED_LOG)
    assert applied is True
    assert compiled is False and triggers is False and fixed is False


def test_unapplied_patch_is_not_applicable_not_false():
    applied, compiled, triggers, _, fixed = _verdict(
        PASSING_LOG, apply_text="$ git apply\n(exit 1)\n"
    )
    assert applied is False
    assert compiled is N_A and triggers is N_A
    assert fixed is False


def test_masked_trigger_is_undetermined_never_fixed():
    """The Mockito 15 case: the trigger's class blew up, so its ``- `` line
    names no method. Reporting ``triggers_fixed`` here is the defect."""
    after = ("Running ant (compile.tests)... OK\nRunning ant (run.dev.tests)... OK\n"
             "Failing tests: 1\n  - broken test input org.foo.ATestorg.junit.Error\n")
    _, _, triggers, _, fixed = _verdict(after)
    assert triggers is UNKNOWN
    assert fixed is UNKNOWN


def test_unrelated_broken_class_does_not_mask_the_trigger():
    after = ("Running ant (compile.tests)... OK\nRunning ant (run.dev.tests)... OK\n"
             "Failing tests: 1\n  - org.other.ZTest\n")
    _, _, triggers, _, fixed = _verdict(after)
    assert triggers is True and fixed is True


def test_killed_run_with_no_apply_log_is_undetermined():
    applied, compiled, triggers, _, fixed = rederive_verdict(
        TRIGGERS, parse_apply_log("", present=False),
        parse_test_log("", present=False), parse_test_log("", present=False),
    )
    assert applied is UNKNOWN and compiled is UNKNOWN
    assert triggers is UNKNOWN and fixed is UNKNOWN


def test_new_failures_are_the_regressions_only():
    before = ("Running ant (compile.tests)... OK\nFailing tests: 1\n"
              "  - org.foo.ATest::testOne\n")
    after = ("Running ant (compile.tests)... OK\nRunning ant (run.dev.tests)... OK\n"
             "Failing tests: 1\n  - org.foo.BTest::testTwo\n")
    _, _, triggers, new, fixed = _verdict(after, before_text=before)
    assert triggers is True
    assert new == ["org.foo.BTest::testTwo"]
    assert fixed is False


def test_masked_triggers_finds_the_hidden_one():
    after = parse_test_log(
        "Failing tests: 1\n  - broken test input org.foo.ATestorg.junit.Error\n"
    )
    assert masked_triggers(TRIGGERS, after) == TRIGGERS


# --------------------------------------------------------------------------
# UNKNOWN must never be silently coerced
# --------------------------------------------------------------------------

def test_unknown_refuses_to_be_a_boolean():
    """The whole point: no call site may accidentally read UNKNOWN as False."""
    with pytest.raises(TypeError):
        bool(UNKNOWN)
    with pytest.raises(TypeError):
        bool(N_A)


def test_unknown_and_not_applicable_are_distinct():
    assert UNKNOWN is not N_A
    assert repr(UNKNOWN) == "UNKNOWN" and repr(N_A) == "N_A"
