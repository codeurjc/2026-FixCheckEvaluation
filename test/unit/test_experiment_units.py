"""
Fast, deterministic unit tests for Experiment's pure helpers: the trigger-based
fix criterion, the failing-test parser, and the Java trigger-method extraction.
No Docker, Ollama or network required.

Run with:

    .venv/bin/python -m pytest test/test_experiment_units.py -v
"""

import os

from Experiment import (
    evaluate_fix,
    extract_java_method,
    extract_trigger_test_code,
    parse_failing_test_lines,
    parse_failing_test_names,
    triggers_possibly_masked,
    unidentified_failing_lines,
    write_text,
)

TRIGGER = "org.apache.commons.lang3.math.NumberUtilsTest::TestLang747"
FLAKY = "org.apache.commons.lang3.SystemUtilsTest::testGetUserHome"

TEST_OUTPUT = f"""\
Running tests...
Failing tests: 2
  - {FLAKY}
  - {TRIGGER}
"""


# ------------------------------------------------------ parse_failing_test_names

def test_parse_failing_test_names():
    names = parse_failing_test_names(TEST_OUTPUT)
    assert names == [FLAKY, TRIGGER]


def test_parse_failing_test_names_empty():
    assert parse_failing_test_names("Failing tests: 0\n") == []


# --------------------------------------------------------------- evaluate_fix

def test_evaluate_fix_true_when_trigger_passes_and_no_regressions():
    # The trigger stops failing; the flaky test fails before and after (not a
    # regression) — the bug is considered fixed.
    triggers_fixed, new_failures, fixed = evaluate_fix(
        trigger_tests=[TRIGGER],
        failing_before_names=[FLAKY, TRIGGER],
        failing_after_names=[FLAKY],
        applied=True,
    )
    assert triggers_fixed is True
    assert new_failures == []
    assert fixed is True


def test_evaluate_fix_false_when_trigger_still_fails():
    triggers_fixed, new_failures, fixed = evaluate_fix(
        trigger_tests=[TRIGGER],
        failing_before_names=[TRIGGER],
        failing_after_names=[TRIGGER],
        applied=True,
    )
    assert triggers_fixed is False
    assert fixed is False


def test_evaluate_fix_false_on_new_regression():
    # Trigger passes, but the patch breaks a previously-passing test.
    triggers_fixed, new_failures, fixed = evaluate_fix(
        trigger_tests=[TRIGGER],
        failing_before_names=[TRIGGER],
        failing_after_names=["some.Other::testBroken"],
        applied=True,
    )
    assert triggers_fixed is True
    assert new_failures == ["some.Other::testBroken"]
    assert fixed is False


def test_evaluate_fix_false_when_not_applied():
    triggers_fixed, new_failures, fixed = evaluate_fix(
        trigger_tests=[TRIGGER],
        failing_before_names=[TRIGGER],
        failing_after_names=[],
        applied=False,
    )
    assert fixed is False


def test_evaluate_fix_false_without_trigger_tests():
    # With no trigger tests we cannot claim the bug is fixed.
    triggers_fixed, _, fixed = evaluate_fix([], [], [], applied=True)
    assert triggers_fixed is False
    assert fixed is False


# -------------------------------------------------------- extract_java_method

JAVA = """\
public class NumberUtilsTest {

    @Test
    public void testOther() {
        assertEquals(1, 1);
    }

    /**
     * Javadoc for the failing test.
     */
    @Test
    public void TestLang747() {
        assertEquals(Integer.valueOf(0x8000), NumberUtils.createNumber("0x8000"));
        if (true) {
            assertTrue(true);
        }
    }

    private int helper() {
        return 0;
    }
}
"""


def test_extract_java_method_returns_target_method_with_annotation():
    code = extract_java_method(JAVA, "TestLang747")
    assert code is not None
    assert "public void TestLang747()" in code
    assert "@Test" in code
    assert "Javadoc for the failing test" in code
    # It must not bleed into the neighbouring methods.
    assert "testOther" not in code
    assert "helper" not in code
    # Nested braces are balanced: the method ends at its own closing brace.
    assert code.count("{") == code.count("}")


def test_extract_java_method_missing_returns_none():
    assert extract_java_method(JAVA, "doesNotExist") is None


# --------------------------------------------------- extract_trigger_test_code

def test_extract_trigger_test_code_keeps_only_failing_method():
    rel = "src/test/java/org/apache/commons/lang3/math/NumberUtilsTest.java"
    trigger = "org.apache.commons.lang3.math.NumberUtilsTest::TestLang747"
    reduced = extract_trigger_test_code([trigger], [(rel, JAVA)])
    assert len(reduced) == 1
    out_rel, content = reduced[0]
    assert out_rel == rel
    assert "TestLang747" in content
    assert "testOther" not in content
    assert content.startswith(f"// Failing test method(s) from {rel}")


def test_extract_trigger_test_code_falls_back_to_full_file():
    # When the method can't be extracted, the whole file is kept.
    rel = "src/test/java/org/apache/commons/lang3/math/NumberUtilsTest.java"
    trigger = "org.apache.commons.lang3.math.NumberUtilsTest::missingMethod"
    reduced = extract_trigger_test_code([trigger], [(rel, JAVA)])
    assert reduced == [(rel, JAVA)]


def test_evaluate_fix_false_when_the_patch_never_compiled():
    """An empty failure list is ambiguous and must not read as success.

    When the patched sources do not compile, `defects4j test` prints no
    "Failing tests:" line, so `parse_failing_tests` returns -1 and the parsed
    failure list comes back empty. Without the `evaluated` guard that looks
    exactly like "no trigger test fails" and the patch scores as a perfect fix
    -- 203 runs of the campaign did precisely that, ~20% of every reported fix.
    """
    triggers_fixed, new_failures, fixed = evaluate_fix(
        ["C::t"], ["C::t"], [], applied=True, evaluated=False,
    )
    assert not triggers_fixed and not fixed
    assert new_failures == []


def test_evaluate_fix_still_true_for_a_genuine_fix():
    # The guard must not disturb the normal path: a real fix compiles, so the
    # run is evaluated and the trigger test is absent from the failures.
    triggers_fixed, _, fixed = evaluate_fix(
        ["C::t"], ["C::t"], [], applied=True, evaluated=True,
    )
    assert triggers_fixed and fixed


def test_evaluate_fix_defaults_to_evaluated():
    # Callers that legitimately know the run produced results (the developer-fix
    # e2e pipeline) keep working unchanged.
    triggers_fixed, _, fixed = evaluate_fix(["C::t"], ["C::t"], [], applied=True)
    assert triggers_fixed and fixed


# ------------------------------------- failing-test lines that name no method
#
# `defects4j test` does not only emit "  - Class::method". Two other shapes
# appear in the campaign's logs, and the previous `(\S+)` parser mangled both:
# it truncated at the first space, so "broken test input <FQCN><Exception>"
# became the token "broken" and the trigger test it was reporting could never
# be matched -- scoring a bug whose trigger class had blown up as fixed.

BROKEN_INPUT_LINE = (
    "broken test input org.mockitousage.bugs.InjectMocksTest"
    "org.mockito.exceptions.base.MockitoException"
)
BARE_CLASS_LINE = "org.apache.commons.lang3.time.DateUtilsRoundingTest"

MIXED_OUTPUT = f"""\
Running ant (compile.tests)....... OK
Failing tests: 3
  - {TRIGGER}
  - {BARE_CLASS_LINE}
  - {BROKEN_INPUT_LINE}
"""


def test_parse_failing_test_lines_keeps_every_line_verbatim():
    lines = parse_failing_test_lines(MIXED_OUTPUT)
    assert lines == [TRIGGER, BARE_CLASS_LINE, BROKEN_INPUT_LINE]


def test_parse_failing_test_names_excludes_lines_naming_no_method():
    assert parse_failing_test_names(MIXED_OUTPUT) == [TRIGGER]


def test_parse_failing_test_names_never_truncates_at_whitespace():
    """The exact defect: the old parser returned "broken" for this line."""
    names = parse_failing_test_names(f"Failing tests: 1\n  - {BROKEN_INPUT_LINE}\n")
    assert names == []
    assert "broken" not in names


def test_unidentified_failing_lines_reports_the_rest():
    assert unidentified_failing_lines(MIXED_OUTPUT) == [
        BARE_CLASS_LINE, BROKEN_INPUT_LINE,
    ]


def test_no_unidentified_lines_in_a_normal_log():
    assert unidentified_failing_lines(TEST_OUTPUT) == []


# ---------------------------------------------------------- masked triggers

def test_trigger_hidden_in_a_broken_input_line_is_detected():
    trigger = "org.mockitousage.bugs.InjectMocksTest::shouldInject"
    output = f"Failing tests: 1\n  - {BROKEN_INPUT_LINE}\n"
    assert triggers_possibly_masked([trigger], output) == [trigger]


def test_trigger_hidden_behind_a_bare_class_name_is_detected():
    trigger = f"{BARE_CLASS_LINE}::testRoundToNearest"
    output = f"Failing tests: 1\n  - {BARE_CLASS_LINE}\n"
    assert triggers_possibly_masked([trigger], output) == [trigger]


def test_unrelated_unidentified_line_masks_nothing():
    output = f"Failing tests: 1\n  - {BARE_CLASS_LINE}\n"
    assert triggers_possibly_masked([TRIGGER], output) == []


def test_trigger_already_named_explicitly_is_not_masked():
    output = f"Failing tests: 2\n  - {TRIGGER}\n  - {BARE_CLASS_LINE}\n"
    assert triggers_possibly_masked([TRIGGER], output) == []


def test_evaluate_fix_is_negative_when_a_trigger_may_be_masked():
    """gpt-oss:120b/Mockito/Bug_15 recorded triggers_fixed=True this way."""
    trigger = "org.mockitousage.bugs.InjectMocksTest::shouldInject"
    triggers_fixed, _, fixed = evaluate_fix(
        [trigger], [trigger], [], applied=True, evaluated=True,
        masked_triggers=[trigger],
    )
    assert triggers_fixed is False
    assert fixed is False


def test_evaluate_fix_unaffected_when_nothing_is_masked():
    triggers_fixed, _, fixed = evaluate_fix(
        [TRIGGER], [TRIGGER], [], applied=True, evaluated=True, masked_triggers=[],
    )
    assert triggers_fixed is True and fixed is True


# ------------------------------------------------------------ atomic writes

def test_write_text_leaves_no_temporary_file(tmp_path):
    write_text(str(tmp_path), "result.json", '{"a": 1}')
    assert (tmp_path / "result.json").read_text() == '{"a": 1}'
    assert list(tmp_path.iterdir()) == [tmp_path / "result.json"]


def test_write_text_replaces_an_existing_file(tmp_path):
    write_text(str(tmp_path), "x.log", "old")
    write_text(str(tmp_path), "x.log", "new")
    assert (tmp_path / "x.log").read_text() == "new"


# ------------------------------------------------- the generation budget
#
# 46 runs of the first campaign lost the model's answer to a token ceiling and
# were recorded as the model failing to fix the bug. The budget is now explicit,
# reachable from the command line, and reaching it is a recorded fact.

class _FakeMessage:
    def __init__(self, content, thinking=""):
        self.content = content
        self.thinking = thinking


class _FakeChatResponse:
    def __init__(self, content, *, thinking="", prompt=100, eval_=50, done_reason="stop"):
        self.message = _FakeMessage(content, thinking)
        self.prompt_eval_count = prompt
        self.eval_count = eval_
        self.done_reason = done_reason


def test_normal_generation_is_ok():
    from llms.ollama_llm import build_response

    r = build_response(_FakeChatResponse("a patch"), num_ctx=4096, max_tokens=1024)
    assert r.generation_status() == "ok"
    assert r.content == "a patch" and not r.truncated and not r.context_exhausted


def test_reasoning_that_never_answered_is_not_a_silent_empty_string():
    """qwen3.6 puts its chain of thought in `thinking`; when the budget ran out
    there, `content` came back empty and the run was scored as a failed fix."""
    from llms.ollama_llm import build_response

    r = build_response(
        _FakeChatResponse("", thinking="x" * 5000, eval_=1024, done_reason="length"),
        num_ctx=8192, max_tokens=1024,
    )
    assert r.empty
    assert r.truncated is True
    assert r.generation_status() == "truncated"
    assert r.reasoning_chars if hasattr(r, "reasoning_chars") else r.thinking_chars


def test_output_cap_and_context_exhaustion_are_told_apart():
    """The remedy differs: one needs a longer num_predict, the other a bigger
    window. 30 runs hit the first, 16 the second."""
    from llms.ollama_llm import build_response

    capped = build_response(
        _FakeChatResponse("", eval_=24576, prompt=5000), num_ctx=131072,
        max_tokens=24576,
    )
    assert capped.truncated is True and capped.context_exhausted is False
    assert capped.generation_status() == "truncated"

    exhausted = build_response(
        _FakeChatResponse("", eval_=1, prompt=49151), num_ctx=49152,
        max_tokens=32768,
    )
    assert exhausted.context_exhausted is True
    assert exhausted.generation_status() == "context_exhausted"


def test_thinking_is_never_passed_off_as_the_answer():
    from llms.ollama_llm import build_response

    r = build_response(
        _FakeChatResponse("", thinking="I should change line 42"),
        num_ctx=4096, max_tokens=1024,
    )
    assert r.content == ""          # the reasoning is not a patch
    assert r.thinking_chars > 0     # ... but we record that there was some


def test_budget_defaults_are_the_audited_ones():
    """Sized from the campaign: the largest successful generation used 18595
    output tokens, and 17 of 854 prompts exceeded the old 49152-token window."""
    from FixGenerator import FixGenerator

    assert FixGenerator.DEFAULT_MAX_TOKENS == 32768
    assert FixGenerator.DEFAULT_CONTEXT_LENGTH == 131072
    assert FixGenerator.DEFAULT_CONTEXT_LENGTH > FixGenerator.DEFAULT_MAX_TOKENS


# ------------------------------------------ an interrupted FixCheck is not a verdict

def test_fixcheck_pending_marker_reads_as_not_run():
    """result.json is written *before* FixCheck, carrying this marker. If the
    run is killed during FixCheck, the marker is what stays on disk -- and every
    reader must see "FixCheck did not produce a verdict", not "not suspicious".
    """
    from Experiment import FIXCHECK_PENDING

    assert FIXCHECK_PENDING["ok"] is False
    assert FIXCHECK_PENDING["pending"] is True
    assert "analyzed_test_classes" not in FIXCHECK_PENDING


def test_collect_project_treats_pending_fixcheck_as_invoked_not_ran(tmp_path):
    import json
    from Experiment import FIXCHECK_PENDING
    from summarize_campaign import collect_project

    bug = tmp_path / "Math" / "Bug_10"
    bug.mkdir(parents=True)
    (bug / "result.json").write_text(json.dumps({
        "applied": True, "compiled_after": True, "triggers_fixed": True,
        "fixed": True, "fixcheck": dict(FIXCHECK_PENDING),
        "fixcheck_suspicious": False,
    }))
    (bug / "run_status.json").write_text(json.dumps({"status": "timeout"}))
    r = collect_project(str(tmp_path), "Math")[0]
    assert r["fixed"] is True                 # the verdict survived the kill
    assert r["fixcheck_invoked"] is True
    assert r["fixcheck_ran"] is False         # ... but FixCheck gave none
    assert r["fixcheck_analyzed"] is None


# ------------------------------------- the post-fix suite has a budget too
#
# gpt-oss Closure 74: the patch applied and compiled, then `defects4j test` ran
# for the whole 3 h per-bug timeout and the run was killed with no result.json.

def test_post_fix_budget_scales_with_the_pre_fix_suite():
    from Experiment import POST_FIX_TEST_FACTOR, post_fix_test_budget
    assert post_fix_test_budget(600) == 600 * POST_FIX_TEST_FACTOR


def test_post_fix_budget_is_clamped_both_ways():
    from Experiment import (POST_FIX_TEST_MAX_BUDGET, POST_FIX_TEST_MIN_BUDGET,
                            post_fix_test_budget)
    assert post_fix_test_budget(5) == POST_FIX_TEST_MIN_BUDGET        # quick suites
    assert post_fix_test_budget(None) == POST_FIX_TEST_MIN_BUDGET
    assert post_fix_test_budget(99_999) == POST_FIX_TEST_MAX_BUDGET   # slow suites


def test_post_fix_budget_plus_fixcheck_fits_in_the_per_bug_timeout():
    """Otherwise the per-bug timeout would still win and erase the verdict."""
    import re
    from Experiment import POST_FIX_TEST_MAX_BUDGET
    from FixCheckWrapper import DEFAULT_FIXCHECK_TIMEOUT
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = open(os.path.join(root, "scripts/runCampaign.sh"), encoding="utf-8").read()
    per_bug = int(re.search(r'^TIMEOUT="(\d+)"', script, re.M).group(1))
    assert POST_FIX_TEST_MAX_BUDGET + DEFAULT_FIXCHECK_TIMEOUT < per_bug


def test_bounded_command_wraps_in_coreutils_timeout():
    from Experiment import bounded_command
    assert bounded_command("defects4j test", 1800) == \
        "timeout --kill-after=30 1800 defects4j test"


def test_superseded_path_keeps_the_layout_under_results_old():
    from Experiment import superseded_path
    p = superseded_path("results/qwen3.6:35b/Math/Bug_13", "20260910T200000Z")
    assert p == "results/old/superseded/20260910T200000Z/qwen3.6:35b/Math/Bug_13"


def test_hang_marker_is_the_same_in_the_pipeline_and_the_verifier():
    """audit/rederive.py keeps its own copy on purpose (it imports nothing from
    Experiment.py); the two must never drift apart."""
    from Experiment import POST_FIX_HANG_MARKER as pipeline
    from audit.rederive import POST_FIX_HANG_MARKER as verifier
    assert pipeline == verifier
