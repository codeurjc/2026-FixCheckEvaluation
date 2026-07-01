"""
Integration test: full Experiment pipeline for Defects4J Lang 1.

GIVEN:  Defects4J Lang 1b (buggy), the project source files, and the LLM model
WHEN:   FixGenerator generates a fix and it is applied with ``apply_diff``
THEN:   The fixed project compiles and ``defects4j test`` reports 0 failing tests.

Run with:

    .venv/bin/python -m pytest test/test_experiment_lang1.py -v -s

Requires a running Docker daemon with the ``defects4j:3.0.1`` image and a
running Ollama daemon with the ``gemma3:12b`` model pulled.  Set
``OLLAMA_BASE_URL`` if Ollama is not on ``http://localhost:1995``.  The test is
skipped when any of these prerequisites is missing.
"""

import json
import os
import urllib.request

import pytest

from Experiment import (
    DEFECTS4J_IMAGE,
    apply_diff,
    locate_source_files,
    parse_failing_tests,
    read_sources,
    run_step,
    start_container,
)
from FixGenerator import FixGenerator

PROJECT = "Lang"
BUG_ID = "1"
MODEL = os.getenv("FIXGEN_TEST_MODEL", "ollama/gpt-oss:120b")
MODEL_NAME = MODEL.replace("ollama/", "")
OLLAMA_HOST = os.getenv("OLLAMA_BASE_URL", "http://localhost:1995")


def _docker_image_available():
    try:
        import docker

        client = docker.from_env()
        client.ping()
        tags = [tag for image in client.images.list() for tag in image.tags]
        return DEFECTS4J_IMAGE in tags
    except Exception:
        return False


def _model_available():
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2) as resp:
            tags = [m["name"] for m in json.load(resp).get("models", [])]
        return MODEL_NAME in tags
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _model_available(),
        reason=f"Ollama daemon unreachable or model {MODEL_NAME!r} not pulled",
    ),
    pytest.mark.skipif(
        not _docker_image_available(),
        reason=f"Docker daemon or image {DEFECTS4J_IMAGE} not available",
    ),
]


@pytest.fixture(scope="module")
def pipeline_result(tmp_path_factory):
    """Run the full Experiment pipeline for Lang 1b and yield the outcome.

    Mirrors exactly what Experiment.py does:
      checkout → compile (pre) → test (pre) → info → sources →
      generate fix → apply → compile (post) → test (post)

    The container is always removed afterwards. Each stage result is captured
    so that individual test functions can make targeted assertions.
    """
    import docker

    mount_dir = str(tmp_path_factory.mktemp("workspace"))
    workdir = os.path.join(mount_dir, f"{PROJECT}_{BUG_ID}")

    os.environ["OLLAMA_BASE_URL"] = OLLAMA_HOST

    client = docker.from_env()
    container = start_container(client, mount_dir)
    try:
        # 1. Checkout the buggy version.
        checkout = run_step(
            container,
            f"defects4j checkout -p {PROJECT} -v {BUG_ID}b -w {workdir}",
            workdir=None,
            description=f"Checking out {PROJECT} {BUG_ID}b",
        )
        assert checkout.ok, f"checkout failed:\n{checkout.output}"

        # 2. Compile pre-fix — confirm the project builds in its buggy state.
        compile_before = run_step(
            container, "defects4j compile", workdir,
            description="Compiling buggy sources (pre-fix)",
        )
        assert compile_before.ok, f"pre-fix compilation failed:\n{compile_before.output}"

        # 3. Run tests pre-fix — record how many tests were failing.
        test_before = run_step(
            container, "defects4j test", workdir,
            description="Running test suite (pre-fix)",
        )
        failing_before = parse_failing_tests(test_before.output)
        print(f"\n[test] Failing tests before fix: {failing_before}")

        # 4. Extract bug metadata.
        info = run_step(
            container,
            f"defects4j info -p {PROJECT} -b {BUG_ID}",
            workdir=None,
            description="Extracting bug metadata",
        )
        assert info.ok, f"info failed:\n{info.output}"

        # 5. Locate and read the buggy source files.
        files = locate_source_files(container, workdir)
        sources = read_sources(files)
        assert sources, "no buggy source files could be read"

        # 6. Generate fix via LLM.
        generator = FixGenerator(model=MODEL, temperature=0.0, max_tokens=-1)
        gen = generator.generate(info.output, sources)

        diff = gen["diff"]
        print("\n===== Generated fix (Lang 1) =====\n")
        print(diff)
        print("\n===== End of fix =====\n")

        # 7. Apply the diff.
        applied, apply_log = apply_diff(container, workdir, diff)
        print(f"[test] Diff applied: {applied}")
        if not applied:
            print(f"[test] Apply log:\n{apply_log}")

        # 8 & 9. Compile and test post-fix only if the diff was applied.
        compiled = False
        failing_after = -1
        compile_after_output = ""
        if applied:
            compile_after = run_step(
                container, "defects4j compile", workdir,
                description="Compiling fixed sources (post-fix)",
            )
            compiled = compile_after.ok
            compile_after_output = compile_after.output

            if compiled:
                test_after = run_step(
                    container, "defects4j test", workdir,
                    description="Running test suite (post-fix)",
                )
                failing_after = parse_failing_tests(test_after.output)
                print(f"[test] Failing tests after fix: {failing_after}")

        yield {
            "failing_before": failing_before,
            "diff": diff,
            "applied": applied,
            "apply_log": apply_log,
            "compiled": compiled,
            "compile_after_output": compile_after_output,
            "failing_after": failing_after,
        }
    finally:
        container.stop()
        container.remove()


def test_bug_present_before_fix(pipeline_result):
    """Lang 1b has at least one failing test before any fix is applied."""
    assert pipeline_result["failing_before"] > 0, (
        "Expected ≥1 failing test before the fix; "
        f"got {pipeline_result['failing_before']}"
    )


def test_diff_applied(pipeline_result):
    """The generated diff can be applied to the checked-out source tree."""
    assert pipeline_result["applied"], (
        f"apply_diff failed for all strategies.\nApply log:\n{pipeline_result['apply_log']}"
    )


def test_fixed_sources_compile(pipeline_result):
    """The project compiles without errors after applying the fix."""
    assert pipeline_result["compiled"], (
        f"defects4j compile failed after applying the fix.\n"
        f"{pipeline_result['compile_after_output']}"
    )


def test_regression_suite_passes(pipeline_result):
    """The full test suite reports 0 failing tests after the fix is applied."""
    assert pipeline_result["failing_after"] == 0, (
        f"Expected 0 failing tests after fix, got {pipeline_result['failing_after']}"
    )
