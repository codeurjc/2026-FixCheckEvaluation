"""
FixGenerator.py — Generate an LLM bug fix as a unified diff.

Dataset- and infrastructure-agnostic: it knows nothing about Defects4J or Docker.
The caller (e.g. ``Experiment.py``) is responsible for locating and reading the
buggy sources, applying the resulting diff and validating it. FixGenerator only:
  1. Builds a prompt from the bug description and the buggy source contents it is
     handed.
  2. Queries the LLM and extracts the diff from the response.
  3. Optionally persists the generation artifacts (``fix.diff`` and the raw
     response) and returns the generation metadata.
"""

import os
import re
import time
from datetime import datetime, timezone

from llms import GoogleLLM, OpenAILLM, OpenRouterLLM, OllamaLLM, CopilotLLM, AnthropicLLM


def extract_diff(response_text: str) -> str:
    """Extract a unified diff from an LLM response.

    Strips surrounding markdown code fences (```diff ... ``` or ``` ... ```)
    if the model added them despite instructions.
    """
    text = response_text.strip()

    # Remove a leading ```/```diff fence and the trailing ``` fence if present.
    fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, flags=re.DOTALL)
    if fence:
        return fence.group(1).strip()

    return text


def normalize_diff(diff_text: str) -> str:
    """Repair common malformations in LLM-generated unified diffs.

    The most frequent problem is blank context lines emitted without their
    leading space, which makes ``git apply``/``patch`` treat the hunk as ending
    prematurely ("corrupt patch" / "unexpectedly ends in middle of line"). Inside
    a hunk body, empty lines are rewritten as single-space context lines.
    """
    lines = diff_text.split("\n")
    out = []
    in_hunk = False
    for line in lines:
        if line.startswith("@@"):
            in_hunk = True
            out.append(line)
            continue
        # A new file header ends the current hunk body.
        if line.startswith(("--- ", "+++ ", "diff ", "index ")):
            in_hunk = False
            out.append(line)
            continue
        if in_hunk and line == "":
            # Blank context line that lost its leading space.
            out.append(" ")
        else:
            out.append(line)
    return "\n".join(out)


class FixGenerator:
    """Generates an LLM fix (unified diff) from a bug description and sources."""

    def __init__(self, model="ollama/gpt-oss:20b", temperature=0.0, max_tokens=8192):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

        self.llm = self._initialize_llm()

    # ------------------------------------------------------------------ LLM

    def _initialize_llm(self):
        """Initialize the LLM client based on the model identifier."""
        providers = [OllamaLLM, CopilotLLM, AnthropicLLM, OpenRouterLLM, GoogleLLM, OpenAILLM]
        for provider in providers:
            if provider.is_supported(self.model):
                # Ollama defaults to free-form text output (response_format=None),
                # which is what we need for a unified diff.
                return provider.initialize(self.model, self.temperature, self.max_tokens)
        raise ValueError(f"No provider found for model: {self.model}")

    # -------------------------------------------------------------- prompt

    def _build_prompt(self, bug_info, sources):
        """Build the fix-generation prompt.

        Args:
            bug_info: Free-form text describing the bug.
            sources: List of ``(relative_path, content)`` for the buggy file(s).
        """
        source_sections = []
        for rel_path, content in sources:
            source_sections.append(
                f"--- FILE: {rel_path} ---\n{content}"
            )
        sources_block = "\n\n".join(source_sections)

        return f"""[SYSTEM INSTRUCTION]
You are an expert software engineer fixing a real bug. You will be given bug \
metadata and the buggy source file(s). Produce a correct, complete fix.

[BUG METADATA]
{bug_info}

[BUGGY SOURCE FILE(S)]
{sources_block}

[TASK]
Fix the bug so that the failing (triggering) tests pass while keeping all other \
tests passing. Make all changes necessary to fix the bug correctly. Do not alter \
test files.

[OUTPUT FORMAT]
Respond with ONLY a single Git-compatible unified diff describing the changes.
- Use standard headers: `--- a/<path>` and `+++ b/<path>`, where <path> is the \
file path shown above (e.g. `{sources[0][0] if sources else 'path/To/File.java'}`).
- Include `@@ ... @@` hunk headers with correct line context.
- Do NOT wrap the diff in markdown code fences.
- Do NOT include any explanation before or after the diff.
"""

    # ----------------------------------------------------------------- run

    def generate(self, bug_info, sources, results_dir=None):
        """Generate a fix from the bug description and buggy sources.

        Args:
            bug_info: Free-form text describing the bug.
            sources: List of ``(relative_path, content)`` for the buggy file(s).
            results_dir: Optional directory; when given, the generation artifacts
                (``fix.diff`` and the raw response) are written there.

        Returns:
            A dict with the generation metadata: ``model``, ``temperature``,
            ``timestamp``, ``elapsed_seconds``, ``diff``, ``raw_response`` and
            ``usage_metadata``. Validation (applying the diff, running tests) is
            the caller's responsibility.
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        prompt = self._build_prompt(bug_info, sources)
        print(prompt)
        print(f"[fixgen] Querying LLM ({self.model}) for a fix ...")
        start = time.time()
        response = self.llm.invoke(prompt)
        elapsed = round(time.time() - start, 3)

        diff_text = extract_diff(response.content)

        if results_dir is not None:
            self._write_text(results_dir, "fix.diff", diff_text)
            self._write_text(results_dir, "raw_response.txt", response.content)

        return {
            "model": self.model,
            "temperature": self.temperature,
            "timestamp": timestamp,
            "elapsed_seconds": elapsed,
            "diff": diff_text,
            "raw_response": response.content,
            "usage_metadata": getattr(response, "usage_metadata", None),
        }

    @staticmethod
    def _write_text(results_dir, filename, content):
        os.makedirs(results_dir, exist_ok=True)
        path = os.path.join(results_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
