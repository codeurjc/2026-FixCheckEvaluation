"""
Shared pytest configuration.

Loads environment variables from the project ``.env`` and makes the
project root importable so tests can ``from llms import ...`` regardless
of where pytest is launched.
"""

import os
import sys

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Make the project root importable.
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Load variables from .env (does not override already-set env vars).
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
