"""
Integration test: ``Experiment.py`` driving ``FixCheckWrapper.py``, end to end.

The other FixCheck tests each cover one side of the seam and stop there:
``test_fixcheck_devfix.py`` and ``test_fixcheck_ollama_generator.py`` build a
``FixCheckWrapper`` themselves and hand it an already-patched checkout, while
``test_experiment_lang1.py`` exercises the pipeline with FixCheck switched off.
Nothing checks that ``Experiment.main()`` wires the two together correctly, and
that wiring has real hazards:

- the *pre-fix* failure trace must be written before the patch is generated --
  once the patch is applied, Defects4J's ``failing_tests`` file has been
  overwritten and the trace FixCheck compares against is gone;
- the trigger methods' **source** has to reach the wrapper (keyed by class) for
  ``select_fixcheck_inputs`` to pick an ``inputs-class`` at all;
- the container's network mode is fixed at creation time, long before FixCheck
  runs, so an Ollama-backed generator has to be accounted for up front;
- FixCheck is advisory and must never change ``fixed``;
- its artifacts have to survive into ``results/`` before the checkout is reused.

So this test runs the real ``Experiment.main()`` with ``--fixcheck``, then reads
back ``result.json`` and the copied artifacts.

**The LLM is replayed, not called.** The recorded response in
``fixtures/lang12_raw_response.txt`` is the one gpt-oss:120b actually produced
for Lang 12 (see ``scripts/runExperiment.sh``); replaying it through
``FixGenerator._initialize_llm`` keeps the real prompt building and
SEARCH/REPLACE-to-diff parsing in play while making the outcome deterministic.
That matters because FixCheck only runs on a *plausible* patch: with live
generation, a model that failed to fix the bug would silently turn this into a
test that asserts nothing.

FixCheck's own assertion generator *is* parametrized, over
``--fixcheck-assertions`` (comma-separated, default ``previous-assertion``), so
the same wiring can be checked with and without a live model:

    # fast, no model calls
    .venv/bin/python -m pytest test/e2e/test_experiment_fixcheck_integration.py -v -s

    # exercising the configurable Ollama generator too
    .venv/bin/python -m pytest test/e2e/test_experiment_fixcheck_integration.py -v -s \
        --fixcheck-assertions previous-assertion,ollama:gpt-oss:120b@1995

Requires Docker with the ``defects4j:3.0.1`` image and the FixCheck jar
(``bash scripts/buildFixcheck.sh``); an Ollama-backed generator additionally
needs the daemon to serve that model. Skipped automatically otherwise.
"""

import argparse
import json
import os
import sys
import urllib.request
from unittest.mock import patch

import pytest

import Experiment
from Experiment import DEFECTS4J_IMAGE, FIXCHECK_JAR, model_dir_name
from FixCheckWrapper import (
    resolve_ollama_backend,
    validate_assertion_generator,
)
from FixGenerator import FixGenerator
from fixcheck_devfix_pipeline import docker_image_available

PROJECT, BUG_ID = "Lang", "12"
# Only used for the results path and the recorded metadata -- no call is made.
MODEL = "ollama/gpt-oss:120b"
# One model call per variation when the generator is LLM-backed, so keep it low.
NUM_PREFIXES = 3

_HERE = os.path.dirname(os.path.abspath(__file__))
RAW_RESPONSE_PATH = os.path.join(_HERE, "fixtures", "lang12_raw_response.txt")

# Lang 12's second trigger method has no mutable String literal, so FixCheck
# analyzes only the first -- asserted below, since dropping the *wrong* one
# would still look like a healthy run.
TRIGGER_CLASS = "org.apache.commons.lang3.RandomStringUtilsTest"
ANALYZED_METHOD = "testExceptions"


def pytest_generate_tests(metafunc):
    """Parametrize over ``--fixcheck-assertions``, as the devfix test does."""
    if "assertion_generator" not in metafunc.fixturenames:
        return
    raw = metafunc.config.getoption("--fixcheck-assertions")
    generators = [g.strip() for g in raw.split(",") if g.strip()]
    for generator in generators:
        try:
            validate_assertion_generator(generator)
        except argparse.ArgumentTypeError as exc:
            raise pytest.UsageError(f"bad --fixcheck-assertions value: {exc}")
    metafunc.parametrize("assertion_generator", generators, scope="module")


def _ollama_ready(assertion_generator):
    """True unless the generator needs an Ollama model that is not served."""
    backend = resolve_ollama_backend(assertion_generator)
    if backend is None:
        return True
    try:
        with urllib.request.urlopen(f"{backend.base_url}/api/tags", timeout=5) as resp:
            tags = [m["name"] for m in json.load(resp).get("models", [])]
        return backend.wanted_tag in tags
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not os.path.isfile(RAW_RESPONSE_PATH),
        reason=f"pre-recorded response not found: {RAW_RESPONSE_PATH}",
    ),
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


class _ReplayedResponse:
    """Stands in for the provider's response object.

    A plain object rather than a ``MagicMock`` because ``Experiment.main()``
    serializes ``usage_metadata`` into ``result.json``, and a mock's
    auto-created attribute is not JSON-serializable.
    """

    def __init__(self, content):
        self.content = content
        self.usage_metadata = {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        }


@pytest.fixture(scope="module")
def experiment_run(tmp_path_factory, assertion_generator):
    """Run ``Experiment.main()`` with ``--fixcheck`` and return its artifacts.

    Yields ``(result, results_dir, workdir)`` where ``result`` is the parsed
    ``result.json``. Module-scoped: the four checks below share one pipeline
    run rather than paying for it four times.
    """
    if not _ollama_ready(assertion_generator):
        backend = resolve_ollama_backend(assertion_generator)
        pytest.skip(
            f"Ollama at {backend.base_url} does not serve {backend.wanted_tag!r}"
        )

    with open(RAW_RESPONSE_PATH, "r", encoding="utf-8") as f:
        raw_response = f.read()

    mount_dir = str(tmp_path_factory.mktemp("workspace"))
    workdir = os.path.join(mount_dir, f"{PROJECT}_{BUG_ID}")
    # Experiment.main() builds its results path relative to the CWD, so run it
    # from a scratch directory instead of polluting the repository's results/.
    run_root = str(tmp_path_factory.mktemp("results_root"))
    results_dir = os.path.join(
        run_root, "results", model_dir_name(MODEL), PROJECT, f"Bug_{BUG_ID}"
    )

    argv = [
        "Experiment.py",
        "--project", PROJECT,
        "--bug-id", BUG_ID,
        "--workdir", mount_dir,
        "--model", MODEL,
        "--include-test-code", "--include-test-log",
        "--fixcheck",
        "--fixcheck-assertions", assertion_generator,
        "--fixcheck-prefixes", str(NUM_PREFIXES),
    ]

    class _StubLLM:
        def invoke(self, _prompt):
            return _ReplayedResponse(raw_response)

    previous_cwd = os.getcwd()
    os.chdir(run_root)
    try:
        with patch.object(FixGenerator, "_initialize_llm", return_value=_StubLLM()), \
                patch.object(sys, "argv", argv):
            Experiment.main()
    finally:
        os.chdir(previous_cwd)

    with open(os.path.join(results_dir, "result.json"), encoding="utf-8") as f:
        result = json.load(f)
    print(f"\n[test] FixCheck block: {json.dumps(result['fixcheck'], indent=2)}")
    yield result, results_dir, workdir


def test_the_patch_is_plausible_so_fixcheck_had_something_to_check(experiment_run):
    """The replayed patch applies and fixes the trigger tests.

    A precondition rather than the point of the test: FixCheck only runs on a
    plausible patch, so if this regressed everything below would pass
    vacuously.
    """
    result, _results_dir, _workdir = experiment_run
    assert result["applied"], "the recorded patch no longer applies"
    assert result["triggers_fixed"], (
        f"the recorded patch no longer fixes the trigger tests "
        f"{result['trigger_tests']}"
    )


def test_result_json_carries_the_fixcheck_verdict(experiment_run, assertion_generator):
    """``Experiment.main()`` records what FixCheck was asked and what it found."""
    result, _results_dir, _workdir = experiment_run
    fixcheck = result["fixcheck"]
    assert fixcheck is not None, (
        "FixCheck did not run even though the patch was plausible"
    )
    assert fixcheck["ran"] and fixcheck["ok"], (
        f"FixCheck did not run cleanly: {fixcheck.get('error')}"
    )
    # The CLI options made the round trip into the wrapper's configuration.
    assert fixcheck["assertion_generator"] == assertion_generator
    assert fixcheck["num_prefixes"] == NUM_PREFIXES
    # The top-level flag mirrors the block, so a caller reading only the flag
    # never disagrees with one reading the block.
    assert result["fixcheck_suspicious"] == fixcheck["suspicious"]


def test_fixcheck_received_the_trigger_test_sources(experiment_run):
    """The trigger methods' source crossed the ``Experiment`` -> wrapper seam.

    ``select_fixcheck_inputs`` needs the method bodies to pick an
    ``inputs-class``; without them every class is skipped as unmutable and the
    run reaches a verdict having analyzed nothing.
    """
    result, _results_dir, _workdir = experiment_run
    fixcheck = result["fixcheck"]
    assert fixcheck["analyzed_test_classes"] > 0, (
        "no trigger class was analyzed, so the verdict is vacuous"
    )
    assert fixcheck["inputs_class"].get(TRIGGER_CLASS) == "java.lang.String"

    record = next(
        r for r in fixcheck["per_test_class"] if r["test_class"] == TRIGGER_CLASS
    )
    # Lang 12's other trigger method has no mutable String literal; passing it
    # anyway would abort the whole class inside FixCheck.
    assert record["test_methods"] == [ANALYZED_METHOD]

    report = record["report"]
    executed = report["passing"] + report["crashing"] + report["assertion_failing"]
    assert executed > 0, (
        f"none of the {report['total']} generated variations compiled and ran "
        f"(non_compiling={report['non_compiling']}), so the verdict carries no "
        "information"
    )


def test_the_prefix_failure_trace_was_captured_before_the_patch(experiment_run):
    """The trace FixCheck compares against is the *pre-fix* failure.

    ``Experiment.main()`` has to write this during step 5d, before generating
    the fix: afterwards the patch is applied and Defects4J has overwritten its
    ``failing_tests`` file. If the ordering regressed, the file would still
    exist but describe a passing run, and every similarity score would be
    computed against nothing.
    """
    _result, _results_dir, workdir = experiment_run
    trace_path = os.path.join(workdir, ".fixcheck", f"{TRIGGER_CLASS}.failing_tests")
    assert os.path.exists(trace_path), f"no pre-fix failure trace at {trace_path}"
    trace = open(trace_path, encoding="utf-8", errors="replace").read()
    assert ANALYZED_METHOD in trace and "Exception" in trace, (
        "the captured trace does not look like the original failure -- it may "
        f"have been written after the patch was applied:\n{trace[:500]}"
    )


def test_fixcheck_artifacts_are_copied_into_the_results_dir(experiment_run):
    """The run's evidence outlives the checkout it was produced in."""
    _result, results_dir, _workdir = experiment_run
    dest = os.path.join(results_dir, "fixcheck", TRIGGER_CLASS.rsplit(".", 1)[-1])
    assert os.path.isdir(dest), f"FixCheck artifacts were not copied to {dest}"
    for name in ("report.csv", "fixcheck.log"):
        assert os.path.isfile(os.path.join(dest, name)), f"missing {name} in {dest}"


def test_fixcheck_is_advisory_and_never_decides_fixed(experiment_run):
    """``fixed`` follows the trigger tests alone, whatever FixCheck says."""
    result, _results_dir, _workdir = experiment_run
    assert result["fixed"] == (
        result["triggers_fixed"] and not result["new_failures"]
    ), (
        "'fixed' no longer follows purely from the trigger tests and "
        "regressions; FixCheck's verdict must stay advisory "
        f"(fixcheck_suspicious={result['fixcheck_suspicious']})"
    )
