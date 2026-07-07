"""
Model-quality benchmark: does the LLM actually fix Defects4J Lang 1?

Unlike the deterministic mechanics in ``test_experiment_lang1.py``, this asserts
that the generated patch makes the bug's trigger tests pass. That outcome
depends on the model and is inherently non-deterministic (observed: identical
model/temperature/prompt, different generated patches across runs), so
``lang1_pipeline`` retries generation up to ``FIXGEN_BENCHMARK_ATTEMPTS``
(default 3) times and succeeds as soon as one attempt fixes the trigger tests.
The test is marked ``benchmark`` and runs only when explicitly requested:

    .venv/bin/python -m pytest test/test_benchmark_lang1.py --run-benchmark -v -s

It reuses the session-scoped ``lang1_pipeline`` fixture (in ``test/conftest.py``),
so it shares the same pipeline run as the mechanics tests when both are selected.
"""

import pytest


@pytest.mark.benchmark
def test_trigger_tests_pass(lang1_pipeline):
    """The bug's trigger tests pass after the fix (the LLM fixed the bug)."""
    assert lang1_pipeline["triggers_fixed"], (
        "Expected the trigger tests to pass after the fix (within "
        f"{lang1_pipeline['num_attempts']} attempt(s)), but some still fail on the "
        f"last attempt ({lang1_pipeline['attempt']}/{lang1_pipeline['num_attempts']}): "
        f"{sorted(set(lang1_pipeline['trigger_tests']) & set(lang1_pipeline['failing_after_names']))}"
    )
