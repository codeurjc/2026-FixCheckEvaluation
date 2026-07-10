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

import difflib
import os
import re
import time
from datetime import datetime, timezone

from llms import GoogleLLM, OpenAILLM, OpenRouterLLM, OllamaLLM, CopilotLLM, AnthropicLLM


# --- SEARCH/REPLACE block handling -------------------------------------------
#
# Rather than ask the model for a unified diff directly — which forces it to
# reproduce exact line numbers and context lines, something weaker models
# routinely hallucinate — we ask for Aider-style SEARCH/REPLACE blocks and build
# the diff ourselves with ``difflib`` against the real source. This removes every
# context-anchoring failure mode: the model only has to name the code to change
# and what to change it to; we do the anchoring against ground-truth text.

_SEARCH_REPLACE_RE = re.compile(
    r"<{5,9} *SEARCH[^\n]*\n(?P<search>.*?)\n?={5,9}[^\n]*\n(?P<replace>.*?)\n?>{5,9} *REPLACE",
    re.DOTALL,
)


def parse_search_replace_blocks(response_text):
    """Parse Aider-style SEARCH/REPLACE blocks from an LLM response.

    Returns a list of ``(path, search, replace)`` tuples. ``path`` is taken from
    the last non-empty, non-fence line preceding each block (empty if none).
    """
    blocks = []
    for match in _SEARCH_REPLACE_RE.finditer(response_text):
        prefix_lines = response_text[: match.start()].split("\n")
        path = ""
        for line in reversed(prefix_lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("```"):
                continue
            # Tolerate a leading "File:" / "path:" label.
            path = re.sub(r"^(?:file|path)\s*:\s*", "", stripped, flags=re.IGNORECASE)
            path = path.strip().strip("`").strip()
            break
        blocks.append((path, match.group("search"), match.group("replace")))
    return blocks


def _normalize_for_match(line):
    """Collapse all whitespace so matching tolerates reformatting (e.g.
    ``for (`` vs ``for(``, changed indentation)."""
    return "".join(line.split())


def _resolve_source_path(block_path, source_paths):
    """Map a block's declared path to one of the real source paths."""
    if block_path in source_paths:
        return block_path
    base = os.path.basename(block_path)
    for rel in source_paths:
        if block_path and (rel.endswith(block_path) or os.path.basename(rel) == base):
            return rel
    if len(source_paths) == 1:
        return source_paths[0]
    return None


def _apply_block(content, search, replace):
    """Replace the first whitespace-insensitive match of ``search`` in
    ``content``. Returns ``(new_content, applied)``."""
    file_lines = content.split("\n")
    search_lines = search.split("\n")
    replace_lines = replace.split("\n")
    if not search or not search_lines:
        return content, False

    norm_file = [_normalize_for_match(l) for l in file_lines]
    norm_search = [_normalize_for_match(l) for l in search_lines]
    span = len(norm_search)
    for i in range(len(file_lines) - span + 1):
        if norm_file[i:i + span] == norm_search:
            new_lines = file_lines[:i] + replace_lines + file_lines[i + span:]
            return "\n".join(new_lines), True
    return content, False


def _unified_file_diff(rel_path, original, patched):
    """Produce a git-appliable unified diff between two file contents."""
    diff = difflib.unified_diff(
        original.split("\n"),
        patched.split("\n"),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm="",
    )
    return "\n".join(diff)


def build_diff_from_blocks(sources, blocks):
    """Apply SEARCH/REPLACE ``blocks`` to ``sources`` and return a unified diff.

    Args:
        sources: List of ``(relative_path, content)``.
        blocks: List of ``(path, search, replace)`` from
            :func:`parse_search_replace_blocks`.

    Returns:
        ``(diff_text, applied, failed)`` where ``diff_text`` is the combined
        unified diff for every changed file, ``applied`` is the number of blocks
        that matched, and ``failed`` is a list of ``(path, reason)`` for blocks
        that could not be located.
    """
    originals = {rel: content for rel, content in sources}
    working = dict(originals)
    source_paths = list(originals)

    applied = 0
    failed = []
    for block_path, search, replace in blocks:
        rel = _resolve_source_path(block_path, source_paths)
        if rel is None:
            failed.append((block_path, "no matching source file"))
            continue
        new_content, ok = _apply_block(working[rel], search, replace)
        if ok:
            working[rel] = new_content
            applied += 1
        else:
            failed.append((block_path or rel, "SEARCH text not found in source"))

    diffs = [
        _unified_file_diff(rel, originals[rel], working[rel])
        for rel in source_paths
        if working[rel] != originals[rel]
    ]
    return "\n".join(d for d in diffs if d), applied, failed


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

    def __init__(self, model="ollama/gpt-oss:20b", temperature=0.0, max_tokens=24576):
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

    def _build_prompt(self, sources, test_sources=None, test_log=None, issue_text=None):
        """Build the fix-generation prompt.

        Args:
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

        first_path = sources[0][0] if sources else "path/To/File.java"

        return f"""[SYSTEM INSTRUCTION]
You are an expert software engineer fixing a real bug. You will be given the \
buggy source file(s). Produce a correct, complete fix.

[BUGGY SOURCE FILE(S)]
{sources_block}
{optional_sections}
[TASK]
Fix the bug so that the failing (triggering) tests pass while keeping all other \
tests passing. Make all changes necessary to fix the bug correctly. Do not alter \
test files.

[OUTPUT FORMAT]
Do NOT output a diff. Describe every change as one or more *SEARCH/REPLACE blocks*.
For each change, write the path of the file to edit on its own line — exactly as \
shown above (e.g. `{first_path}`) — followed immediately by a block in this shape:

<<<<<<< SEARCH
(lines copied VERBATIM from the file shown above)
=======
(the replacement lines)
>>>>>>> REPLACE

Rules:
- The SEARCH section MUST be an exact, contiguous copy of lines from the file \
shown above: same characters, same indentation. Do NOT paraphrase, reformat, \
renumber, add, or omit lines. If it is not a verbatim copy it cannot be located \
and the change is rejected.
- Include enough surrounding lines (aim for 3 or more) so the SEARCH section \
matches exactly one place in the file.
- Keep each block small and focused; use several blocks instead of one large one.
- To insert code, copy an existing anchor into SEARCH and repeat it plus the new \
lines in REPLACE.
- Output ONLY file paths and SEARCH/REPLACE blocks — no diff, no line numbers, no \
explanations, no markdown code fences.

[EXAMPLE]
{first_path}
<<<<<<< SEARCH
        if (hexDigits > 8) {{ // too many for an int
            return createLong(str);
        }}
        return createInteger(str);
=======
        if (hexDigits > 8) {{ // too many for an int
            return createLong(str);
        }}
        if (hexDigits == 8 && firstDigit >= '8') {{
            return createLong(str);
        }}
        return createInteger(str);
>>>>>>> REPLACE
"""

    # ----------------------------------------------------------------- run

    def generate(self, sources, test_sources=None, test_log=None,
                 issue_text=None, results_dir=None):
        """Generate a fix from the buggy sources.

        Args:
            sources: List of ``(relative_path, content)`` for the buggy file(s).
            test_sources: Optional list of ``(relative_path, content)`` for the
                regression (trigger) test file(s).
            test_log: Optional free-form text with the regression test's
                failure output.
            issue_text: Optional free-form text with the original bug-tracker
                issue report.
            results_dir: Optional directory; when given, the generation artifacts
                (``prompt.txt``, ``fix.diff`` and the raw response) are written
                there.

        Returns:
            A dict with the generation metadata: ``model``, ``temperature``,
            ``timestamp``, ``elapsed_seconds``, ``prompt``, ``diff``,
            ``raw_response``, ``usage_metadata`` and the SEARCH/REPLACE
            bookkeeping (``blocks_parsed``, ``blocks_applied``, ``blocks_failed``).
            Validation (applying the diff, running tests) is the caller's
            responsibility.
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        prompt = self._build_prompt(sources, test_sources, test_log, issue_text)
        print(f"[fixgen] Querying LLM ({self.model}) for a fix ...")
        start = time.time()
        response = self.llm.invoke(prompt)
        elapsed = round(time.time() - start, 3)

        # The model returns SEARCH/REPLACE blocks; we anchor them against the
        # real source and build the unified diff ourselves with difflib.
        blocks = parse_search_replace_blocks(response.content)
        diff_text, applied, failed = build_diff_from_blocks(sources, blocks)
        print(
            f"[fixgen] SEARCH/REPLACE blocks: {len(blocks)} parsed, "
            f"{applied} applied, {len(failed)} failed."
        )
        for path, reason in failed:
            print(f"[fixgen]   WARNING: block for {path!r} skipped: {reason}")

        if results_dir is not None:
            self._write_text(results_dir, "prompt.txt", prompt)
            self._write_text(results_dir, "fix.diff", diff_text)
            self._write_text(results_dir, "raw_response.txt", response.content)

        return {
            "model": self.model,
            "temperature": self.temperature,
            "timestamp": timestamp,
            "elapsed_seconds": elapsed,
            "prompt": prompt,
            "diff": diff_text,
            "raw_response": response.content,
            "usage_metadata": getattr(response, "usage_metadata", None),
            "blocks_parsed": len(blocks),
            "blocks_applied": applied,
            "blocks_failed": failed,
        }

    @staticmethod
    def _write_text(results_dir, filename, content):
        os.makedirs(results_dir, exist_ok=True)
        path = os.path.join(results_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
