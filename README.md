# 2026-FixCheckEvaluation

LLM-based bug fix generation and evaluation on the
[Defects4J](https://github.com/rjust/defects4j) benchmark.

Given a Defects4J bug, the experiment generates a candidate fix with a Large
Language Model and evaluates it by running the project's test suite: the fix is
considered **fixed** when the previously failing tests pass and the full suite
passes.

## How it works

1. **`Experiment.py`** orchestrates a single run:
   - Starts an *ephemeral* Docker container from the `defects4j:3.0.1` image,
     mounting the host working directory as a shared volume (same absolute path
     inside and outside the container).
   - Checks out the buggy version (`defects4j checkout -p <project> -v <id>b`).
   - Compiles it and runs the test suite to confirm the bug is present.
   - Extracts bug metadata (`defects4j info`).
   - Delegates to `FixGenerator`.
   - Always stops and removes the container at the end.
2. **`FixGenerator.py`** generates and evaluates the fix:
   - Locates the buggy source file(s) via `defects4j export`
     (`classes.modified`, `dir.src.classes`) and reads them from the shared
     volume.
   - Builds a prompt and asks the LLM for a **unified diff**.
   - Applies the diff with `git apply` inside the container.
   - Re-runs `defects4j test`.
   - Stores all artifacts under `results/<project>/<bug_id>/`.

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

## Output

Artifacts are written to `results/<project>/<bug_id>/`:

- `fix.diff` — the unified diff produced by the LLM.
- `result.json` — run summary: `applied`, `fixed`, failing-test counts before
  and after, modified files, bug metadata, token usage and the raw LLM response.
- `test_before.log` / `test_after.log` — test suite output before and after the
  fix.
- `apply.log` — output of the `git apply` attempts.
