"""
Integration test for the configurable Ollama assertion generator.

``assertion/OllamaGenerator.java`` (added by
``scripts/fixcheck-patches/0003-generic-ollama-assertion-generator.patch``)
is the one generator whose model and endpoint come from the configuration
rather than from ``private final`` fields: it is selected as
``ollama:<model>[@[<host>:]<port>]``, e.g. ``ollama:gpt-oss:120b@1995``. The
existing ``codellama`` / ``llama3.1`` generators can only ever talk to
``localhost:11434`` under a tag hardcoded in the jar.

What "it works" means here, and why each check is needed:

- FixCheck completes and reports on the trigger class, rather than throwing
  once per prefix (which is what a wrong endpoint or an unknown model looks
  like -- an empty report, not an error);
- the **configured** model and port were the ones actually used, taken from
  the run's own ``fixcheck.log``. Without this the test would still pass if
  the generator silently fell back to ``localhost:11434``, which is exactly
  the behavior the class exists to replace;
- the model really produced assertions and they reached the generated prefix
  sources. This is the check with teeth: a generator that returns nothing
  yields prefixes that pass vacuously, which is how upstream's
  ``previous-assertion`` looked healthy while asserting nothing at all (see
  docs/fixcheck-verdict-limitations.md).

The subject is Lang 12, whose ``RandomStringUtilsTest.testExceptions`` mutates
a ``java.lang.String`` literal. That matters for reliability: FixCheck runs
each variation *without* assertions first and only calls the generator when it
did not crash (``FixCheck.generateSimilarPrefixes``), and mutating an ``int``
often rewrites an array index into an out-of-bounds one -- Math 69 can crash
every variation before the generator is ever reached. Swapping one string for
another cannot. Its developer fix is applied first, so the run exercises the
generator on a real patched checkout via the shared pipeline in
``fixcheck_devfix_pipeline``.

Costs one model call per prefix, so ``NUM_PREFIXES`` is deliberately small.

Requires Docker with the ``defects4j:3.0.1`` image, the FixCheck jar
(``bash scripts/buildFixcheck.sh``) and an Ollama daemon serving the model;
skipped automatically when any of them is missing. Override the target with
``FIXCHECK_OLLAMA_TEST_MODEL`` (default ``gpt-oss:120b``) and
``FIXCHECK_OLLAMA_TEST_PORT`` (default ``1995``).

Run with:

    .venv/bin/python -m pytest test/e2e/test_fixcheck_ollama_generator.py -v -s
"""

import glob
import json
import os
import re
import urllib.request

import pytest

from Experiment import DEFECTS4J_IMAGE, FIXCHECK_JAR
from FixCheckWrapper import parse_ollama_generator
from fixcheck_devfix_pipeline import (
    LOGS_DIR,
    docker_image_available,
    fixcheck_on_developer_fix,
)

PROJECT, BUG_ID = "Lang", "12"
MODEL = os.getenv("FIXCHECK_OLLAMA_TEST_MODEL", "gpt-oss:120b")
PORT = os.getenv("FIXCHECK_OLLAMA_TEST_PORT", "1995")
ASSERTION_GENERATOR = f"ollama:{MODEL}@{PORT}"

# One model call per prefix, so keep it low; three is enough to show the
# generator is driven repeatedly and not just once.
NUM_PREFIXES = 3
SIMILARITY_THRESHOLD = 0.8

BACKEND = parse_ollama_generator(ASSERTION_GENERATOR)


def _model_available():
    """True when the configured daemon serves the configured tag."""
    try:
        with urllib.request.urlopen(f"{BACKEND.base_url}/api/tags", timeout=5) as resp:
            tags = [m["name"] for m in json.load(resp).get("models", [])]
        return BACKEND.wanted_tag in tags
    except Exception:
        return False


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
    pytest.mark.skipif(
        not _model_available(),
        reason=f"Ollama at {BACKEND.base_url} does not serve {BACKEND.wanted_tag!r}",
    ),
]


@pytest.fixture(scope="module")
def ollama_fixcheck(tmp_path_factory):
    """Run FixCheck over the bug's developer fix with the Ollama generator."""
    log_dir = os.path.join(LOGS_DIR, f"{PROJECT}_{BUG_ID}", "ollama-generator")
    with fixcheck_on_developer_fix(
        PROJECT, BUG_ID, ASSERTION_GENERATOR,
        mount_dir=str(tmp_path_factory.mktemp("workspace")),
        log_dir=log_dir,
        num_prefixes=NUM_PREFIXES,
        similarity_threshold=SIMILARITY_THRESHOLD,
    ) as (result, _workdir):
        # If every variation crashed on its own, FixCheck never reached the
        # assertion-generation step and the run says nothing about the
        # generator. That is upstream's role-blind literal mutation (see
        # docs/fixcheck-verdict-limitations.md) and random per run, so report
        # it as a skip rather than a spurious failure.
        logs = _fixcheck_logs(log_dir)
        invoked = sum(
            open(path, encoding="utf-8", errors="replace").read().count(
                "---> assertion generator:"
            )
            for path in logs
        )
        if not invoked:
            pytest.skip(
                f"every one of the {NUM_PREFIXES} variations crashed before the "
                "assertion generator ran, so this run cannot exercise it; "
                f"artifacts in {log_dir}"
            )
        yield result, log_dir


def _analyzed(result):
    """The trigger classes FixCheck actually attempted.

    A class it declined up front (an inherited trigger method, or literals
    that only occur inside assertions) is expected and unrelated to the
    generator.
    """
    return [r for r in result["per_test_class"] if not r.get("skipped")]


def _fixcheck_logs(log_dir):
    return glob.glob(os.path.join(log_dir, "*", "fixcheck.log"))


def test_fixcheck_runs_with_the_ollama_generator(ollama_fixcheck):
    """The run completes and reports on at least one trigger class."""
    result, _log_dir = ollama_fixcheck
    assert result["ok"], f"FixCheck did not run cleanly: {result.get('error')}"
    analyzed = _analyzed(result)
    assert analyzed, "FixCheck attempted no trigger class"
    for record in analyzed:
        assert record["ok"], (
            f"FixCheck({record['test_class']}) produced no usable report: "
            f"{record.get('error')}. An unreachable daemon or an unknown model "
            "surfaces exactly like this."
        )


def test_the_configured_model_and_port_were_used(ollama_fixcheck):
    """The endpoint came from the option, not from the hardcoded default.

    This is the whole point of the class: ``codellama`` / ``llama3.1`` can
    only reach ``localhost:11434`` under a fixed tag.
    """
    _result, log_dir = ollama_fixcheck
    logs = _fixcheck_logs(log_dir)
    assert logs, f"no fixcheck.log under {log_dir}"
    log = open(logs[0], encoding="utf-8", errors="replace").read()

    assert f"ollama endpoint: model={MODEL} at {BACKEND.base_url}/api/generate" in log, (
        "FixCheck did not resolve the endpoint from the "
        f"{ASSERTION_GENERATOR!r} option"
    )
    assert f'"model":"{MODEL}"' in log, (
        f"no request carried the configured model {MODEL!r}"
    )
    assert "localhost:11434" not in log, (
        "the run still touched the hardcoded default endpoint"
    )


def test_the_model_wrote_assertions_into_the_prefixes(ollama_fixcheck):
    """The generated variations actually carry model-written assertions.

    Without this the run could look perfectly healthy while every prefix
    asserted nothing and passed vacuously.
    """
    _result, log_dir = ollama_fixcheck
    logs = _fixcheck_logs(log_dir)
    assert logs, f"no fixcheck.log under {log_dir}"
    log = open(logs[0], encoding="utf-8", errors="replace").read()

    returned = [
        line for line in log.splitlines()
        if line.startswith("---> assertions: [") and line.strip() != "---> assertions: []"
    ]
    assert returned, (
        "the model returned no assertion for any prefix; the generator's "
        f"response parsing may not fit {MODEL!r}. Inspect {logs[0]}"
    )

    # And they must survive into the sources FixCheck compiled and ran: a
    # returned assertion that JavaParser rejects is dropped silently.
    sources = glob.glob(os.path.join(log_dir, "*", "fixcheck-output", "*", "*.java"))
    assert sources, f"no generated prefix sources under {log_dir}"
    with_assertions = [
        path for path in sources
        if re.search(r"\bassert(True|False|Equals|NotNull|Null|NotEquals)\s*\(",
                     open(path, encoding="utf-8", errors="replace").read())
    ]
    assert with_assertions, (
        f"none of the {len(sources)} generated prefixes contains an assertion, "
        "so their verdicts carry no information"
    )


def test_prefixes_were_compiled_and_executed(ollama_fixcheck):
    """The variations ran, so the report is not vacuous."""
    result, _log_dir = ollama_fixcheck
    for record in _analyzed(result):
        report = record["report"]
        assert report, f"no report for {record['test_class']}"
        executed = report["passing"] + report["crashing"] + report["assertion_failing"]
        assert executed > 0, (
            f"FixCheck({record['test_class']}) compiled none of its "
            f"{report['total']} generated prefixes (non_compiling="
            f"{report['non_compiling']})"
        )
