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

# Run tests
.venv/bin/python -m pytest test/ -v -s

# Run a specific test file
.venv/bin/python -m pytest test/test_fixgenerator_lang1.py -v -s
.venv/bin/python -m pytest test/test_experiment_lang1.py -v -s
```

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

## Integration tests

Both test files share the same skip guards: they are skipped automatically when the Docker daemon, the `defects4j:3.0.1` image, or the target Ollama model is unavailable.

- `test_fixgenerator_lang1.py` — validates only that `FixGenerator` returns a well-formed unified diff (format check, no patch application).
- `test_experiment_lang1.py` — runs the full pipeline and asserts: bug present pre-fix → diff applies → project compiles → 0 failing tests post-fix. Uses a module-scoped fixture so the container runs once for all four assertions.

The model used by the tests defaults to `ollama/gpt-oss:120b` and can be overridden with `FIXGEN_TEST_MODEL`.
