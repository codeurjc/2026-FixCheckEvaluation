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
bash scripts/patchDefects4jImage.sh   # ALWAYS re-run after a base rebuild

# Build the FixCheck jar (only needed for --fixcheck; requires Docker + network)
bash scripts/buildFixcheck.sh

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

The project has a deliberate layer separation:

**`Experiment.py`** — owns everything Defects4J- and Docker-specific:
- Starts an ephemeral container from `defects4j:3.0.1`, mounting the host workdir at the same absolute path inside the container (so paths are valid on both sides).
- Runs the full pipeline: checkout → compile (pre-fix) → test (pre-fix) → info → locate sources → generate fix → apply diff → compile (post-fix) → test (post-fix).
- Applies the diff with a sequence of increasingly lenient strategies (`git apply`, `git apply --recount`, `patch --fuzz`) to tolerate LLM-generated diff imperfections.
- Optionally (`--fixcheck`), on a plausible patch (applied and every trigger test passing), delegates to `FixCheckWrapper` for the overfitting check and copies its per-class `fixcheck-output/`/`fixcheck.log` into `results_dir/fixcheck/<simple_name>/`. Advisory only — never affects `fixed`. Requires the jar from `bash scripts/buildFixcheck.sh`.
- Writes artifacts to `results/<project>/<bug_id>/`: `fix.diff`, `raw_response.txt`, `result.json`, `test_before.log`, `test_after.log`, `apply.log`.

**`FixGenerator.py`** — dataset- and Docker-agnostic:
- Receives only `sources` (list of `(rel_path, content)` tuples), plus optional test/issue context; it never touches Docker or Defects4J.
- Builds the prompt, queries the LLM, extracts the unified diff from the response (stripping markdown fences if the model added them), and returns the diff plus generation metadata.
- `normalize_diff()` repairs blank context lines that LLMs commonly emit without their leading space, which would otherwise break `git apply`.

**`FixCheckWrapper.py`** — runs the vendored `fixcheck/` jar as an overfitting check, kept apart from `Experiment.py` so it can be built and tested independently:
- The `FixCheckWrapper` class takes its configuration (`num_prefixes`, `assertion_generator`, `similarity_threshold`, `inputs_class`) directly through `__init__` rather than an argparse `Namespace`, and its `run(container, workdir, trigger_tests, trigger_method_sources)` mutates the trigger test's inputs, reruns the variations against the patched program, and flags the patch suspicious when a failing variation closely matches the original failure.
- Still needs a running Defects4J container and shared-volume `workdir` (compiling, exporting classpaths and invoking the jar all happen inside it), but has no dependency on `Experiment.py` or the LLM fix-generation pipeline.
- Also owns the FixCheck-specific pure helpers: the `inputs-class` heuristic (`select_fixcheck_inputs`, mirroring FixCheck's own assertion-exclusion logic), the `.properties` renderer, and the `report.csv`/`scores-failing-tests.csv` parsers.
- `parse_ollama_generator` mirrors `properties/OllamaProperty.java`'s parsing of the `ollama:<model>[@[<host>:]<port>]` assertion-generator spec — **keep the two in step**, since the Python side picks the container's network mode (`needs_host_network`) and probes the daemon (`check_ollama_backend`) before the jar ever reads the option. `validate_assertion_generator` is the argparse `type` for `--fixcheck-assertions`, which cannot use a static `choices=` list because that spec is open-ended.
- **Treat a `suspicious: false` as weak evidence, not proof the patch is good.** Two upstream defects used to gut the check (assertion-stripping under `previous-assertion`, and a Java 9+-dead stack-trace normalization); both are fixed by the patches in `scripts/fixcheck-patches/`, which `scripts/buildFixcheck.sh` applies automatically after cloning — never edit `fixcheck/` directly without regenerating those patches, since the clone is gitignored and local edits are lost on re-clone. Remaining limitations (role-blind literal mutation, non-reproducible runs) are documented in [docs/fixcheck-verdict-limitations.md](docs/fixcheck-verdict-limitations.md) — read it before reporting any FixCheck verdict as a result.

**`d4j/` + `experiment_runner.py` / `run_project.py` / `summarize_campaign.py`** — the full-benchmark campaign (854 bugs, one run per bug; see [docs/campaign.md](docs/campaign.md)). `d4j/` holds what knows about the *benchmark itself*, as opposed to the pipeline that runs on it:
- **The prompt gets only the reporter's original text.** `strip_issue_comments` cuts the issue at the first `Comment:` line, because the maintainers' thread is written *after* the diagnosis and routinely discusses — sometimes states — the fix, which is answer leakage in a repair benchmark. The marker is reliable because our own fetchers write it, not the scraping. Measured: 436 of the 813 issues with content (54%) carry a thread, cutting removes 42% of all issue text, and **none** is left empty. The 337 Jira bugs are untouched (the fetcher only requests `summary,description`), which also removes an asymmetry — until then only GitHub/Google Code bugs carried discussion at all. Applied in `issue_text`, not in the fetcher: the cache keeps the thread as auditable evidence.
- `d4j/fetch_issues.py` downloads every bug's issue report **once** into the versioned `d4j/issues/<Project>/<bug_id>.txt`, so a campaign never touches a tracker. It has to handle five families, and two of them are traps: the 174 Closure bugs point at Google Code **JSON**, and the 23 `code.google.com` Mockito pages render through JavaScript — scraping one returns 210 bytes of "enable JavaScript", so those URLs are rewritten to the archive's JSON. 18 Chart bugs have `report.url = UNKNOWN` and are cached as empty, which `load_cached_issue` deliberately reports as `""` (not `None`) so they are never re-fetched.
- `d4j/defects4j_bugs.py` owns which bugs exist. Ids are **not contiguous** (deprecated bugs keep their id reserved), so `active_bug_ids` reads Defects4J's own `active-bugs.csv` — with fallbacks to `$DEFECTS4J_HOME` and `defects4j bids` in the image, since `defects4j/` is gitignored — and `resolve_bug_ids` **rejects a deprecated id by name** instead of letting it burn minutes of container time on a doomed checkout.
- `experiment_runner.py` holds what every bulk runner shares: `parse_bug_ids` (commas *and* spaces — `sbatch --export` cannot carry a space), `experiment_args`/`add_experiment_flags` (forward only user-set flags so `Experiment.py` keeps its defaults), `clean_checkout`, `load_result`, plus `run_experiment` (per-bug timeout: nothing else bounds a run, as the Ollama client sets none) and `reap_containers`. **`run_iterations.py` imports these; do not reintroduce a second copy.**
- `run_project.py` runs one project's bugs once each. Per-bug failures are isolated (`Experiment.py` exits 1 on a failed checkout/compile, which is expected for a fair number of bugs), each checkout is deleted afterwards (~400 MB apiece; `Experiment.py` only clears at the start), and `--resume` skips bugs with a `result.json`. Its `preflight()` aborts when `OLLAMA_BASE_URL` and `--fixcheck-assertions` name **different ports** — on this single-node cluster that would silently mean another job's daemon.
- `summarize_campaign.py` aggregates from `results/`, not from any job's summary, so it is immune to chunking and re-submission. It also **re-scores historical runs**: `compiled_after` is re-derived from `failing_tests_after == -1` for results written before the guard below, and `fixed`/`triggers_fixed` are corrected accordingly, with `fixed_as_recorded` kept so the size of the correction stays auditable.
- **A patch that applies but does not compile must never count as fixed.** `defects4j test` prints no `Failing tests:` line when compilation fails, so the parsed failure list is empty — indistinguishable from "no test fails". `evaluate_fix` therefore takes an `evaluated` flag (false when `parse_failing_tests` returned `-1`) and `result.json` records `compiled_after`. This was not hypothetical: 203 campaign runs (104 qwen3.6:35b, 99 gpt-oss:120b) applied without compiling and **every one had been scored as fixed**, inflating the reported fix rate by ~20%.
- **`audit/` re-derives every verdict from the raw artifacts, and imports nothing from `Experiment.py` on purpose** — a second opinion that shares the first one's parser is not one. `python -m audit.rederive` recomputes `applied`/`compiled_after`/`triggers_fixed`/`fixed` from `apply.log` + `test_before.log` + `test_after.log` and reports every disagreement with `result.json`; `python -m audit.inventory` censuses the artifacts and flags incoherent run directories. Both keep "not measured" in the type: `UNKNOWN` raises `TypeError` on `bool()` so no call site can quietly read it as `False`. Findings and their measured impact are in [docs/audit-2026-09.md](docs/audit-2026-09.md). **Run `audit.rederive` after any change to how a verdict is computed** — it is what catches a defect of the shape that has now bitten twice: a value that degraded silently into something a legitimate value could also look like.
- **A missing measurement must never be recorded as a measured zero.** `collect_project` returns `None` (not `0`/`0.0`) for `fixcheck_analyzed`, `failing_prefixes` and `max_failure_similarity` when FixCheck never ran, and it keeps `fixcheck_invoked` (Experiment.py called it), `fixcheck_ran` (it got as far as running) and `fixcheck_analyzed` apart. `bool(fixcheck)` counted the 203 non-compiling patches as FixCheck runs — FixCheck is gated on the *pre-guard* `triggers_fixed`, so it was invoked on all of them and aborted on all of them — which is why the CLI table and the notebook once disagreed by exactly 203 on that column.
- **`scripts/patchDefects4jImage.sh` must be re-run after every `docker build` of the base image** — it retags `defects4j:3.0.1` in place after `chmod a+w`-ing the per-project `dir-layout.csv` files. Defects4J appends a newly-determined directory layout there on a cache miss, and the stock file is root-owned 644 while containers run as the host uid; Chart 26 (buggy revision 102, absent from Chart's layout map) is the one bug in the benchmark that hits it and dies ~5 s in with no result. The image carries the label `org.fixcheckeval.layout-writable=1` and `run_project.warn_if_image_unpatched()` warns when it is missing. Same trap as editing `fixcheck/` without regenerating its patches.
- `Experiment.py`'s `get_issue_text` reads the cache first and only falls back to the network, so `--include-issue` works offline and reproducibly. It returns `(text, status)` and `result.json` records **both** `included_issue` and `issue_status`: 41 of the 854 bugs have no usable issue for three different reasons (`unusable` — the 22 SourceForge ones scrape to a navigation menu, Chart 8 + Time 14; `empty` — 18 Chart bugs with no URL and one deleted Jsoup issue), and a bare `""` made them indistinguishable. **Chart contributes no issue at all.** The exclusion lives in `UNUSABLE_ISSUE_HOSTS` and is policy, not deletion: the scraped files stay in `d4j/issues/` as evidence.

**`docker_utils.py`** — thin wrapper around the Docker SDK, used by everything above instead of the SDK directly: `exec_in_container()` returns an `ExecResult(command, exit_code, output)` dataclass; `run_step()` and `export_property()` are small Defects4J-command conveniences built on it.

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
- `test_campaign_units.py` — the campaign's pure helpers: `parse_bug_ids`, `active_bug_ids`/`resolve_bug_ids`/`chunk` (including a cross-check that the vendored Defects4J still totals **854** bugs across the 17 projects), `experiment_args`, and `run_experiment`'s ok/error/timeout paths against stub scripts. Also guards two things whose failure would be silent on a single-node cluster: that no SLURM script pattern-kills Ollama with `pkill`, and that `project_job.sbatch` still *derives* `--fixcheck-assertions` from `$MODEL`.
- `test_fixcheck_units.py` — the FixCheck integration's pure helpers: `group_triggers_by_class`, the `select_fixcheck_inputs` inputs-class heuristic, `build_fixcheck_properties`, the `report.csv`/`scores-failing-tests.csv` parsers, and the `ollama:<model>[@[<host>:]<port>]` spec parsing (`parse_ollama_generator`, `needs_host_network`, `check_ollama_backend`, `validate_assertion_generator`).

**`test/e2e/`** — integration tests, skipped automatically when the Docker daemon, the `defects4j:3.0.1` image, or the target Ollama model is unavailable.

- `test_fixgenerator_lang1.py` — validates only that `FixGenerator` returns a well-formed unified diff (format check, no patch application).
- `test_experiment_lang1.py` — deterministic pipeline mechanics: trigger tests failing pre-fix → diff applies → project compiles → no new failures.
- `test_benchmark_lang1.py` — the model-quality gate (the LLM's fix makes the trigger tests pass). Marked `benchmark`; runs only with `--run-benchmark` since it depends on the (non-deterministic) model output.
- `test_fixcheck_devfix.py` — runs FixCheck against a bug's actual *developer* fix (`<id>b` diffed against the `<id>f` checkout, not an LLM guess) and asserts it is not flagged suspicious, plus that FixCheck really compiled and ran prefixes so the verdict isn't vacuous. **Parametrized over `FIXCHECK_BUGS`** (`(project, bug_id)` pairs, test id `<Project>-<id>`); add a candidate subject by appending to that list. Since most Defects4J bugs are not usable FixCheck subjects, a bug whose every trigger class is skipped (all-assertion trigger tests like Lang 1's, or inherited ones like Lang 10's) is reported as a **skip carrying the reason**, not a failure — so `failed` always means something actually broke. Each run's artifacts are copied to `logs/test/<Project>_<BugId>/` (`fixcheck.log` with the generated assertions, `fixcheck-output/` with the generated prefix sources and `report.csv`, the developer diff and the pre-fix failure trace) for manual inspection; the per-generator directory is wiped at the start of each run. Also parametrized over `--fixcheck-assertions` (comma-separated), which accepts the `ollama:<model>[@[<host>:]<port>]` form too. Skipped unless the FixCheck jar is built (`bash scripts/buildFixcheck.sh`), independently of the Docker/Ollama skip checks above. The pipeline itself lives in `fixcheck_devfix_pipeline.py`.
- `test_fixcheck_ollama_generator.py` — the configurable Ollama assertion generator (`ollama:<model>[@[<host>:]<port>]`). Asserts the run reached a report, that the **configured** model and port were the ones used (read from `fixcheck.log`, so a silent fallback to the hardcoded `localhost:11434` fails), and that the model's assertions reached the generated prefix sources. Subject is **Lang 12**, deliberately: FixCheck runs each variation without assertions first and only calls the generator when it did not crash, and an `int` `inputs-class` (Math 69) can crash every variation before the generator is reached — the fixture skips with that reason if it happens. Defaults to `gpt-oss:120b` on port `1995`; override with `FIXCHECK_OLLAMA_TEST_MODEL` / `FIXCHECK_OLLAMA_TEST_PORT`.
- `test_experiment_fixcheck_integration.py` — the **`Experiment.py` ↔ `FixCheckWrapper.py` seam**, the only test that runs `Experiment.main()` itself with `--fixcheck`. Covers what neither side's own test can: the pre-fix failure trace being captured *before* the patch (afterwards Defects4J has overwritten `failing_tests`), the trigger-method sources reaching `select_fixcheck_inputs`, the `fixcheck` block and `fixcheck_suspicious` in `result.json`, the artifacts copied into `results_dir/fixcheck/<SimpleName>/`, and `fixed` staying independent of the verdict. The **LLM is replayed** from `fixtures/lang12_raw_response.txt` (through `FixGenerator._initialize_llm`, so prompt building and SEARCH/REPLACE parsing stay real) because FixCheck only runs on a plausible patch — with live generation a model that missed the bug would turn the whole test vacuous. FixCheck's assertion generator *is* parametrized over `--fixcheck-assertions`, so the same wiring can be checked with `previous-assertion` (fast, no model) or `ollama:gpt-oss:120b@1995`.
- `fixcheck_devfix_pipeline.py` — not a test: the shared developer-fix preamble (checkout `<id>b`/`<id>f` → diff → apply → verify the trigger tests pass → run FixCheck → copy artifacts to `logs/test/`) used by both FixCheck e2e tests, so the two cannot drift apart.
- `conftest.py` — the session-scoped `lang1_pipeline` fixture shared by the two Lang 1 tests, so the container pipeline runs once.

The model used by the integration tests defaults to `ollama/gpt-oss:120b` and can be overridden with `FIXGEN_TEST_MODEL`. The `--run-benchmark` flag and `benchmark` marker are registered in the root `conftest.py`.
