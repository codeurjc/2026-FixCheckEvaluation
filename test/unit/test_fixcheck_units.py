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

import argparse
import json

import pytest

from Experiment import extract_trigger_method_sources_by_class
from FixCheckWrapper import (
    _resolve_classpath,
    allocate_prefixes,
    build_fixcheck_properties,
    check_ollama_backend,
    copy_fixcheck_artifacts,
    count_mutable_literals,
    derive_seed,
    fixcheck_failure_log_path,
    generator_removes_assertions,
    group_triggers_by_class,
    literal_type_dir,
    missing_test_classes,
    needs_host_network,
    parse_fixcheck_report,
    parse_fixcheck_scores,
    parse_fixcheck_variations,
    parse_ollama_generator,
    plan_fixcheck_runs,
    resolve_ollama_backend,
    strip_defects4j_headers,
    summarize_fixcheck_runs,
    validate_assertion_generator,
    write_fixcheck_failure_logs,
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


# -------------------------------------------------------- mutable literal counts
#
# FixCheck mutates a literal only where its InputTransformer can reach it; a run
# planned for a type it cannot find dies with "No locals of type <T>" and no
# report. These pin the count to what FixCheck actually reaches.

def mutable(src, assertions_removed=True):
    """The literal types with at least one mutable literal."""
    return {t for t, n in count_mutable_literals(src, assertions_removed).items() if n}


def test_counts_string_literals():
    assert count_mutable_literals('String a = "abc"; foo("def", "ghi");')["java.lang.String"] == 3


def test_counts_int_literals():
    counts = count_mutable_literals("foo(1, 2, 3);")
    assert counts["int"] == 3 and counts["long"] == 0


def test_floating_point_literals_are_double_not_int():
    counts = count_mutable_literals("foo(1.5, 2.75, 3f, 4e10, .5);")
    assert counts["double"] == 5 and counts["int"] == 0


def test_counts_boolean_literals():
    assert count_mutable_literals("foo(true); bar(false); baz(true);")["boolean"] == 3


def test_long_suffix_counts_as_long():
    counts = count_mutable_literals("foo(100L, 200L, 300l);")
    assert counts["long"] == 3 and counts["int"] == 0


def test_hex_binary_and_underscored_literals_count_as_int():
    assert count_mutable_literals("foo(0x8000, 0b101, 1_000);")["int"] == 3


def test_nothing_mutable_without_literals():
    assert mutable("foo(bar, baz);") == set()
    assert mutable("") == set()


def test_digits_inside_string_or_char_literals_are_not_numbers():
    # The "1" in toString(1) is a real int literal; the "123" and the '7' are
    # inside a string and a char literal.
    counts = count_mutable_literals("foo(\"123\", '7', NumberUtils.toString(1));")
    assert counts["java.lang.String"] == 1 and counts["int"] == 1


# FixCheck refuses to mutate literals that only occur inside assertions
# (InputTransformer.isAssertion), so the count must ignore them too.

def test_ignores_literals_inside_assertions():
    # Lang 1's TestLang747 shape: nothing but assertEquals(...) lines.
    src = """\
public void TestLang747() {
    assertEquals(Integer.valueOf(0x8000), NumberUtils.createNumber("0x8000"));
    assertEquals(Integer.valueOf(0x80000), NumberUtils.createNumber("0x80000"));
}"""
    assert mutable(src) == set()


def test_only_literals_outside_assertions_count():
    # Strings dominate the method overall, but they all sit inside
    # assertions; only the int literal is actually mutable.
    src = """\
public void testThing() {
    int size = 42;
    assertEquals("aaa", f("bbb"));
    assertEquals("ccc", f("ddd"));
}"""
    assert mutable(src) == {"int"}


def test_every_assertion_call_name_counts_as_an_assertion():
    src = """\
public void testThing() {
    assertTrue(flag);
    assertFalse(other);
    assertNotNull("x");
    assertNotEquals("y", "z");
    fail("boom");
    check("nope");
}"""
    assert mutable(src) == set()


def test_qualified_assertion_calls_are_assertions_too():
    # FixCheck compares the method name only.
    src = """\
public void testThing() {
    Assert.assertEquals("aaa", f("bbb"));
    org.junit.Assert.assertTrue(g("ccc"));
}"""
    assert mutable(src) == set()


def test_ignores_literals_in_comments():
    # Lang 6's testEscapeSurrogatePairs is all assertions plus a comment
    # linking to ".../wiki/UTF-16"; that 16 must not be read as an int.
    src = """\
public void testEscapeSurrogatePairs() {
    // Examples from https://en.wikipedia.org/wiki/UTF-16
    assertEquals("a", escapeCsv("b"));
    /* block comment with 42 and "quoted" text */
    assertEquals("c", escapeCsv("d"));
}"""
    assert mutable(src) == set()


def test_block_statement_does_not_absorb_next_assertion():
    src = """\
public void testThing() {
    if (cond) {
        helper(7);
    }
    assertEquals("aaa", f("bbb"));
}"""
    assert mutable(src) == {"int"}


def test_assertion_nested_in_a_block_depends_on_the_generator():
    # With previous-assertion FixCheck keeps the assertions, and an if-block's
    # literals include those of the assertion inside it. Every other generator
    # removes the assertion first, at any depth.
    src = """\
public void testThing() {
    if (cond) {
        assertEquals("aaa", f("bbb"));
    }
}"""
    assert mutable(src, assertions_removed=False) == {"java.lang.String"}
    assert mutable(src, assertions_removed=True) == set()


def test_fail_message_inside_try_is_out_of_reach_once_assertions_are_removed():
    # Math 67's testQuinticMin: its only strings are fail(...) messages inside
    # a try. Counting them planned a String run FixCheck died on (No locals of
    # type java.lang.String); the other statements' literals still count.
    src = """\
public void testQuinticMin() {
    UnivariateRealOptimizer underlying = new BrentOptimizer(1e-9, 1e-14);
    underlying.setMaxEvaluations(40);
    try {
        minimizer.getOptima();
        fail("an exception should have been thrown");
    } catch (IllegalStateException ise) {
        // expected
    }
}"""
    counts = count_mutable_literals(src)
    assert counts["java.lang.String"] == 0
    assert counts["int"] == 1 and counts["double"] == 2


def test_semicolon_inside_string_does_not_split():
    # A ';' inside a string literal must not end the assertion statement,
    # which would leak the rest of it back into the mutable part.
    src = """\
public void testThing() {
    assertEquals("a;b", f("c;d"));
}"""
    assert mutable(src) == set()


def test_annotation_braces_are_not_the_method_body():
    src = """\
@SuppressWarnings({"unchecked"})
public void testThing() {
    helper(7);
}"""
    assert mutable(src) == {"int"}


# ------------------------------------------------------------ allocate_prefixes

def test_allocation_is_proportional_and_exact():
    shares = allocate_prefixes({"java.lang.String": 6, "int": 3, "boolean": 1}, total=100)
    assert sum(shares.values()) == 100
    assert shares["java.lang.String"] > shares["int"] > shares["boolean"] >= 1
    assert list(shares) == ["java.lang.String", "int", "boolean"]


def test_allocation_gives_every_present_type_at_least_one_prefix():
    shares = allocate_prefixes({"java.lang.String": 1000, "long": 1}, total=100)
    assert shares["long"] >= 1 and sum(shares.values()) == 100


def test_allocation_with_fewer_prefixes_than_types_favours_the_most_literals():
    assert allocate_prefixes({"java.lang.String": 1, "int": 5, "double": 3}, total=2) == {
        "int": 1, "double": 1,
    }


def test_allocation_breaks_ties_in_type_order():
    assert allocate_prefixes({"int": 1, "java.lang.String": 1}, total=3) == {
        "java.lang.String": 2, "int": 1,
    }


def test_allocation_is_empty_when_nothing_is_mutable():
    assert allocate_prefixes({"java.lang.String": 0, "int": 0}) == {}
    assert allocate_prefixes({"int": 3}, total=0) == {}


# ------------------------------------------------------ seeds and run layout

def test_seed_is_derived_from_the_run_and_repeatable():
    seed = derive_seed("Lang-12", "org.foo.ATest", "testA", "int")
    assert seed == derive_seed("Lang-12", "org.foo.ATest", "testA", "int")
    assert seed != derive_seed("Lang-12", "org.foo.ATest", "testA", "java.lang.String")
    assert 0 <= seed < 2 ** 63


def test_literal_type_dir_names():
    assert literal_type_dir("java.lang.String") == "String"
    assert literal_type_dir("int") == "int"


def test_only_previous_assertion_keeps_the_assertions():
    assert generator_removes_assertions("previous-assertion") is False
    for generator in ("assert-true", "codellama", "ollama:gpt-oss:120b@1995"):
        assert generator_removes_assertions(generator) is True


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
        # A report from before patch 0008 has no such column: not measured.
        "timed_out": None,
        # Nor this one before patch 0012.
        "assertion_generation_failed": None,
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


# ----------------------------------------------------------- plan_fixcheck_runs

def test_plan_splits_each_methods_budget_among_its_literal_types():
    sources = {"org.foo.ATest": {"testA": 'public void testA() { f("x", "y", 3); }'}}
    runs, skipped = plan_fixcheck_runs(["org.foo.ATest::testA"], sources, total=10)
    assert skipped == []
    assert [(r["method"], r["inputs_class"], r["num_prefixes"]) for r in runs] == [
        ("testA", "java.lang.String", 6), ("testA", "int", 4),
    ]


def test_plan_reports_the_methods_fixcheck_cannot_work_on():
    sources = {"org.foo.ATest": {
        "testA": "public void testA() { f(1); }",
        "testB": 'public void testB() { assertEquals("y", g()); }',
    }}
    triggers = ["org.foo.ATest::testA", "org.foo.ATest::testB", "org.foo.ATest::testC"]
    runs, skipped = plan_fixcheck_runs(triggers, sources, total=5)
    assert [(r["method"], r["inputs_class"], r["num_prefixes"]) for r in runs] == [
        ("testA", "int", 5),
    ]
    reasons = {s["method"]: s["reason"] for s in skipped}
    assert reasons["testB"].startswith("no mutable literal")
    # testC has no source in its own class: inherited, invisible to FixCheck.
    assert "inherited" in reasons["testC"]


def test_plan_with_a_forced_inputs_class_gives_it_the_whole_budget():
    sources = {"org.foo.ATest": {"testA": 'public void testA() { f("x", 3); }'}}
    runs, _ = plan_fixcheck_runs(["org.foo.ATest::testA"], sources, total=100, inputs_class="int")
    assert [(r["inputs_class"], r["num_prefixes"]) for r in runs] == [("int", 100)]


def test_plan_counts_with_the_generators_assertion_handling():
    sources = {"org.foo.ATest": {
        "testA": 'public void testA() { if (c) { assertEquals("x", f()); } }',
    }}
    kept, _ = plan_fixcheck_runs(["org.foo.ATest::testA"], sources, assertions_removed=False)
    removed, skipped = plan_fixcheck_runs(["org.foo.ATest::testA"], sources, assertions_removed=True)
    assert [r["inputs_class"] for r in kept] == ["java.lang.String"]
    assert removed == [] and skipped[0]["method"] == "testA"


# ------------------------------------------------------------------ _resolve_classpath

def test_resolve_classpath_anchors_relative_entries_to_workdir():
    resolved = _resolve_classpath("/wd", "target/classes:/abs/dep.jar:target/test-classes")
    assert resolved == "/wd/target/classes:/abs/dep.jar:/wd/target/test-classes"


def test_resolve_classpath_drops_empty_entries():
    assert _resolve_classpath("/wd", "target/classes::") == "/wd/target/classes"


# ------------------------------------------------------- Ollama-backed generators

def test_needs_host_network_only_for_ollama_generators():
    # These two post to a hardcoded http://localhost:11434, which under the
    # default bridge network is the container's own (empty) loopback.
    assert needs_host_network("codellama")
    assert needs_host_network("llama3.1")
    # These do not touch the host's localhost, so the default network is fine.
    assert not needs_host_network("previous-assertion")
    assert not needs_host_network("assert-true")
    assert not needs_host_network("gpt-3.5")


class _FakeContainer:
    """Stands in for a docker container, returning a canned exec result.

    Records the commands it was given, so a test can check *what* was probed
    and not only what the probe returned.
    """

    def __init__(self, exit_code, output):
        self._result = (exit_code, output.encode("utf-8"))
        self.commands = []

    def exec_run(self, command, workdir=None, demux=False):
        self.commands.append(command)
        return self._result


def _tags_json(*names):
    return json.dumps({"models": [{"name": n} for n in names]})


def test_check_ollama_backend_ok_when_exact_tag_present():
    container = _FakeContainer(0, _tags_json("codellama:latest", "other:7b"))
    assert check_ollama_backend(container, "codellama") is None


def test_check_ollama_backend_rejects_wrong_tag_of_right_model():
    # The crux: FixCheck hardcodes the bare name 'codellama', which Ollama
    # resolves to 'codellama:latest'. Having codellama:7b pulled is NOT
    # enough, and the failure is otherwise invisible until every prefix's
    # assertion call has already thrown.
    container = _FakeContainer(0, _tags_json("codellama:7b"))
    error = check_ollama_backend(container, "codellama")
    assert error and "codellama:latest" in error
    assert "ollama cp" in error, "the error should say how to fix it"


def test_check_ollama_backend_reports_unreachable_daemon():
    container = _FakeContainer(7, "")  # curl exit 7: connection refused
    error = check_ollama_backend(container, "codellama")
    assert error and "did not respond" in error


def test_check_ollama_backend_reports_unparsable_response():
    container = _FakeContainer(0, "<html>502 Bad Gateway</html>")
    error = check_ollama_backend(container, "codellama")
    assert error and "unexpected response" in error


# ------------------------------------------- generic 'ollama:<model>' generator
#
# These mirror OllamaProperty.parse() in
# fixcheck/src/main/java/org/imdea/fixcheck/properties/OllamaProperty.java;
# the two parsers must agree, since the Python side decides the container's
# network mode and probes the daemon before the jar ever reads the option.

@pytest.mark.parametrize("spec,model,host,port", [
    # The endpoint is split at '@' precisely because the model tag already
    # contains the ':' of <model>:<version>.
    ("ollama:gpt-oss:120b@1995", "gpt-oss:120b", "localhost", 1995),
    ("ollama:gpt-oss:120b", "gpt-oss:120b", "localhost", 11434),
    ("ollama:mistral", "mistral", "localhost", 11434),
    ("ollama:llama3.1:8b@remote.lan:1995", "llama3.1:8b", "remote.lan", 1995),
    ("ollama:mistral@remote.lan", "mistral", "remote.lan", 11434),
])
def test_parse_ollama_generator_reads_model_and_endpoint(spec, model, host, port):
    backend = parse_ollama_generator(spec)
    assert (backend.model, backend.host, backend.port) == (model, host, port)
    assert backend.base_url == f"http://{host}:{port}"


def test_parse_ollama_generator_resolves_the_tag_ollama_will_match():
    # A tagged model is used as-is; a bare name is what Ollama expands to
    # '<name>:latest', which is the tag /api/tags has to list.
    assert parse_ollama_generator("ollama:gpt-oss:120b").wanted_tag == "gpt-oss:120b"
    assert parse_ollama_generator("ollama:mistral").wanted_tag == "mistral:latest"


def test_parse_ollama_generator_ignores_the_fixed_option_keys():
    assert parse_ollama_generator("previous-assertion") is None
    assert parse_ollama_generator("codellama") is None


@pytest.mark.parametrize("spec", [
    "ollama",             # no model at all
    "ollama:",            # ditto
    "ollama:m@",          # nothing after the endpoint separator
    "ollama:m@host:abc",  # port is not a number
    "ollama:m@99999",     # port out of range
])
def test_parse_ollama_generator_rejects_malformed_specs(spec):
    with pytest.raises(ValueError):
        parse_ollama_generator(spec)


def test_resolve_ollama_backend_covers_the_legacy_generators():
    # The two hardcoded ones always mean localhost:11434 with a bare tag.
    backend = resolve_ollama_backend("codellama")
    assert backend.base_url == "http://localhost:11434"
    assert backend.wanted_tag == "codellama:latest"
    assert resolve_ollama_backend("previous-assertion") is None


def test_needs_host_network_follows_the_configured_host():
    # Host networking exists only to make the *host's* loopback reachable, so
    # a daemon named by a remote address does not need it.
    assert needs_host_network("ollama:gpt-oss:120b@1995")
    assert not needs_host_network("ollama:gpt-oss:120b@remote.lan:1995")
    assert not needs_host_network("previous-assertion")


def test_check_ollama_backend_probes_the_configured_port():
    container = _FakeContainer(0, _tags_json("gpt-oss:120b"))
    assert check_ollama_backend(container, "ollama:gpt-oss:120b@1995") is None
    assert "http://localhost:1995/api/tags" in container.commands[-1]


def test_check_ollama_backend_reports_a_model_absent_from_the_daemon():
    container = _FakeContainer(0, _tags_json("gpt-oss:20b"))
    error = check_ollama_backend(container, "ollama:gpt-oss:120b@1995")
    assert error and "gpt-oss:120b" in error
    # The 'ollama cp' advice belongs to the generators with a hardcoded tag;
    # here the tag is the user's own choice.
    assert "ollama cp" not in error


def test_validate_assertion_generator_accepts_both_forms():
    assert validate_assertion_generator("previous-assertion") == "previous-assertion"
    assert validate_assertion_generator("ollama:gpt-oss:120b@1995") == "ollama:gpt-oss:120b@1995"


@pytest.mark.parametrize("value", ["nope", "ollama:m@host:abc"])
def test_validate_assertion_generator_rejects_the_rest(value):
    with pytest.raises(argparse.ArgumentTypeError):
        validate_assertion_generator(value)


# ------------------------------------------------ FixCheck's own time budget
#
# FixCheck runs mutated prefixes with no timeout, and role-blind mutation of an
# int can make one effectively unbounded. Math 10 and 13 hung in FixCheck for
# hours in both campaigns until the per-bug timeout killed the run -- after the
# patch had already passed every test -- so plausible fixes were lost.

def test_fixcheck_command_is_bounded_by_default():
    from FixCheckWrapper import DEFAULT_FIXCHECK_TIMEOUT, fixcheck_command

    cmd = fixcheck_command("/cp", "/run/fixcheck.properties")
    assert cmd.startswith(f"timeout --kill-after=30 {DEFAULT_FIXCHECK_TIMEOUT} java ")
    assert cmd.endswith("org.imdea.fixcheck.FixCheck -p /run/fixcheck.properties")


def test_fixcheck_command_can_be_unbounded():
    from FixCheckWrapper import fixcheck_command

    assert fixcheck_command("/cp", "/p", 0).startswith("java ")
    assert fixcheck_command("/cp", "/p", None).startswith("java ")


def test_a_runs_budget_covers_its_prefixes_at_the_measured_p99():
    """The run budget is a safety net, not a working limit.

    Measured on the campaign of record: a prefix runs in 0.7 s and a model call
    returns in 46 s at the 99th percentile. A run of the default size whose
    every prefix hit both would still end inside the budget.
    """
    from FixCheckWrapper import (
        DEFAULT_FIXCHECK_LLM_TIMEOUT, DEFAULT_FIXCHECK_PREFIX_TIMEOUT,
        DEFAULT_FIXCHECK_PREFIXES, DEFAULT_FIXCHECK_TIMEOUT,
    )

    assert DEFAULT_FIXCHECK_PREFIXES * (0.7 + 46) < DEFAULT_FIXCHECK_TIMEOUT
    assert 0.7 < DEFAULT_FIXCHECK_PREFIX_TIMEOUT < DEFAULT_FIXCHECK_TIMEOUT
    assert 46 < DEFAULT_FIXCHECK_LLM_TIMEOUT < DEFAULT_FIXCHECK_TIMEOUT


def test_defaults_follow_fixchecks_own_evaluation():
    """100 prefixes and 0.4, as agreed with FixCheck's author."""
    from FixCheckWrapper import DEFAULT_FIXCHECK_PREFIXES, DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD

    assert DEFAULT_FIXCHECK_PREFIXES == 100
    assert DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD == 0.4


def test_wrapper_carries_its_timeouts():
    from FixCheckWrapper import (
        DEFAULT_FIXCHECK_LLM_TIMEOUT, DEFAULT_FIXCHECK_PREFIX_TIMEOUT,
        DEFAULT_FIXCHECK_TIMEOUT, FixCheckWrapper,
    )

    wrapper = FixCheckWrapper()
    assert wrapper.timeout_seconds == DEFAULT_FIXCHECK_TIMEOUT
    assert wrapper.prefix_timeout_seconds == DEFAULT_FIXCHECK_PREFIX_TIMEOUT
    assert wrapper.llm_timeout_seconds == DEFAULT_FIXCHECK_LLM_TIMEOUT
    assert FixCheckWrapper(timeout_seconds=60).timeout_seconds == 60


# ---------------------------------------------------------- pre-fix traces

def test_strip_defects4j_headers_drops_only_the_header_lines():
    trace = (
        "--- org.foo.ATest::testA\n"
        "java.lang.AssertionError: boom\n"
        "\tat org.foo.ATest.testA(ATest.java:10)\n"
    )
    assert strip_defects4j_headers(trace) == (
        "java.lang.AssertionError: boom\n\tat org.foo.ATest.testA(ATest.java:10)"
    )


def test_failure_logs_are_written_per_method_without_headers(tmp_path):
    triggers = ["org.foo.ATest::testA", "org.foo.ATest::testB"]
    raw = {
        "org.foo.ATest::testA": ("cmd", "--- org.foo.ATest::testA\nError: A\n"),
        "org.foo.ATest::testB": ("cmd", "--- org.foo.ATest::testB\nError: B\n"),
    }
    paths = write_fixcheck_failure_logs(str(tmp_path), triggers, raw)
    assert paths["org.foo.ATest::testA"] == fixcheck_failure_log_path(
        str(tmp_path), "org.foo.ATest", "testA"
    )
    assert open(paths["org.foo.ATest::testA"], encoding="utf-8").read() == "Error: A"
    assert open(paths["org.foo.ATest::testB"], encoding="utf-8").read() == "Error: B"


# ------------------------------------------------ properties, reports and logs

def test_build_fixcheck_properties_writes_the_optional_properties_when_given():
    text = build_fixcheck_properties(
        test_classes_path="p", test_class="C", test_methods=["m"], test_classes_src="s",
        failure_log_path="f", inputs_class="int", num_prefixes=40,
        assertion_generator="ollama:m@1",
        subject_classpath="/wd/classes:/wd/tests", output_dir="/wd/run/out", seed=42,
        prefix_timeout_seconds=60, ollama_timeout_seconds=120,
        ollama_temperature=0, ollama_seed=42,
    )
    assert text.strip("\n").split("\n")[8:] == [
        "subject-classpath=/wd/classes:/wd/tests",
        "output-dir=/wd/run/out",
        "seed=42",
        "prefix-timeout-seconds=60",
        "ollama-timeout-seconds=120",
        "ollama-temperature=0",
        "ollama-seed=42",
    ]


def test_parse_fixcheck_report_reads_timed_out_prefixes():
    report = parse_fixcheck_report(
        REPORT_HEADER + ",timed_out_prefixes\nC,1,int,,5,6,7,10,4,2,1,2\n"
    )
    assert report["timed_out"] == 2 and report["non_compiling"] == 1


def test_parse_fixcheck_report_without_the_column_has_no_timed_out_measurement():
    report = parse_fixcheck_report(REPORT_HEADER + "\nC,1,int,,5,6,7,10,4,2,1\n")
    assert report["timed_out"] is None and report["non_compiling"] == 3
    assert report["assertion_generation_failed"] is None


def test_parse_fixcheck_report_reads_assertion_generation_failures():
    report = parse_fixcheck_report(
        REPORT_HEADER + ",timed_out_prefixes,assertion_generation_failed_prefixes\n"
        "C,1,int,,5,6,7,10,3,2,1,1,2\n"
    )
    assert report["assertion_generation_failed"] == 2
    # Not a non-compiling prefix: the buckets still partition every prefix.
    assert report["non_compiling"] == 1


SAMPLE_LOG = """\
> FixCheck
====== GENERATION ======
PREFIX 1 of 3
---> transformer: InputTransformer
---> transformation: ["--prefix":java.lang.String] replaced by ["--pref":java.lang.String]
---> prefix execution without assertions
---> prefix crashed

---> Checking similarity
Original failure:
x
Current failure:
y
---> failure similarity: 0.8123
PREFIX 2 of 3
---> transformation: [1:int] replaced by [1:java.lang.Integer]
---> prefix execution without assertions
---> assertion generator: OllamaGenerator
---> prefix execution with assertions
---> prefix failed assertion
---> failure similarity: 0.25
PREFIX 3 of 3
---> transformation: [30:int] replaced by [86:java.lang.Integer]
---> prefix timed out after 60s
---> prefix timed out
PREFIX 4 of 4
---> transformation: [2:int] replaced by [7:java.lang.Integer]
---> prefix execution without assertions
---> assertion generator: OllamaGenerator
---> assertion generation failed: java.lang.RuntimeException: Ollama call failed
---> time: 300012ms
---> prefix assertion generation failed
====== OUTPUT ======
"""


def test_parse_fixcheck_variations_reads_each_prefix():
    variations = parse_fixcheck_variations(SAMPLE_LOG)
    assert [v["outcome"] for v in variations] == [
        "crashed", "failed assertion", "timed out", "assertion generation failed",
    ]
    assert variations[3]["score"] is None
    assert variations[0]["original"] == '"--prefix"'
    assert variations[0]["replacement"] == '"--pref"'
    assert variations[0]["score"] == pytest.approx(0.8123)
    assert variations[1]["identity"] and not variations[0]["identity"]
    assert variations[1]["assertions_generated"] and not variations[0]["assertions_generated"]
    assert variations[2]["score"] is None


def test_parse_fixcheck_variations_of_an_empty_log():
    assert parse_fixcheck_variations("") == []


# ------------------------------------------------------------ the verdict

def _run(method="testA", ok=True, report=None, variations=(), timed_out=False):
    default_report = {
        "total": 3, "passing": 1, "crashing": 1, "assertion_failing": 1,
        "non_compiling": 0, "timed_out": 0,
    }
    return {
        "test_class": "org.foo.ATest", "method": method, "inputs_class": "int",
        "ok": ok, "timed_out": timed_out,
        "report": report if report is not None else (default_report if ok else None),
        "variations": list(variations),
    }


def test_a_scored_prefix_at_the_threshold_flags_the_patch():
    variations = [
        {"outcome": "crashed", "score": 0.4, "identity": False},
        {"outcome": "passed", "score": None, "identity": True},
    ]
    summary = summarize_fixcheck_runs([_run(variations=variations)], [], similarity_threshold=0.4)
    assert summary["suspicious"] is True
    assert summary["max_failure_similarity"] == 0.4
    assert summary["analyzed_runs"] == 1 and summary["analyzed_test_classes"] == 1
    assert summary["failing_prefixes"] == 2
    assert summary["identity_prefixes"] == 1 and summary["identity_failing_prefixes"] == 0


def test_runs_without_a_report_measure_nothing():
    variations = [{"outcome": "crashed", "score": 0.9, "identity": False}]
    summary = summarize_fixcheck_runs(
        [_run(ok=False, variations=variations, timed_out=True)], [{"method": "testB"}], 0.4
    )
    assert summary["suspicious"] is False
    assert summary["analyzed_runs"] == 0 and summary["timed_out_runs"] == 1
    assert summary["max_failure_similarity"] is None      # nothing measured, not 0.0
    assert summary["timed_out_prefixes"] is None
    assert summary["skipped_methods"] == 1


def test_failing_identity_prefixes_are_counted_as_noise():
    variations = [{"outcome": "failed assertion", "score": 0.1, "identity": True}]
    summary = summarize_fixcheck_runs([_run(variations=variations)], [], 0.4)
    assert summary["identity_failing_prefixes"] == 1 and summary["suspicious"] is False


def test_timed_out_prefixes_is_unknown_for_reports_without_the_column():
    old = {"total": 3, "passing": 3, "crashing": 0, "assertion_failing": 0,
           "non_compiling": 0, "timed_out": None}
    assert summarize_fixcheck_runs([_run(report=old)], [], 0.4)["timed_out_prefixes"] is None


def test_assertion_generation_failures_are_summed_and_never_scored():
    report = {"total": 3, "passing": 1, "crashing": 0, "assertion_failing": 0,
              "non_compiling": 0, "timed_out": 0, "assertion_generation_failed": 2}
    variations = [{"outcome": "assertion generation failed", "score": None, "identity": False}] * 2
    summary = summarize_fixcheck_runs([_run(report=report, variations=variations)], [], 0.4)
    assert summary["assertion_generation_failed_prefixes"] == 2
    assert summary["failing_prefixes"] == 0 and summary["scored_prefixes"] == 0
    # A report from before patch 0012 did not measure it.
    assert summarize_fixcheck_runs([_run()], [], 0.4)["assertion_generation_failed_prefixes"] is None


def test_copy_fixcheck_artifacts_keeps_one_directory_per_run(tmp_path):
    run_dir = tmp_path / "wd" / ".fixcheck" / "runs" / "x"
    (run_dir / "fixcheck-output").mkdir(parents=True)
    (run_dir / "fixcheck.log").write_text("log")
    (run_dir / "fixcheck-output" / "report.csv").write_text("csv")
    result = {"runs": [
        {"test_class": "org.foo.ATest", "method": "testA",
         "inputs_class": "java.lang.String", "run_dir": str(run_dir)},
        {"test_class": "org.foo.ATest", "method": "testB",
         "inputs_class": "int", "run_dir": None},
    ]}
    dest = tmp_path / "out"
    assert copy_fixcheck_artifacts(result, str(dest)) == 1
    copied = dest / "ATest" / "testA" / "String"
    assert (copied / "fixcheck.log").read_text() == "log"
    assert (copied / "fixcheck-output" / "report.csv").exists()


def test_missing_test_classes_finds_the_uncompiled_trigger_classes(tmp_path):
    # Right after `defects4j compile`, Mockito's test-classes directory is
    # empty; FixCheck's prefixes then cannot compile against TestBase & co.
    compiled = tmp_path / "org" / "foo"
    compiled.mkdir(parents=True)
    (compiled / "ATest.class").write_bytes(b"")
    triggers = ["org.foo.ATest::testA", "org.foo.BTest::testB", "org.foo.BTest::testC"]
    assert missing_test_classes(str(tmp_path), triggers) == ["org.foo.BTest"]
