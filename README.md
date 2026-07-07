# 2026-FixCheckEvaluation

LLM-based bug fix generation and evaluation on the
[Defects4J](https://github.com/rjust/defects4j) benchmark.

Given a Defects4J bug, the experiment generates a candidate fix with a Large
Language Model and evaluates it by running the project's test suite: the fix is
considered **fixed** when all of the bug's trigger tests pass again and the
patch introduces no new failures (no test that passed before now fails). This
trigger-based criterion — rather than requiring a zero total — is robust to
environment-flaky tests (e.g. `SystemUtils`' user-home test under `HOME=/tmp`)
that fail regardless of the patch.

## How it works

1. **`Experiment.py`** owns everything Defects4J- and Docker-specific:
   - Starts an *ephemeral* Docker container from the `defects4j:3.0.1` image,
     mounting the host working directory as a shared volume (same absolute path
     inside and outside the container).
   - Checks out the buggy version (`defects4j checkout -p <project> -v <id>b`).
   - Compiles it and runs the test suite to confirm the bug is present.
   - Extracts bug metadata (`defects4j info`) — kept in `result.json` for
     reference, no longer passed to the LLM.
   - Locates the buggy source file(s) via `defects4j export`
     (`classes.modified`, `dir.src.classes`) and reads them from the shared
     volume.
   - Optionally (see *Usage* below) gathers three extra pieces of context: the
     failing trigger test method(s) — located via `defects4j export -p
     tests.trigger` + `dir.src.tests` and reduced to just the failing method(s)
     rather than the whole test file — that test's isolated failure log
     (`defects4j test -t <test>`), and the original bug-tracker issue report
     (fetched from the `Bug report url` in `defects4j info`, via the Jira or
     GitHub REST API, or a generic HTML fetch for other trackers).
   - Delegates **fix generation** to `FixGenerator`, passing the buggy source
     contents and any of the optional context gathered above.
   - Applies the generated diff with `git apply` inside the container and
     **validates** it by re-running `defects4j test`, then checking the bug's
     trigger tests pass and no new failures were introduced.
   - Writes the validation artifacts (`apply.log`, `test_before.log`,
     `test_after.log`) and the combined `result.json`.
   - Always stops and removes the container at the end.
2. **`FixGenerator.py`** is a dataset- and Docker-agnostic fix generator:
   - Receives the buggy source contents, and optionally the regression test
     source, its failure log, and the issue report (it never touches Docker or
     Defects4J itself).
   - Asks the LLM for **SEARCH/REPLACE blocks** (an exact snippet of the
     original code plus its replacement) rather than a raw diff. This avoids
     the line-number and context hallucinations that make LLM-produced diffs
     fail to apply.
   - Anchors each block against the real source (whitespace-tolerant matching)
     and builds the **unified diff itself** with `difflib`, so the resulting
     diff always matches the file.
   - Returns the diff plus generation metadata, and writes the generation
     artifacts (`prompt.txt`, `fix.diff`, `raw_response.txt`) under
     `results/<project>/<bug_id>/`.

The LLM connectors live in `llms/` (Google, OpenAI, OpenRouter, Ollama,
Copilot). `LLMCommitAnnotator.py` is a separate, reference usage of the same
connectors.

## Requirements

- Docker, with the `defects4j:3.0.1` image available locally.
  Build it from the bundled context if needed:
  ```bash
  docker build -t defects4j:3.0.1 ./defects4j
  ```
- Python virtual environment with the dependencies installed:
  ```bash
  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```
- A running LLM backend. By default the experiment uses **Ollama**:
  ```bash
  ollama serve            # start the daemon
  ollama pull gpt-oss:20b # download the default model
  ```
  Set `OLLAMA_BASE_URL` if Ollama is not on `http://localhost:11434`.
  Other providers can be selected via `--model` (they read their API keys from
  the environment):
  - **Anthropic (Claude)** — `--model claude-opus-4-8` (or `anthropic/<id>`),
    `ANTHROPIC_API_KEY`.
  - **OpenAI** — `--model gpt-4o`, `OPENAI_API_KEY`.
  - **OpenRouter** (default fallback) — any other model id, `OPENROUTER_API_KEY`.

## Usage

```bash
python Experiment.py --project Lang --bug-id 1 --workdir ./workspace
```

Arguments:

| Argument        | Description                                              | Default              |
|-----------------|----------------------------------------------------------|----------------------|
| `--project`     | Defects4J project name (e.g. `Lang`, `Math`).            | required             |
| `--bug-id`      | Numeric bug id (the `b` suffix is added automatically).  | required             |
| `--workdir`     | Host directory for the checkout (mounted as a volume).   | required             |
| `--model`       | LLM model identifier.                                    | `ollama/gpt-oss:20b` |
| `--temperature` | LLM sampling temperature.                                | `0.0`                |
| `--include-test-code` | Include the failing trigger test method(s) in the prompt (extracted from the test file, not the whole file). | off |
| `--include-test-log`  | Include the regression (trigger) test's isolated failure log in the prompt. | off |
| `--include-issue`     | Include the original bug-tracker issue report in the prompt. | off |
| `--iteration`   | Iteration index; when set, artifacts go to `results/<project>/<bug_id>/<iteration>/` instead of `results/<project>/<bug_id>/`. Used by `run_iterations.py`. | none |

### Repeating a run (non-determinism)

LLM fix generation is non-deterministic — even at `temperature=0`, a large MoE
model served locally can produce a different patch on each call — so a single
run is not a reliable signal. `run_iterations.py` runs `Experiment.py` N times
for one bug, wiping the checkout between runs to avoid contamination, storing
each run under `results/<project>/<bug_id>/<iteration>/`, and aggregating the
outcomes into `results/<project>/<bug_id>/summary.json`:

```bash
python run_iterations.py --project Lang --bug-id 1 --iterations 5
```

`run_iterations.py` mirrors `Experiment.py`'s fix-generation flags (`--model`,
`--temperature`, `--include-test-code`, `--include-test-log`, `--include-issue`)
and forwards them to every run, so they behave exactly as they do there.

## Output

Artifacts are written to `results/<project>/<bug_id>/` (or
`results/<project>/<bug_id>/<iteration>/` when `--iteration` is set):

- `prompt.txt` — the exact prompt sent to the LLM.
- `fix.diff` — the unified diff produced by the LLM.
- `raw_response.txt` — the raw LLM response before diff extraction.
- `result.json` — run summary: `applied`, `fixed`, `triggers_fixed`, the bug's
  `trigger_tests`, any `new_failures` the patch introduced, failing-test counts
  before and after, modified files, bug metadata, token usage, the raw LLM
  response, and whether the regression test code/log/issue were included in the
  prompt (`included_test_code`, `included_test_log`, `included_issue`).
- `test_before.log` / `test_after.log` — test suite output before and after the
  fix.
- `apply.log` — output of the `git apply` attempts.
- `regression_test.log` — output of running the regression (trigger) test(s)
  in isolation; only written when `--include-test-log` is set.
- `issue.txt` — the fetched bug-tracker issue report; only written when
  `--include-issue` is set.
