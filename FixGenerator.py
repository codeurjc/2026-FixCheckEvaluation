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


# Matches a unified-diff hunk header, capturing the old/new start lines and any
# trailing section heading (e.g. the enclosing function name git echoes). The
# line counts are intentionally not captured: ``normalize_diff`` recomputes them.
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")

_FILE_HEADER_PREFIXES = ("--- ", "+++ ", "diff ", "index ")


def normalize_diff(diff_text: str) -> str:
    """Repair common malformations in LLM-generated unified diffs.

    Three fixes are applied, all of which frequently break ``git apply``/
    ``patch`` on otherwise-correct LLM output:

    1. Blank context lines emitted without their leading space are rewritten as
       single-space context lines (otherwise the hunk is treated as ending
       prematurely: "corrupt patch" / "unexpectedly ends in middle of line").
    2. A trailing marker or comment appended after the last hunk (e.g.
       ``*** End of File ***``), despite explicit instructions not to, is
       dropped: any in-hunk line that isn't a context (" "), removal ("-"),
       addition ("+") or no-newline-marker ("\\") line ends the diff.
    3. Each ``@@`` hunk header's line counts are recomputed from the actual
       hunk body. Models routinely emit wrong counts, which makes the parser
       mis-detect the hunk boundary and reject the whole patch as corrupt.
    """
    cleaned = _strip_hunk_bodies(diff_text.split("\n"))
    return "\n".join(_recount_hunk_headers(cleaned))


def _strip_hunk_bodies(lines):
    """Fix blank context lines and drop trailing non-diff garbage."""
    out = []
    in_hunk = False
    for line in lines:
        if line.startswith("@@"):
            in_hunk = True
            out.append(line)
            continue
        # A new file header ends the current hunk body.
        if line.startswith(_FILE_HEADER_PREFIXES):
            in_hunk = False
            out.append(line)
            continue
        if in_hunk and line == "":
            # Blank context line that lost its leading space.
            out.append(" ")
            continue
        if in_hunk and line[:1] not in (" ", "+", "-", "\\"):
            # Trailing garbage after the last hunk line; the diff is over.
            break
        out.append(line)
    return out


def _recount_hunk_headers(lines):
    """Rewrite every ``@@`` header's line counts to match its body.

    Headers without parseable start lines (e.g. a bare ``@@ @@``) are left
    untouched, since their offsets can't be recovered here.
    """
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        match = _HUNK_HEADER_RE.match(line)
        if not match:
            out.append(line)
            i += 1
            continue

        old_start, new_start, heading = match.groups()
        body = []
        j = i + 1
        while j < n:
            body_line = lines[j]
            if body_line.startswith("@@") or body_line.startswith(_FILE_HEADER_PREFIXES):
                break
            body.append(body_line)
            j += 1

        old_count = sum(1 for b in body if b[:1] in (" ", "-"))
        new_count = sum(1 for b in body if b[:1] in (" ", "+"))
        out.append(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{heading}")
        out.extend(body)
        i = j
    return out


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

    def _build_prompt(self, bug_info, sources, test_sources=None, test_log=None, issue_text=None):
        """Build the fix-generation prompt.

        Args:
            bug_info: Free-form text describing the bug.
            sources: List of ``(relative_path, content)`` for the buggy file(s).
            test_sources: Optional list of ``(relative_path, content)`` for the
                regression (trigger) test file(s).
            test_log: Optional free-form text with the regression test's
                failure output.
            issue_text: Optional free-form text with the original bug-tracker
                issue report.
        """
        source_sections = []
        for rel_path, content in sources:
            source_sections.append(
                f"--- FILE: {rel_path} ---\n{content}"
            )
        sources_block = "\n\n".join(source_sections)

        optional_sections = ""
        if test_sources:
            test_source_sections = []
            for rel_path, content in test_sources:
                test_source_sections.append(
                    f"--- FILE: {rel_path} ---\n{content}"
                )
            optional_sections += (
                "\n[REGRESSION TEST SOURCE FILE(S)]\n"
                + "\n\n".join(test_source_sections)
                + "\n"
            )
        if test_log:
            optional_sections += f"\n[REGRESSION TEST LOG]\n{test_log}\n"
        if issue_text:
            optional_sections += f"\n[ORIGINAL ISSUE REPORT]\n{issue_text}\n"

        return f"""[SYSTEM INSTRUCTION]
You are an expert software engineer fixing a real bug. You will be given bug \
metadata and the buggy source file(s). Produce a correct, complete fix.

[BUG METADATA]
{bug_info}

[BUGGY SOURCE FILE(S)]
{sources_block}
{optional_sections}
[TASK]
Fix the bug so that the failing (triggering) tests pass while keeping all other \
tests passing. Make all changes necessary to fix the bug correctly. Do not alter \
test files.

[OUTPUT FORMAT]
Respond with ONLY a single Git-compatible unified diff describing the changes.
- Use standard headers: `--- a/<path>` and `+++ b/<path>`, where <path> is the \
file path shown above (e.g. `{sources[0][0] if sources else 'path/To/File.java'}`).
- Every hunk header MUST include all four line numbers, in the exact form \
`@@ -<start_line>,<line_count> +<start_line>,<line_count> @@`. A bare `@@ @@` \
with no numbers is INVALID and will be rejected.
- The line counts in each hunk header MUST match the number of context/removed \
lines (for the first count) and context/added lines (for the second count) that \
actually follow it.
- Do NOT wrap the diff in markdown code fences.
- Do NOT include any explanation, commentary, or marker (e.g. "End of file", \
"Done") before, between, or after the diff. The response must contain nothing \
but the diff itself, and it must end immediately after the last hunk's last line.

[EXAMPLE OF A CORRECTLY FORMATTED HUNK]
--- a/path/To/File.java
+++ b/path/To/File.java
@@ -10,7 +10,7 @@ class Example {{
     unchanged line
     unchanged line
-    old line to remove
+    new line to add
+    another new line
     unchanged line
     unchanged line
"""

    # ----------------------------------------------------------------- run

    def generate(self, bug_info, sources, test_sources=None, test_log=None,
                 issue_text=None, results_dir=None):
        """Generate a fix from the bug description and buggy sources.

        Args:
            bug_info: Free-form text describing the bug.
            sources: List of ``(relative_path, content)`` for the buggy file(s).
            test_sources: Optional list of ``(relative_path, content)`` for the
                regression (trigger) test file(s).
            test_log: Optional free-form text with the regression test's
                failure output.
            issue_text: Optional free-form text with the original bug-tracker
                issue report.
            results_dir: Optional directory; when given, the generation artifacts
                (``fix.diff`` and the raw response) are written there.

        Returns:
            A dict with the generation metadata: ``model``, ``temperature``,
            ``timestamp``, ``elapsed_seconds``, ``diff``, ``raw_response`` and
            ``usage_metadata``. Validation (applying the diff, running tests) is
            the caller's responsibility.
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        prompt = self._build_prompt(bug_info, sources, test_sources, test_log, issue_text)
        print(f"[fixgen] Prompt:\n{prompt}")
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
