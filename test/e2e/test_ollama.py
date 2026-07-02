"""
Smoke test for the Ollama connector.

Verifies that ``OllamaLLM`` can talk to the local model ``gpt-oss:20b`` by
asking a trivial question (2 + 2) and checking that the answer contains "4".

Run with:

    .venv/bin/python -m pytest test/test_ollama.py -v -s

Requires a running Ollama daemon with the ``gpt-oss:20b`` model pulled. Set
``OLLAMA_BASE_URL`` if Ollama is not on ``http://localhost:1995``. The test is
skipped when the daemon is unreachable or the model is not pulled.
"""

import json
import os
import urllib.request

import pytest

from llms import OllamaLLM

MODEL = "ollama/gpt-oss:120b"
MODEL_NAME = MODEL.replace("ollama/", "")
# Single source of truth for the daemon host, shared by the skip check and the
# connector (which reads OLLAMA_BASE_URL), so both target the same Ollama.
HOST = os.getenv("OLLAMA_BASE_URL", "http://localhost:1995")


def _model_available():
    """True when the Ollama daemon is reachable and the target model is pulled."""
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=2) as resp:
            tags = [m["name"] for m in json.load(resp).get("models", [])]
        return MODEL_NAME in tags
    except Exception:
        return False


@pytest.mark.skipif(
    not _model_available(),
    reason=f"Ollama daemon unreachable or model {MODEL_NAME!r} not pulled",
)
def test_ollama_2_plus_2():
    """The model should answer that 2 + 2 equals 4."""
    # Ensure OllamaLLM connects to the same host the skip check verified.
    os.environ["OLLAMA_BASE_URL"] = HOST
    llm = OllamaLLM.initialize(model=MODEL, temperature=0.0, max_tokens=64)

    response = llm.invoke("What is 2 + 2? Answer with just the number.")
    text = getattr(response, "content", str(response))

    print(f"\nModel response: {text!r}")
    assert "4" in text, f"Expected '4' in the response, got: {text!r}"
