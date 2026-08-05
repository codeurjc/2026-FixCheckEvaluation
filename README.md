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
   - Optionally (`--fixcheck`), once the patch is *plausible* (applied and
     every trigger test passing), runs
     [FixCheck](https://github.com/facumolina/fixcheck) (vendored in
     `fixcheck/`) as an overfitting check: it mutates the trigger test's
     inputs, reruns the generated variations against the **patched** program
     — reusing the original test's assertions by default
     (`previous-assertion`) — and measures how similar each failing
     variation's failure trace is to the original bug's. A failing variation
     with high similarity is evidence the patch didn't really fix the
     underlying defect rather than just satisfying the trigger test. This is
     purely advisory: it never changes `fixed`, only adds a `fixcheck` block
     and a `fixcheck_suspicious` flag to `result.json`.
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
     `results/<model>/<project>/Bug_<bug_id>/`.

The LLM connectors live in `llms/` (Google, OpenAI, OpenRouter, Ollama,
Copilot). `LLMCommitAnnotator.py` is a separate, reference usage of the same
connectors.

## Requirements

- Docker, with the `defects4j:3.0.1` image available locally.
  Build it from the bundled context if needed:
  ```bash
  git clone git@github.com:rjust/defects4j.git
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
- (Optional, for `--fixcheck`) The FixCheck jar, built once from the vendored
  `fixcheck/` sources:
  ```bash
  bash scripts/buildFixcheck.sh
  ```
  Needs Docker and network access (the Gradle wrapper downloads Gradle on
  first use).

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
| `--fixcheck` | Run FixCheck on plausible patches (applied and every trigger test passing) as an overfitting check. Requires the jar from `bash scripts/buildFixcheck.sh`. | off |
| `--fixcheck-prefixes` | Number of input variations ("prefixes") FixCheck generates per trigger method. | `25` |
| `--fixcheck-assertions` | FixCheck's assertion-generation strategy: `assert-true`, `previous-assertion`, `replit-code-llm`, `gpt-3.5`, `codellama`, `llama3.1`. The LLM-backed options aren't wired up for this project's container/network setup yet. | `previous-assertion` |
| `--fixcheck-inputs-class` | Force FixCheck's `inputs-class` (e.g. `int`, `java.lang.String`) instead of inferring it from the trigger test source. Also the way to run FixCheck on a trigger test the heuristic considers unmutable (see *Not every bug is a FixCheck subject* below). | heuristic |
| `--fixcheck-similarity-threshold` | Minimum failure-similarity score (0-1) a FixCheck failing variation needs to mark the patch suspicious. | `0.8` |
| `--iteration`   | Iteration index; when set, artifacts go to `results/<model>/<project>/Bug_<bug_id>/<iteration>/` instead of `results/<model>/<project>/Bug_<bug_id>/`. Used by `run_iterations.py`. | none |

### Not every bug is a FixCheck subject

FixCheck generates each test variation by replacing **one literal of
`inputs-class`** in the bug-revealing test, and it only draws that literal
from a statement that is *not* an assertion (see `isAssertion` in
`fixcheck/src/main/java/org/imdea/fixcheck/transform/input/InputTransformer.java`).
Many Defects4J trigger tests are nothing but `assertEquals(...)` lines — Lang
1's `TestLang747` is one — and for those **no `inputs-class` works at all**:
upstream FixCheck dies with `IllegalArgumentException: No locals of type <T>`
and writes no report.

`Experiment.py` detects this up front (`select_fixcheck_inputs` returns no
usable type when the trigger methods have no mutable literal) and skips that
test class with an explanatory message instead of spending minutes on a run
that cannot produce anything. The `fixcheck` block still records the skip, and
`analyzed_test_classes` reports how many trigger classes actually yielded a
report — a `suspicious: false` verdict is only meaningful when that count is
above zero. Pass `--fixcheck-inputs-class` to force a run anyway.

Three related upstream behaviors are worth knowing about when picking subjects:

- **Inherited trigger methods are invisible to FixCheck.** It parses only the
  named test class's own source file, so a trigger like Lang 10's
  `FastDateFormat_ParserTest::testLANG_831` — inherited from
  `FastDateParserTest` — yields no prefixes. Those classes are skipped too.
- **One unmutable method used to sink the whole class.** FixCheck generates
  variations for every method in `test-methods` and lets
  `IllegalArgumentException` escape `main`, so a single method without a
  literal of `inputs-class` aborts the run before any report is written.
  `Experiment.py` therefore passes only the methods it can actually mutate.
- **A prefix that fails to compile aborts the run.** `PrefixRunner` records a
  `null` execution result and `FixCheck.generateSimilarPrefixes` dereferences
  it, so the process dies with a `NullPointerException` and writes no report
  at all — the `non_compiling` count in `report.csv` is unreachable in
  practice. This is an upstream bug; the integration treats it as one more
  advisory failure.

### Repeating a run (non-determinism)

LLM fix generation is non-deterministic — even at `temperature=0`, a large MoE
model served locally can produce a different patch on each call — so a single
run is not a reliable signal. `run_iterations.py` runs `Experiment.py` N times
per bug, wiping the checkout between runs to avoid contamination, storing each
run under `results/<model>/<project>/Bug_<bug_id>/<iteration>/`, and aggregating
each bug's outcomes into `results/<model>/<project>/Bug_<bug_id>/summary.json`
(plus a global summary printed at the end):

```bash
python run_iterations.py --project Lang --bug-id 1 --iterations 5
```

`--bug-id` accepts several ids and inclusive ranges, running `--iterations` runs
for each bug — e.g. `--bug-id 1-5 8` covers bugs 1, 2, 3, 4, 5 and 8:

```bash
python run_iterations.py --project Lang --bug-id 1-5 8 --iterations 10
```

Iterations that are already done are **skipped**: if an iteration's
`result.json` already exists it is reused (marked `skipped`) instead of
re-running `Experiment.py`, so an interrupted run only recomputes the
outstanding work when resubmitted.

`run_iterations.py` mirrors `Experiment.py`'s fix-generation flags (`--model`,
`--temperature`, `--include-test-code`, `--include-test-log`, `--include-issue`,
and the `--fixcheck`/`--fixcheck-*` flags) and forwards them to every run, so
they behave exactly as they do there. Its per-bug and global summaries, and
`summary.json`, also report a `fixcheck_suspicious` count alongside
`applied`/`triggers_fixed`/`fixed`.

## Output

Artifacts are written to `results/<model>/<project>/Bug_<bug_id>/` (or
`results/<model>/<project>/Bug_<bug_id>/<iteration>/` when `--iteration` is
set), where `<model>` is `--model` with any `<provider>/` prefix stripped
(e.g. `ollama/qwen3.6:35b` → `qwen3.6:35b`):

- `prompt.txt` — the exact prompt sent to the LLM.
- `fix.diff` — the unified diff produced by the LLM.
- `raw_response.txt` — the raw LLM response before diff extraction.
- `result.json` — run summary: `applied`, `fixed`, `triggers_fixed`, the bug's
  `trigger_tests`, any `new_failures` the patch introduced, failing-test counts
  before and after, modified files, bug metadata, token usage, the raw LLM
  response, whether the regression test code/log/issue were included in the
  prompt (`included_test_code`, `included_test_log`, `included_issue`), and
  the FixCheck overfitting check's result (`fixcheck`, `fixcheck_suspicious`
  — see below).
- `test_before.log` / `test_after.log` — test suite output before and after the
  fix.
- `apply.log` — output of the `git apply` attempts.
- `regression_test.log` — Defects4J's `failing_tests` file content from
  running the regression (trigger) test(s) in isolation: one entry per
  failing test with the exception type, message, and full stack trace. Only
  written when `--include-test-log` is set.
- `issue.txt` — the fetched bug-tracker issue report; only written when
  `--include-issue` is set.
- `fixcheck/<test-class>/` — only written when `--fixcheck` ran on a plausible
  patch (one subdirectory per trigger test class): `report.csv` and
  `scores-failing-tests.csv` (FixCheck's own output; see
  `fixcheck/src/main/java/org/imdea/fixcheck/writer/ReportWriter.java` and
  `PrefixWriter.java` for their exact format), the generated `passing-tests/`,
  `failing-tests/` and `non-compiling-tests/` prefix sources, and
  `fixcheck.log` (FixCheck's console output). `result.json`'s `fixcheck` block
  mirrors the same data in
  structured form: per-class parsed reports/scores, `analyzed_test_classes`,
  `failing_prefixes`, `max_failure_similarity`, and the `suspicious` verdict
  (a failing variation scored at or above `--fixcheck-similarity-threshold`).
  `fixcheck_suspicious` is the top-level convenience boolean mirroring that
  verdict; both are `null`/`false` when `--fixcheck` wasn't set or the patch
  wasn't plausible enough to run it on. Check `analyzed_test_classes` before
  reading a `false` verdict as evidence of correctness — see *Not every bug
  is a FixCheck subject* above.
