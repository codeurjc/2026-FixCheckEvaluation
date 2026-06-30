"""
FixGenerator.py — Generate, apply and evaluate an LLM bug fix.

Called from ``Experiment.py`` for a single Defects4J bug. It:
  1. Locates and reads the buggy source file(s) in the working directory.
  2. Builds a prompt describing the bug and asking for a unified diff.
  3. Queries the LLM and extracts the diff from the response.
  4. Applies the diff with ``git apply`` inside the container.
  5. Re-runs the test suite to check whether the bug is fixed.
  6. Stores all artifacts under ``results/<project>/<bug_id>/``.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

from llms import GoogleLLM, OpenAILLM, OpenRouterLLM, OllamaLLM, CopilotLLM, AnthropicLLM
from docker_utils import exec_in_container


def parse_failing_tests(output: str) -> int:
    """Parse the number of failing tests from ``defects4j test`` output.

    Returns the count, or -1 if the expected line is not present.
    """
    match = re.search(r"Failing tests:\s*(\d+)", output)
    return int(match.group(1)) if match else -1


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
    """Generates and evaluates an LLM fix for a single Defects4J bug."""

    DIFF_FILENAME = "_llm_fix.diff"  # temp diff written into the mounted workdir

    def __init__(self, project, bug_id, workdir, container,
                 model="ollama/gpt-oss:20b", temperature=0.0, max_tokens=8192):
        self.project = project
        self.bug_id = str(bug_id)
        self.workdir = workdir
        self.container = container
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

        self.results_dir = os.path.join("results", project, self.bug_id)
        os.makedirs(self.results_dir, exist_ok=True)

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

    # ----------------------------------------------------------- source I/O

    def _export(self, prop):
        """Return the value of a Defects4J export property as a string.

        ``defects4j export`` interleaves ant progress messages with the value on
        the combined stream, so we write the value to a file with ``-o`` and read
        it back from the shared volume to get a clean result.
        """
        out_file = os.path.join(self.workdir, f".export_{prop}")
        result = exec_in_container(
            self.container,
            f"defects4j export -p {prop} -o {out_file} -w {self.workdir}",
            workdir=None,
        )
        if not result.ok:
            print(f"[fixgen] WARNING: export of '{prop}' failed:\n{result.output}")
            return ""
        try:
            with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def _locate_source_files(self):
        """Map modified classes to their source file paths on the host.

        Returns a list of (relative_path, absolute_path) tuples.
        """
        src_dir = self._export("dir.src.classes")
        modified = self._export("classes.modified")
        classes = [c.strip() for c in modified.splitlines() if c.strip()]

        files = []
        for fq_class in classes:
            rel_path = os.path.join(src_dir, fq_class.replace(".", "/") + ".java")
            abs_path = os.path.join(self.workdir, rel_path)
            files.append((rel_path, abs_path))
        return files

    def _read_sources(self, files):
        """Read source files, returning a list of (relative_path, content)."""
        sources = []
        for rel_path, abs_path in files:
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    sources.append((rel_path, f.read()))
            except FileNotFoundError:
                print(f"[fixgen] WARNING: source file not found: {abs_path}")
        return sources

    # -------------------------------------------------------------- prompt

    def _build_prompt(self, bug_info, sources):
        """Build the fix-generation prompt."""
        source_sections = []
        for rel_path, content in sources:
            source_sections.append(
                f"--- FILE: {rel_path} ---\n{content}"
            )
        sources_block = "\n\n".join(source_sections)

        return f"""[SYSTEM INSTRUCTION]
You are an expert Java software engineer fixing a real bug from the Defects4J \
benchmark. You will be given bug metadata and the buggy source file(s). Produce \
a minimal fix.

[BUG METADATA]
{bug_info}

[BUGGY SOURCE FILE(S)]
{sources_block}

[TASK]
Fix the bug so that the failing (triggering) tests pass while keeping all other \
tests passing. Change as little as possible and do not alter test files.

[OUTPUT FORMAT]
Respond with ONLY a single Git-compatible unified diff describing the changes.
- Use standard headers: `--- a/<path>` and `+++ b/<path>`, where <path> is the \
file path shown above (e.g. `{sources[0][0] if sources else 'path/To/File.java'}`).
- Include `@@ ... @@` hunk headers with correct line context.
- Do NOT wrap the diff in markdown code fences.
- Do NOT include any explanation before or after the diff.
"""

    # --------------------------------------------------------------- apply

    def _apply_diff(self, diff_text):
        """Write the diff into the mounted workdir and apply it with git.

        Returns (applied: bool, apply_log: str).
        """
        diff_path = os.path.join(self.workdir, self.DIFF_FILENAME)
        normalized = normalize_diff(diff_text)
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write(normalized if normalized.endswith("\n") else normalized + "\n")

        logs = []
        # Try a sequence of increasingly lenient strategies. LLM-generated diffs
        # often have slightly wrong hunk line counts or blank context lines, so
        # we use --recount/--ignore-whitespace and finally fall back to `patch`,
        # which tolerates fuzzy context.
        commands = [
            f"git -C {self.workdir} apply {diff_path}",
            f"git -C {self.workdir} apply --recount --ignore-whitespace {diff_path}",
            f"git -C {self.workdir} apply --recount --ignore-whitespace -p0 {diff_path}",
            f"patch -d {self.workdir} -p1 --fuzz=3 -i {diff_path}",
            f"patch -d {self.workdir} -p0 --fuzz=3 -i {diff_path}",
        ]
        for cmd in commands:
            result = exec_in_container(self.container, cmd, workdir=None)
            logs.append(f"$ {result.command}\n(exit {result.exit_code})\n{result.output}")
            if result.ok:
                return True, "\n\n".join(logs)
        return False, "\n\n".join(logs)

    # ----------------------------------------------------------------- run

    def run(self, bug_info, test_before_log, failing_before):
        """Generate, apply and evaluate the fix; persist all artifacts."""
        timestamp = datetime.now(timezone.utc).isoformat()

        # 1. Locate and read buggy sources.
        files = self._locate_source_files()
        sources = self._read_sources(files)
        if not sources:
            print("[fixgen] WARNING: no buggy source files could be read.")

        # 2-3. Build prompt and query the LLM.
        prompt = self._build_prompt(bug_info, sources)
        print(f"[fixgen] Querying LLM ({self.model}) for a fix ...")
        start = time.time()
        response = self.llm.invoke(prompt)
        elapsed = round(time.time() - start, 3)

        diff_text = extract_diff(response.content)

        # 4. Apply the diff.
        applied, apply_log = self._apply_diff(diff_text)
        print(f"[fixgen] Diff applied: {applied}")

        # 5. Re-run tests (post-fix).
        failing_after = -1
        test_after_log = ""
        if applied:
            test_after = exec_in_container(
                self.container, "defects4j test", workdir=self.workdir
            )
            test_after_log = test_after.output
            failing_after = parse_failing_tests(test_after_log)
            print(f"[fixgen] Failing tests after fix: {failing_after}")

        fixed = applied and failing_after == 0

        # 6. Persist artifacts.
        self._write_text("fix.diff", diff_text)
        self._write_text("apply.log", apply_log)
        self._write_text("test_before.log", test_before_log)
        self._write_text("test_after.log", test_after_log)

        result = {
            "project": self.project,
            "bug_id": self.bug_id,
            "model": self.model,
            "temperature": self.temperature,
            "timestamp": timestamp,
            "elapsed_seconds": elapsed,
            "applied": applied,
            "fixed": fixed,
            "failing_tests_before": failing_before,
            "failing_tests_after": failing_after,
            "modified_files": [rel for rel, _ in files],
            "bug_metadata": bug_info,
            "usage_metadata": getattr(response, "usage_metadata", None),
            "raw_response": response.content,
        }
        self._write_text("result.json", json.dumps(result, indent=2))

        return result

    def _write_text(self, filename, content):
        path = os.path.join(self.results_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
