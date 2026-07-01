"""
Integration test: generate a fix for Defects4J Lang 1 with ``FixGenerator``.

This test performs the part of ``Experiment.py`` needed to feed ``FixGenerator``:
it starts a ``defects4j`` container, checks out Lang 1b, exports the buggy source
file(s) and the bug metadata, and hands them to ``FixGenerator.generate()``.

It deliberately stays within FixGenerator's responsibility: it only validates that
the returned diff has the expected unified-diff format and prints it. It does NOT
apply the diff nor run the test suite (that is Experiment's job).

Run with:

    .venv/bin/python -m pytest test/test_fixgenerator_lang1.py -v -s

Requires a running Docker daemon with the ``defects4j:3.0.1`` image and a running
Ollama daemon with the ``gpt-oss:20b`` model pulled. Set ``OLLAMA_BASE_URL`` if
Ollama is not on ``http://localhost:1995``. The test is skipped when any of these
is missing.
"""

import json
import os
import urllib.request

import pytest

from Experiment import (
    DEFECTS4J_IMAGE,
    start_container,
    run_step,
    locate_source_files,
    read_sources,
)
from FixGenerator import FixGenerator

PROJECT = "Lang"
BUG_ID = "1"
MODEL = os.getenv("FIXGEN_TEST_MODEL", "ollama/gpt-oss:120b")
MODEL_NAME = MODEL.replace("ollama/", "")
# Single source of truth for the Ollama host, shared by the skip check and the
# connector (which reads OLLAMA_BASE_URL), so both target the same daemon.
OLLAMA_HOST = os.getenv("OLLAMA_BASE_URL", "http://localhost:1995")


def _docker_image_available():
    """True when the Docker daemon is reachable and the image is present."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        tags = [tag for image in client.images.list() for tag in image.tags]
        return DEFECTS4J_IMAGE in tags
    except Exception:
        return False


def _model_available():
    """True when the Ollama daemon is reachable and the target model is pulled."""
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
def lang1_inputs(tmp_path_factory):
    """Check out Lang 1b and return the (bug_info, sources) for FixGenerator.

    Mirrors the setup Experiment.py does before delegating to FixGenerator, then
    yields exactly the data FixGenerator needs. The container is always removed.
    """
    import docker

    mount_dir = str(tmp_path_factory.mktemp("workspace"))
    workdir = os.path.join(mount_dir, f"{PROJECT}_{BUG_ID}")

    client = docker.from_env()
    container = start_container(client, mount_dir)
    try:
        checkout = run_step(
            container,
            f"defects4j checkout -p {PROJECT} -v {BUG_ID}b -w {workdir}",
            workdir=None,
            description=f"Checking out {PROJECT} {BUG_ID}b",
        )
        assert checkout.ok, f"checkout failed:\n{checkout.output}"

        info = run_step(
            container,
            f"defects4j info -p {PROJECT} -b {BUG_ID}",
            workdir=None,
            description="Extracting bug metadata (defects4j info)",
        )
        assert info.ok, f"info failed:\n{info.output}"

        files = locate_source_files(container, workdir)
        sources = read_sources(files)
        assert sources, "no buggy source files could be read"

        yield info.output, sources
    finally:
        container.stop()
        container.remove()


def test_fixgenerator_lang1_diff_format(lang1_inputs):
    """FixGenerator returns a well-formed unified diff for Lang 1."""
    bug_info, sources = lang1_inputs

    # Ensure OllamaLLM connects to the same host the skip check verified.
    os.environ["OLLAMA_BASE_URL"] = OLLAMA_HOST
    # gpt-oss:20b is a reasoning model: its "thinking" can exhaust a fixed token
    # budget before the diff is emitted, so let it generate until it stops
    # naturally (-1 = unlimited num_predict, bounded by the context window).
    generator = FixGenerator(model=MODEL, temperature=0.0, max_tokens=-1)
    gen = generator.generate(bug_info, sources)

    diff = gen["diff"]
    print("\n===== Generated fix (Lang 1) =====\n")
    print(diff)
    print("\n===== End of fix =====\n")

    assert diff.strip(), "FixGenerator returned an empty diff"
    # Unified-diff markers: file headers and at least one hunk.
    assert "--- " in diff, f"missing '--- ' file header in diff:\n{diff}"
    assert "+++ " in diff, f"missing '+++ ' file header in diff:\n{diff}"
    assert "@@" in diff, f"missing '@@' hunk header in diff:\n{diff}"
