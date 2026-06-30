"""
Smoke test for the OpenRouter connector.

Verifies that ``OpenRouterLLM`` can talk to the free model
``openai/gpt-oss-20b:free`` by asking a trivial question (2 + 2)
and checking that the answer contains "4".

Run with:

    .venv/bin/python -m pytest test/test_openrouter.py -v -s

The ``.env`` is loaded automatically by ``conftest.py``.
"""

import os

import pytest

from llms import OpenRouterLLM

MODEL = "openai/gpt-oss-20b:free"


@pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY is not set",
)
def test_openrouter_2_plus_2():
    """The model should answer that 2 + 2 equals 4."""
    llm = OpenRouterLLM.initialize(model=MODEL, temperature=0.0, max_tokens=64)

    response = llm.invoke("What is 2 + 2? Answer with just the number.")
    text = getattr(response, "content", str(response))

    print(f"\nModel response: {text!r}")
    assert "4" in text, f"Expected '4' in the response, got: {text!r}"
