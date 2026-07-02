# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

LLM-based automated bug repair on the [Defects4J](https://github.com/rjust/defects4j) benchmark. Given a bug id, it generates a candidate patch with an LLM and evaluates it by running the project's test suite inside a Docker container.

## Commands

```bash
# Set up
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Build the Defects4J Docker image (only if not already available)
docker build -t defects4j:3.0.1 ./defects4j

# Run a full experiment
python Experiment.py --project Lang --bug-id 1 --workdir ./workspace

# Run the fast unit tests (no Docker/Ollama/network)
.venv/bin/python -m pytest test/unit -v

# Run everything, including the Docker/Ollama integration tests
.venv/bin/python -m pytest test/ -v -s

# Run the model-quality benchmark (LLM must actually fix the bug)
.venv/bin/python -m pytest test/e2e --run-benchmark -v -s
```

Tests live in two folders: `test/unit/` (pure, always run) and `test/e2e/`
(need Docker, the `defects4j:3.0.1` image, and/or a live LLM backend; the shared
`lang1_pipeline` fixture in `test/e2e/conftest.py` runs the pipeline once).

## Architecture

The project has a deliberate two-layer separation:

**`Experiment.py`** — owns everything Defects4J- and Docker-specific:
- Starts an ephemeral container from `defects4j:3.0.1`, mounting the host workdir at the same absolute path inside the container (so paths are valid on both sides).
- Runs the full pipeline: checkout → compile (pre-fix) → test (pre-fix) → info → locate sources → generate fix → apply diff → compile (post-fix) → test (post-fix).
- Applies the diff with a sequence of increasingly lenient strategies (`git apply`, `git apply --recount`, `patch --fuzz`) to tolerate LLM-generated diff imperfections.
- Writes artifacts to `results/<project>/<bug_id>/`: `fix.diff`, `raw_response.txt`, `result.json`, `test_before.log`, `test_after.log`, `apply.log`.

**`FixGenerator.py`** — dataset- and Docker-agnostic:
- Receives only `bug_info` (text) and `sources` (list of `(rel_path, content)` tuples); it never touches Docker or Defects4J.
- Builds the prompt, queries the LLM, extracts the unified diff from the response (stripping markdown fences if the model added them), and returns the diff plus generation metadata.
- `normalize_diff()` repairs blank context lines that LLMs commonly emit without their leading space, which would otherwise break `git apply`.

**`docker_utils.py`** — thin wrapper around the Docker SDK: `exec_in_container()` returns an `ExecResult(command, exit_code, output)` dataclass. Everything above uses this instead of the SDK directly.

**`llms/`** — one class per provider (`OllamaLLM`, `AnthropicLLM`, `OpenAILLM`, `OpenRouterLLM`, `GoogleLLM`, `CopilotLLM`). Each implements `is_supported(model_name) -> bool` and `initialize(model, temperature, max_tokens) -> wrapper`. `FixGenerator` iterates the provider list and picks the first match.

## Environment variables

| Variable | Used by | Default |
|---|---|---|
| `OLLAMA_BASE_URL` | `OllamaLLM` | `http://localhost:11434` |
| `ANTHROPIC_API_KEY` | `AnthropicLLM` | — |
| `OPENAI_API_KEY` | `OpenAILLM` | — |
| `OPENROUTER_API_KEY` | `OpenRouterLLM` | — |

API keys can also be placed in a `.env` file at the project root; `conftest.py` loads it automatically for tests via `python-dotenv`.

## Model selection

Models are identified by a prefixed string. The prefix determines the provider:

- `ollama/<name>` → Ollama (local)
- `claude-*` or `anthropic/<name>` → Anthropic
- `gpt-*` → OpenAI
- anything else → OpenRouter

The Ollama daemon used in tests listens on `http://localhost:1995` (non-standard port). Tests read `OLLAMA_BASE_URL`; set it if your daemon is elsewhere.

## Keeping README.md up to date

After any change that affects the public-facing behavior of the project — arguments, defaults, output artifacts, workflow steps, environment variables, or provider support — update `README.md` in the same edit session. Specifically:

- **`Experiment.py`** — if `--model` default, arguments, or the pipeline steps change, update the *Usage* table and *How it works* section.
- **`FixGenerator.py`** — if the prompt strategy, output format, or `results/` artifacts change, update the *Output* section.
- **`llms/`** — if a provider is added, removed, or its env-var / model-id convention changes, update the *Requirements → LLM backend* block.

Do not update README.md for internal refactors that leave the observable behavior unchanged.

## Tests

**Every change to `Experiment.py` or `FixGenerator.py` must be validated by running the unit tests (`.venv/bin/python -m pytest test/unit -v`) before the change is considered done.** If the change adds or alters logic in a pure helper (e.g. `evaluate_fix`, `normalize_diff`, `build_diff_from_blocks`, `extract_java_method`), add or update the corresponding unit test in the same session.

**`test/unit/`** — fast, pure tests (no Docker/Ollama/network, always run):

- `test_fixgenerator_units.py` — `FixGenerator`'s SEARCH/REPLACE parsing, `build_diff_from_blocks` (whitespace-tolerant matching, path resolution, unlocatable-block reporting) and `normalize_diff`.
- `test_experiment_units.py` — `Experiment`'s trigger-based fix criterion (`evaluate_fix`), the failing-test parser, and the Java trigger-method extraction (`extract_java_method` / `extract_trigger_test_code`).

**`test/e2e/`** — integration tests, skipped automatically when the Docker daemon, the `defects4j:3.0.1` image, or the target Ollama model is unavailable.

- `test_fixgenerator_lang1.py` — validates only that `FixGenerator` returns a well-formed unified diff (format check, no patch application).
- `test_experiment_lang1.py` — deterministic pipeline mechanics: trigger tests failing pre-fix → diff applies → project compiles → no new failures.
- `test_benchmark_lang1.py` — the model-quality gate (the LLM's fix makes the trigger tests pass). Marked `benchmark`; runs only with `--run-benchmark` since it depends on the (non-deterministic) model output.
- `conftest.py` — the session-scoped `lang1_pipeline` fixture shared by the two above, so the container pipeline runs once.

The model used by the integration tests defaults to `ollama/gpt-oss:120b` and can be overridden with `FIXGEN_TEST_MODEL`. The `--run-benchmark` flag and `benchmark` marker are registered in the root `conftest.py`.
