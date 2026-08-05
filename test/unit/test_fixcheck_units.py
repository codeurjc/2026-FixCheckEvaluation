"""
Fast, deterministic unit tests for the FixCheck integration's pure helpers:
grouping trigger tests by class, the ``inputs-class`` heuristic, the
``.properties`` file renderer, and the ``report.csv`` / ``scores-failing-
tests.csv`` parsers. No Docker, Ollama or network required.

The pure FixCheck-specific helpers live in ``FixCheckWrapper.py`` (kept apart
from ``Experiment.py`` precisely so they can be tested on their own, without
any Docker/Defects4J pipeline machinery); ``extract_trigger_method_sources_by_class``
stays in ``Experiment.py`` since it shares ``extract_java_method`` with the
(fixcheck-unrelated) ``--include-test-code`` prompt path.

Run with:

    .venv/bin/python -m pytest test/unit/test_fixcheck_units.py -v
"""

from Experiment import extract_trigger_method_sources_by_class
from FixCheckWrapper import (
    _resolve_classpath,
    build_fixcheck_properties,
    group_triggers_by_class,
    parse_fixcheck_report,
    parse_fixcheck_scores,
    select_fixcheck_inputs,
)

# Verbatim header from fixcheck/src/main/java/org/imdea/fixcheck/writer/ReportWriter.java.
REPORT_HEADER = (
    "test_class,input_prefixes,inputs_class,target_class,prefixes_gen_time,"
    "assertions_gen_time,prefixes_running_time,output_prefixes,passing_prefixes,"
    "crashing_prefixes,assertion_failing_prefixes"
)


# ------------------------------------------------------- group_triggers_by_class

def test_group_triggers_by_class_mixed_classes_preserve_order():
    triggers = [
        "org.foo.ATest::testX",
        "org.bar.BTest::testY",
        "org.foo.ATest::testZ",
    ]
    grouped = group_triggers_by_class(triggers)
    assert list(grouped.keys()) == ["org.foo.ATest", "org.bar.BTest"]
    assert grouped["org.foo.ATest"] == ["testX", "testZ"]
    assert grouped["org.bar.BTest"] == ["testY"]


def test_group_triggers_by_class_dedupes_duplicate_methods():
    triggers = ["org.foo.ATest::testX", "org.foo.ATest::testX", "org.foo.ATest::testY"]
    assert group_triggers_by_class(triggers) == {"org.foo.ATest": ["testX", "testY"]}


def test_group_triggers_by_class_empty():
    assert group_triggers_by_class([]) == {}


# ------------------------------------------------------------- inputs-class

def infer(src):
    """The single-method form of select_fixcheck_inputs: its chosen type."""
    return select_fixcheck_inputs(["m"], {"m": src})[0]


def test_infer_inputs_class_string_dominated():
    src = 'String a = "abc"; foo("def", "ghi");'
    assert infer(src) == "java.lang.String"


def test_infer_inputs_class_int_only():
    assert infer("foo(1, 2, 3);") == "int"


def test_infer_inputs_class_float_prefers_double():
    assert infer("foo(1.5, 2.75);") == "double"


def test_infer_inputs_class_boolean():
    assert infer("foo(true); bar(false); baz(true);") == "boolean"


def test_infer_inputs_class_long_suffix():
    assert infer("foo(100L, 200L, 300L);") == "long"


def test_infer_inputs_class_none_when_no_literals():
    assert infer("foo(bar, baz);") is None


def test_infer_inputs_class_none_when_empty():
    assert infer("") is None


def test_infer_inputs_class_digits_inside_string_not_double_counted():
    # The "1" in toString(1) is a real int literal; the "123" is inside a
    # string literal and must not also be counted as one -- otherwise they
    # would tie and java.lang.String would win on the tie-break instead.
    assert infer('foo("123", NumberUtils.toString(1));') == "java.lang.String"


# FixCheck refuses to mutate literals that only occur inside assertions
# (InputTransformer.isAssertion), so the heuristic must ignore them too --
# otherwise it proposes a type FixCheck then dies looking for.

def test_infer_inputs_class_ignores_literals_inside_assertions():
    # Lang 1's TestLang747 shape: nothing but assertEquals(...) lines. There
    # is no inputs-class FixCheck could use, so the heuristic must say so
    # rather than proposing java.lang.String.
    src = """\
public void TestLang747() {
    assertEquals(Integer.valueOf(0x8000), NumberUtils.createNumber("0x8000"));
    assertEquals(Integer.valueOf(0x80000), NumberUtils.createNumber("0x80000"));
}"""
    assert infer(src) is None


def test_infer_inputs_class_prefers_type_outside_assertions():
    # Strings dominate the method overall, but they all sit inside
    # assertions; only the int literal is actually mutable.
    src = """\
public void testThing() {
    int size = 42;
    assertEquals("aaa", f("bbb"));
    assertEquals("ccc", f("ddd"));
}"""
    assert infer(src) == "int"


def test_infer_inputs_class_counts_all_assertion_call_names():
    src = """\
public void testThing() {
    assertTrue(flag);
    assertFalse(other);
    assertNotNull("x");
    assertNotEquals("y", "z");
    fail("boom");
    check("nope");
}"""
    assert infer(src) is None


def test_infer_inputs_class_ignores_literals_in_comments():
    # Lang 6's testEscapeSurrogatePairs is all assertions plus a comment
    # linking to ".../wiki/UTF-16"; that 16 must not be read as an int
    # literal, which would wrongly make the method look mutable.
    src = """\
public void testEscapeSurrogatePairs() {
    // Examples from https://en.wikipedia.org/wiki/UTF-16
    assertEquals("a", escapeCsv("b"));
    /* block comment with 42 and "quoted" text */
    assertEquals("c", escapeCsv("d"));
}"""
    assert infer(src) is None


def test_infer_inputs_class_block_statement_does_not_absorb_next_assertion():
    # An if-block ends at '}' with no ';'. If the splitter glued it to the
    # following assertion, that assertion's strings would count as mutable
    # and the answer would flip to java.lang.String.
    src = """\
public void testThing() {
    if (cond) {
        helper(7);
    }
    assertEquals("aaa", f("bbb"));
}"""
    assert infer(src) == "int"


def test_infer_inputs_class_counts_assertions_nested_in_a_block():
    # FixCheck's findAll is recursive over non-assertion statements, so
    # literals inside an assertion nested in an if-block *are* reachable.
    src = """\
public void testThing() {
    if (cond) {
        assertEquals("aaa", f("bbb"));
    }
}"""
    assert infer(src) == "java.lang.String"


def test_infer_inputs_class_semicolon_inside_string_does_not_split():
    # A ';' inside a string literal must not end the assertion statement,
    # which would leak the rest of it back into the mutable part.
    src = """\
public void testThing() {
    assertEquals("a;b", f("c;d"));
}"""
    assert infer(src) is None


# --------------------------------------------------------- build_fixcheck_properties

def test_build_fixcheck_properties_exact_keys_and_values():
    text = build_fixcheck_properties(
        test_classes_path="/wd/target/test-classes",
        test_class="org.apache.commons.lang3.math.NumberUtilsTest",
        test_methods=["testFoo", "testBar"],
        test_classes_src="/wd/src/test/java",
        failure_log_path="/wd/.fixcheck/NumberUtilsTest.failing_tests",
        inputs_class="int",
        num_prefixes=25,
        assertion_generator="previous-assertion",
    )
    assert text.endswith("\n")
    lines = text.strip("\n").split("\n")
    assert lines == [
        "test-classes-path=/wd/target/test-classes",
        "test-class=org.apache.commons.lang3.math.NumberUtilsTest",
        "test-methods=testFoo:testBar",
        "test-classes-src=/wd/src/test/java",
        "test-failure-trace-log=/wd/.fixcheck/NumberUtilsTest.failing_tests",
        "inputs-class=int",
        "number-of-prefixes=25",
        "assertion-generator=previous-assertion",
    ]


def test_build_fixcheck_properties_single_method_no_trailing_colon():
    text = build_fixcheck_properties(
        test_classes_path="p", test_class="C", test_methods=["onlyOne"],
        test_classes_src="s", failure_log_path="f", inputs_class="boolean",
        num_prefixes=1, assertion_generator="assert-true",
    )
    assert "test-methods=onlyOne\n" in text


# ------------------------------------------------------------ parse_fixcheck_report

def test_parse_fixcheck_report_happy_path():
    csv_text = REPORT_HEADER + "\n" + "org.foo.ATest,2,int,org.foo.A,120,45,300,20,15,3,2\n"
    report = parse_fixcheck_report(csv_text)
    assert report == {
        "test_class": "org.foo.ATest",
        "input_prefixes": 2,
        "inputs_class": "int",
        "target_class": "org.foo.A",
        "prefixes_gen_time_ms": 120,
        "assertions_gen_time_ms": 45,
        "prefixes_running_time_ms": 300,
        "total": 20,
        "passing": 15,
        "crashing": 3,
        "assertion_failing": 2,
        "non_compiling": 0,
    }


def test_parse_fixcheck_report_non_compiling_arithmetic():
    # 10 generated - 4 passing - 1 crashing - 1 assertion-failing = 4 that
    # never compiled at all (report.csv has no dedicated column for these).
    csv_text = REPORT_HEADER + "\n" + "org.foo.ATest,1,int,org.foo.A,10,5,50,10,4,1,1\n"
    report = parse_fixcheck_report(csv_text)
    assert report["non_compiling"] == 4


def test_parse_fixcheck_report_empty_is_none():
    assert parse_fixcheck_report("") is None
    assert parse_fixcheck_report("   \n") is None


def test_parse_fixcheck_report_header_only_is_none():
    assert parse_fixcheck_report(REPORT_HEADER + "\n") is None


def test_parse_fixcheck_report_malformed_row_is_none():
    # Fewer data columns than the header -- e.g. a partially-written file.
    csv_text = REPORT_HEADER + "\n" + "org.foo.ATest,2,int\n"
    assert parse_fixcheck_report(csv_text) is None


# ----------------------------------------------------------- parse_fixcheck_scores

def test_parse_fixcheck_scores_with_header():
    csv_text = "prefix,score\nATest_1,0.42\nATest_2,0.91\n"
    assert parse_fixcheck_scores(csv_text) == [0.42, 0.91]


def test_parse_fixcheck_scores_without_header():
    csv_text = "ATest_1,0.1\nATest_2,0.2\n"
    assert parse_fixcheck_scores(csv_text) == [0.1, 0.2]


def test_parse_fixcheck_scores_blank_lines_ignored():
    csv_text = "prefix,score\n\nATest_1,0.5\n\n\nATest_2,0.75\n"
    assert parse_fixcheck_scores(csv_text) == [0.5, 0.75]


def test_parse_fixcheck_scores_empty_file():
    assert parse_fixcheck_scores("") == []
    assert parse_fixcheck_scores(None) == []


# ------------------------------------------- extract_trigger_method_sources_by_class

JAVA_SRC = """\
public class FooTest {
    @Test
    public void testA() {
        assertEquals(1, 1);
    }

    @Test
    public void testB() {
        assertEquals(2, 2);
    }
}
"""


def test_extract_trigger_method_sources_by_class_keeps_only_trigger_methods():
    triggers = ["org.foo.FooTest::testA", "org.foo.FooTest::testB"]
    sources = [("src/test/java/org/foo/FooTest.java", JAVA_SRC)]
    result = extract_trigger_method_sources_by_class(triggers, sources)
    assert set(result.keys()) == {"org.foo.FooTest"}
    per_method = result["org.foo.FooTest"]
    assert set(per_method) == {"testA", "testB"}
    # Each entry holds that method's own source, not the whole file.
    assert "testA" in per_method["testA"] and "testB" not in per_method["testA"]
    assert "testB" in per_method["testB"] and "testA" not in per_method["testB"]


def test_extract_trigger_method_sources_by_class_omits_inherited_method():
    # Lang 10's FastDateFormat_ParserTest extends FastDateParserTest and
    # inherits testLANG_831 without declaring it. FixCheck parses only the
    # named class's file, so the method must be reported as absent rather
    # than silently falling back to the whole file.
    subclass = "public class BarTest extends FooTest {\n    // nothing here\n}\n"
    result = extract_trigger_method_sources_by_class(
        ["org.foo.BarTest::testA"], [("src/test/java/org/foo/BarTest.java", subclass)]
    )
    assert result == {"org.foo.BarTest": {}}


def test_extract_trigger_method_sources_by_class_omits_missing_file():
    result = extract_trigger_method_sources_by_class(["org.foo.FooTest::testA"], [])
    assert result == {}


# ------------------------------------------------------- select_fixcheck_inputs

def test_select_fixcheck_inputs_drops_unmutable_methods():
    # FixCheck aborts the whole run if any listed method has no literal of
    # inputs-class, so testB must not be passed along with testA.
    sources = {
        "testA": 'public void testA() { f("x"); }',
        "testB": 'public void testB() { assertEquals("y", g()); }',
    }
    inputs_class, usable = select_fixcheck_inputs(["testA", "testB"], sources)
    assert inputs_class == "java.lang.String"
    assert usable == ["testA"]


def test_select_fixcheck_inputs_prefers_type_covering_most_methods():
    sources = {
        "testA": "public void testA() { f(1); }",
        "testB": "public void testB() { g(2); }",
        "testC": 'public void testC() { h("s"); }',
    }
    inputs_class, usable = select_fixcheck_inputs(["testA", "testB", "testC"], sources)
    assert inputs_class == "int"
    assert usable == ["testA", "testB"]


def test_select_fixcheck_inputs_none_when_nothing_mutable():
    sources = {"testA": 'public void testA() { assertEquals("y", g()); }'}
    assert select_fixcheck_inputs(["testA"], sources) == (None, [])


def test_select_fixcheck_inputs_none_when_source_missing():
    # An inherited method has no entry in method_sources at all.
    assert select_fixcheck_inputs(["testA"], {}) == (None, [])


# ------------------------------------------------------------------ _resolve_classpath

def test_resolve_classpath_anchors_relative_entries_to_workdir():
    resolved = _resolve_classpath("/wd", "target/classes:/abs/dep.jar:target/test-classes")
    assert resolved == "/wd/target/classes:/abs/dep.jar:/wd/target/test-classes"


def test_resolve_classpath_drops_empty_entries():
    assert _resolve_classpath("/wd", "target/classes::") == "/wd/target/classes"
