"""
Shared pytest configuration.

Loads environment variables from the project ``.env`` and makes the
project root importable so tests can ``from llms import ...`` regardless
of where pytest is launched.
"""

import os
import sys

import pytest
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Make the project root importable.
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Load variables from .env (does not override already-set env vars).
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


# --- benchmark tests ---------------------------------------------------------
#
# Tests marked ``benchmark`` measure whether the LLM actually fixes the bug —
# a model-quality signal that is inherently non-deterministic. They are skipped
# by default and run only with ``pytest --run-benchmark``.

def pytest_addoption(parser):
    parser.addoption(
        "--run-benchmark", action="store_true", default=False,
        help="run model-quality benchmark tests (LLM must actually fix the bug)",
    )
    # --- FixCheck assertion generators ---------------------------------------
    #
    # Which generator(s) test/e2e/test_fixcheck_devfix.py runs each subject
    # with. Comma-separated to compare several in one go; each one multiplies
    # the run time, so the default stays at a single generator.
    parser.addoption(
        "--fixcheck-assertions", action="store", default="previous-assertion",
        help="comma-separated FixCheck assertion generator(s) for the devfix "
             "e2e test, e.g. 'previous-assertion,codellama' (default: "
             "previous-assertion)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "benchmark: model-quality test; runs only with --run-benchmark"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-benchmark"):
        return
    skip_benchmark = pytest.mark.skip(reason="needs --run-benchmark")
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip_benchmark)
