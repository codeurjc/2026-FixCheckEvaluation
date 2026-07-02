"""
Fast, deterministic unit tests for Experiment's pure helpers: the trigger-based
fix criterion, the failing-test parser, and the Java trigger-method extraction.
No Docker, Ollama or network required.

Run with:

    .venv/bin/python -m pytest test/test_experiment_units.py -v
"""

from Experiment import (
    evaluate_fix,
    extract_java_method,
    extract_trigger_test_code,
    parse_failing_test_names,
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
