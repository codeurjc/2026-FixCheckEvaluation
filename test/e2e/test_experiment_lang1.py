"""
Integration test: deterministic mechanics of the Experiment pipeline for
Defects4J Lang 1.

These assertions check the parts of the pipeline that do NOT depend on the LLM
actually fixing the bug: the bug is reproduced, the generated diff applies, the
project still compiles, and the patch introduces no new failures. Whether the
LLM's fix makes the trigger tests pass is a model-quality signal covered
separately by ``test_benchmark_lang1.py`` (run with ``--run-benchmark``).

The shared ``lang1_pipeline`` fixture (in ``test/conftest.py``) runs the full
pipeline once and is skipped when Docker, the ``defects4j:3.0.1`` image, or the
target Ollama model is unavailable.

Run with:

    .venv/bin/python -m pytest test/test_experiment_lang1.py -v -s
"""


def test_bug_present_before_fix(lang1_pipeline):
    """Lang 1b's trigger tests are failing before any fix is applied."""
    failing = set(lang1_pipeline["failing_before_names"])
    triggers = set(lang1_pipeline["trigger_tests"])
    assert triggers & failing, (
        f"Expected the trigger tests {triggers} to be failing before the fix; "
        f"failing tests were {failing}"
    )


def test_diff_applied(lang1_pipeline):
    """The generated diff can be applied to the checked-out source tree."""
    assert lang1_pipeline["applied"], (
        f"apply_diff failed for all strategies.\nApply log:\n{lang1_pipeline['apply_log']}"
    )


def test_fixed_sources_compile(lang1_pipeline):
    """The project compiles without errors after applying the fix."""
    assert lang1_pipeline["compiled"], (
        f"defects4j compile failed after applying the fix.\n"
        f"{lang1_pipeline['compile_after_output']}"
    )


def test_no_new_failures(lang1_pipeline):
    """The patch does not introduce any test that passed before but fails now."""
    assert not lang1_pipeline["new_failures"], (
        f"Patch introduced new failures: {lang1_pipeline['new_failures']}"
    )
