"""
Integration test: run FixCheck against Defects4J bugs' *developer* fixes.

Unlike the LLM-generated fixes exercised by ``test_experiment_lang1.py`` /
``test_benchmark_lang1.py``, this applies the actual upstream patch (checked
out as the ``<id>f`` "fixed" revision and diffed against ``<id>b``) so the
result does not depend on LLM luck. It checks that FixCheck runs end to end on
a patch known to be correct and that its verdict is well-formed -- not that the
verdict is "not suspicious": correct fixes can be flagged (Math 69 is), and how
often is measured by ``replay_fixcheck.py --target devfix``, not asserted here.

The test is parametrized over two axes:

- **the subject**, from :data:`FIXCHECK_BUGS` -- add a candidate by appending
  a ``(project, bug_id)`` pair there;
- **the assertion generator**, from ``--fixcheck-assertions`` (comma-separated,
  default ``previous-assertion``), so the same subjects can be compared under
  several generators in one go.

They are crossed, and each combination gets its own container run (~2 min,
more when the generator calls an LLM) shared by the four checks below.

Which generator is used is not a detail. ``previous-assertion`` reuses the
trigger test's own assertions and ``ollama:<model>`` has one written by an LLM,
while ``assert-true`` only appends a vacuous ``assertTrue(true)`` -- under that
last one ``passing`` counts say nothing and a crash is the only detectable
failure. See docs/fixcheck-verdict-limitations.md, which also records the two
upstream defects (since patched here) that used to strip the assertions from
*every* generator's prefixes.

Not every Defects4J bug is a usable FixCheck subject (see *Not every bug is a
FixCheck subject* in README.md), so a listed bug has three possible outcomes:

- **passed** -- FixCheck analyzed at least one of the bug's trigger methods
  and reached a well-formed verdict. Individual methods it declined to
  attempt are tolerated, since a bug can mix the two: Math 69's
  ``SpearmansRankCorrelationTest`` inherits ``testPValueNearZero`` from
  ``PearsonsCorrelationTest``, which is analyzed normally.
- **skipped** -- FixCheck ran cleanly but had nothing to analyze at all:
  *every* trigger class was declined because its literals only occur inside
  assertions (Lang 1's ``NumberUtilsTest``) or its trigger method is
  inherited and therefore invisible to FixCheck (Lang 10's
  ``FastDateFormat_ParserTest``). That is documented upstream behavior, not a
  defect, so it is reported as a skip with the reason rather than a failure.
- **failed** -- anything else: the integration, or FixCheck itself, broke.

Every run's artifacts are copied to
``logs/test/<Project>_<BugId>/<generator>/`` for manual inspection -- one
directory per FixCheck run, ``<TestClass>/<method>/<literal type>/``, most
usefully its ``fixcheck.log`` (the mutations, the prompts and the assertions the
generator produced) and ``fixcheck-output/`` (the generated prefix sources,
``report.csv`` and ``scores-failing-tests.csv``). Keying by generator
keeps two of them comparable side by side for the same bug; each directory is
wiped at the start of its own run so it never mixes results.

Requires a running Docker daemon with the ``defects4j:3.0.1`` image and the
FixCheck jar built (``bash scripts/buildFixcheck.sh``). Both are checked
independently of Ollama/any LLM backend -- with the default
``previous-assertion`` generator this test never calls one. Skipped
automatically when either prerequisite is missing.

Run with:

    .venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s

    # a single subject
    .venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s -k Lang-12

    # compare two assertion generators on every subject
    .venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s \
        --fixcheck-assertions previous-assertion,codellama
"""

import argparse
import os

import pytest

from Experiment import DEFECTS4J_IMAGE, FIXCHECK_JAR
from FixCheckWrapper import validate_assertion_generator
from fixcheck_devfix_pipeline import (
    LOGS_DIR,
    docker_image_available,
    fixcheck_on_developer_fix,
)

# Candidate subjects, as ``(project, bug_id)``. Extend this list to try a new
# bug; each entry becomes its own parametrized run (test id ``<Project>-<id>``).
#
# Lang 12 is the reference subject: its ``RandomStringUtilsTest`` declares
# inputs in ordinary statements, so it exercises the path that actually
# produces a report, and its second trigger method (``testLANG805``) has no
# mutable literal -- covering the per-method filtering that keeps one awkward
# method from aborting the whole class.
FIXCHECK_BUGS = [
    ("Lang", "12"),
    ("Math", "69"),
    # ("Lang", "1"),   # verified skip: NumberUtilsTest is all assertions
]

NUM_PREFIXES = 5
# Deliberately stricter than the campaign's 0.4: this test checks that the
# integration works, and a flag on the developer's own fix at 0.8 points at the
# harness. How often 0.4 flags a correct patch is a measurement, not a test.
SIMILARITY_THRESHOLD = 0.8


def pytest_generate_tests(metafunc):
    """Turn ``--fixcheck-assertions`` into a second parametrization axis.

    Crossed with :data:`FIXCHECK_BUGS`, so ``--fixcheck-assertions
    previous-assertion,codellama`` runs every subject under both generators
    and each combination keeps its own log directory. Done here rather than
    with a static ``params=`` list because the choice is a run-time one: an
    LLM-backed generator costs a model call per prefix, so which generators
    are worth paying for depends on what is being investigated.
    """
    if "assertion_generator" not in metafunc.fixturenames:
        return
    raw = metafunc.config.getoption("--fixcheck-assertions")
    generators = [g.strip() for g in raw.split(",") if g.strip()]
    for generator in generators:
        # Fail at collection rather than after a couple of minutes of
        # container setup, which is when FixCheck itself would reject it.
        try:
            validate_assertion_generator(generator)
        except argparse.ArgumentTypeError as exc:
            raise pytest.UsageError(f"bad --fixcheck-assertions value: {exc}")
    metafunc.parametrize("assertion_generator", generators, scope="module")


pytestmark = [
    pytest.mark.skipif(
        not os.path.isfile(FIXCHECK_JAR),
        reason=f"FixCheck jar not built; run: bash scripts/buildFixcheck.sh "
               f"(expected at {FIXCHECK_JAR})",
    ),
    pytest.mark.skipif(
        not docker_image_available(),
        reason=f"Docker daemon or image {DEFECTS4J_IMAGE} not available",
    ),
]


@pytest.fixture(scope="module", params=FIXCHECK_BUGS,
                ids=[f"{p}-{b}" for p, b in FIXCHECK_BUGS])
def fixcheck_devfix(request, tmp_path_factory, assertion_generator):
    """Apply a bug's developer fix and run FixCheck against it.

    The pipeline itself lives in ``fixcheck_devfix_pipeline`` since
    ``test_fixcheck_ollama_generator.py`` needs the very same preamble.

    Module-scoped, so the four checks below share one container run per
    (bug, generator) pair rather than paying for it four times.
    """
    project, bug_id = request.param
    # Scoped to this (bug, generator) pair, so other subjects keep their
    # artifacts and two generators can be compared side by side.
    log_dir = os.path.join(LOGS_DIR, f"{project}_{bug_id}", assertion_generator)
    with fixcheck_on_developer_fix(
        project, bug_id, assertion_generator,
        mount_dir=str(tmp_path_factory.mktemp("workspace")),
        log_dir=log_dir,
        num_prefixes=NUM_PREFIXES,
        similarity_threshold=SIMILARITY_THRESHOLD,
    ) as (result, _workdir):
        # A bug none of whose trigger methods can be mutated is not a FixCheck
        # subject at all -- documented upstream behavior rather than a
        # defect, so report it as a skip carrying the reason.
        if result["ok"] and not result["runs"] and result["skipped"]:
            reasons = "; ".join(
                f"{s['test_class']}::{s['method']}: {s['reason']}" for s in result["skipped"]
            )
            pytest.skip(f"{project} {bug_id} is not a FixCheck subject -- {reasons}")
        yield result


def test_fixcheck_runs_cleanly(fixcheck_devfix):
    """FixCheck completes without an orchestration-level error."""
    result = fixcheck_devfix
    assert result["ok"], f"FixCheck did not run cleanly: {result.get('error')}"


def _analyzed(result):
    """The FixCheck runs actually attempted.

    The trigger methods declined up front are in ``skipped`` instead: one
    whose literals all sit inside assertions, or that is inherited and so
    invisible to FixCheck, cannot yield a report by design. A bug can mix the
    two -- Math 69's ``SpearmansRankCorrelationTest`` inherits
    ``testPValueNearZero`` from ``PearsonsCorrelationTest``, which is analyzed
    normally. When *every* method is skipped the fixture skips the bug outright.
    """
    return result["runs"]


def test_fixcheck_produced_a_report_for_every_analyzed_class(fixcheck_devfix):
    """Every FixCheck run attempted got a parsed report.csv."""
    result = fixcheck_devfix
    analyzed = _analyzed(result)
    assert analyzed, "FixCheck attempted no run"
    for record in analyzed:
        assert record["ok"], (
            f"FixCheck({record['test_class']}::{record['method']}, "
            f"{record['inputs_class']}) produced no usable report: {record.get('error')}"
        )


def test_fixcheck_generated_usable_prefixes(fixcheck_devfix):
    """FixCheck actually built and ran variations, not just a report.

    Without this, ``test_developer_fix_is_not_suspicious`` would pass
    vacuously whenever every generated prefix failed to compile: the verdict
    would read "supported" purely because nothing was ever executed.
    """
    result = fixcheck_devfix
    for record in _analyzed(result):
        report = record["report"]
        assert report, f"no report for {record['test_class']}"
        executed = report["passing"] + report["crashing"] + report["assertion_failing"]
        assert executed > 0, (
            f"FixCheck({record['test_class']}::{record['method']}, "
            f"{record['inputs_class']}) compiled none of its "
            f"{report['total']} generated prefixes, so the verdict carries no "
            "information (non_compiling="
            f"{report['non_compiling']})"
        )


def test_the_verdict_on_the_developer_fix_is_backed_by_its_prefixes(fixcheck_devfix):
    """The verdict follows from the scored prefixes of analysed runs, and nothing else.

    This used to assert that the developer's fix is never flagged. It is:
    with Defects4J's header no longer inflating the trace distance, Math 69's
    fix scores 0.86 -- a mutated data point drives its p-value into the
    documented MATH-371 underflow and the original assertion fails -- and Cli
    35's fix is flagged on a genuinely ambiguous option. How often FixCheck
    flags a correct patch is what the ``devfix`` target of
    ``replay_fixcheck.py`` measures; this checks that a verdict, either way,
    is well-formed.
    """
    result = fixcheck_devfix
    assert result["analyzed_runs"] > 0, (
        "no FixCheck run was analyzed, so any verdict would be vacuous"
    )
    scores = [
        variation["score"]
        for run in result["runs"] if run["ok"]
        for variation in run["variations"]
        if variation["outcome"] in ("crashed", "failed assertion") and variation["score"] is not None
    ]
    assert len(scores) == result["scored_prefixes"]
    assert result["suspicious"] == any(s >= result["similarity_threshold"] for s in scores)
    assert result["max_failure_similarity"] == (max(scores) if scores else None)
    print(f"\n[test] {result['project']} {result['bug_id']} developer fix: "
          f"suspicious={result['suspicious']} "
          f"max_failure_similarity={result['max_failure_similarity']}")
