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
     inputs, re-generates the assertions (strategy chosen with
     `--fixcheck-assertions`), reruns the variations against the **patched**
     program, and measures how similar each failing variation's failure trace
     is to the original bug's. A failing variation with high similarity is
     evidence the patch didn't really fix the underlying defect rather than
     just satisfying the trigger test. There is one FixCheck run per trigger
     method and literal type — the method's 100 prefixes split among the
     literal types it contains — and the patch is flagged when a failing
     variation scores at least 0.4: the parameters of FixCheck's own
     evaluation. This is purely advisory: it never changes `fixed`, only adds a
     `fixcheck` block and a `fixcheck_suspicious` flag to `result.json`. The
     vendored FixCheck carries the patches in `scripts/fixcheck-patches/` —
     what each one fixes, and the measured artifact behind it, is in that
     directory's [README](scripts/fixcheck-patches/README.md). Before reporting
     a verdict as a result, read
     [docs/fixcheck-verdict-limitations.md](docs/fixcheck-verdict-limitations.md)
     and [docs/fixcheck-v2-protocol.md](docs/fixcheck-v2-protocol.md).
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
  bash scripts/patchDefects4jImage.sh   # re-run after any base rebuild
  ```
  The second step takes seconds and makes Defects4J's `dir-layout.csv` caches
  writable by the non-root uid the containers run as; without it Chart 26 fails
  with `Permission denied` (see [scripts/README.md](scripts/README.md)).
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
  Needs Docker and network access (the Gradle wrapper downloads Gradle and the
  Shadow plugin on first use). The script clones `fixcheck/` if missing,
  applies the local patches from `scripts/fixcheck-patches/` (see its
  [README](scripts/fixcheck-patches/README.md)), runs FixCheck's own unit
  tests, and refuses a jar that still carries unrelocated dependencies.

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
| `--max-tokens` | Maximum tokens to generate (Ollama `num_predict`). For a reasoning model this budget covers the chain of thought as well as the answer; reaching it is recorded as `generation_status: truncated`, never scored as the model failing. See [docs/generation-budget.md](docs/generation-budget.md). | `32768` |
| `--context-length` | Context window for prompt + generation (Ollama `num_ctx`). `131072` is `gpt-oss:120b`'s native maximum; the daemon must serve at least this much, which `scripts/ollama_serve.sh` ensures by reading the same constant. | `131072` |
| `--include-test-code` | Include the failing trigger test method(s) in the prompt (extracted from the test file, not the whole file). | off |
| `--include-test-log`  | Include the regression (trigger) test's isolated failure log in the prompt. | off |
| `--include-issue`     | Include the original bug-tracker issue report in the prompt. | off |
| `--fixcheck` | Run FixCheck on plausible patches (applied and every trigger test passing) as an overfitting check. Requires the jar from `bash scripts/buildFixcheck.sh`. | off |
| `--fixcheck-prefixes` | Input variations ("prefixes") FixCheck generates per trigger method, split among the literal types (String, int, long, double, boolean) the method can mutate in proportion to how many literals of each it has; one FixCheck run per type. | `100` |
| `--fixcheck-assertions` | FixCheck's assertion-generation strategy: `assert-true`, `previous-assertion`, `codellama`, `llama3.1`, `gpt-3.5`, `replit-code-llm`, or **`ollama:<model>[@[<host>:]<port>]`** for any model an Ollama daemon serves (e.g. `ollama:gpt-oss:120b@1995`). `previous-assertion` keeps the trigger test's own assertions in every variation (restored by the patches in `scripts/fixcheck-patches/` — [background](docs/fixcheck-verdict-limitations.md)); `assert-true` only appends a vacuous `assertTrue(true)`. The Ollama-backed ones generate new assertions with an LLM — see *Ollama-backed assertion generators* below. `gpt-3.5` and `replit-code-llm` aren't wired up for this project's container/network setup yet. | `previous-assertion` |
| `--fixcheck-inputs-class` | Force one `inputs-class` (e.g. `int`, `java.lang.String`): every trigger method gets a single run of that type with the whole prefix budget, instead of the budget being split among the types it contains. | split among the types present |
| `--fixcheck-similarity-threshold` | Minimum failure-similarity score (0-1) a FixCheck failing variation needs to mark the patch suspicious. | `0.4` |
| `--fixcheck-timeout` | Wall-clock budget in seconds for each FixCheck run (one trigger method and literal type); `0` = unbounded. A safety net: prefixes and model calls have budgets of their own. A stop is recorded as `timed_out` and never affects `fixed`; `result.json` is written before FixCheck starts, so an interrupted FixCheck can only cost its own block. | `14400` |
| `--fixcheck-prefix-timeout` | Budget in seconds for running one prefix. One that outlives it — a mutated loop bound or size, as in Math 10 — is stopped, recorded as timed out and not scored; `0` = unbounded. | `60` |
| `--fixcheck-llm-timeout` | Timeout in seconds of each call to an Ollama-backed assertion generator. | `120` |
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

`FixCheckWrapper.plan_fixcheck_runs` therefore counts, for each trigger method,
the literals FixCheck can actually reach — which depends on the generator:
every generator but `previous-assertion` first removes the assertions at any
depth, so a `fail("...")` message inside a `try` offers nothing — and plans one
run per literal type present. A method without a mutable literal is recorded
under `skipped` with its reason instead of spending minutes on a run that
cannot produce anything. `analyzed_runs` reports how many runs actually yielded
a report; a `suspicious: false` verdict is only meaningful when it is above
zero. Pass `--fixcheck-inputs-class` to force a type anyway.

Two related behaviors are worth knowing about when picking subjects:

- **Inherited trigger methods are invisible to FixCheck.** It parses only the
  named test class's own source file, so a trigger like Lang 10's
  `FastDateFormat_ParserTest::testLANG_831` — inherited from
  `FastDateParserTest` — yields no prefixes. Those methods are skipped too.
- **A well-factored test leaves nothing to mutate.** A trigger method that only
  calls a helper with a constant (JxPath 13's
  `doTestCreateAndSetAttribute(DocumentContainer.MODEL_DOM)`) has no literal.

### Ollama-backed assertion generators

An Ollama-backed generator asks a daemon to write each variation's assertions
with an LLM, instead of reusing the trigger test's own. It costs one model call
per prefix — on a local `codellama:7b` generation dominated the run at ~2 min
per prefix, 11 min for Lang 12's larger test — but it is the only way to get an
assertion the original test never made.

**Any model, any port (recommended).** `ollama:<model>[@[<host>:]<port>]` takes
both from the command line:

```bash
python Experiment.py --project Math --bug-id 69 --fixcheck \
    --fixcheck-assertions ollama:gpt-oss:120b@1995
```

- `ollama:gpt-oss:120b` — port `11434` on localhost
- `ollama:gpt-oss:120b@1995` — port `1995` on localhost
- `ollama:llama3.1:8b@gpu-box:11434` — another host

The endpoint is separated with `@` because a colon already belongs to Ollama's
own `<model>:<version>` tags. The model tag is used exactly as written (a bare
name resolves to `<name>:latest`, as Ollama itself does). This is
`assertion/OllamaGenerator.java`, added by
`scripts/fixcheck-patches/0003-generic-ollama-assertion-generator.patch`; it
prompts for bare assertion statements rather than a completed method, since
asking a reasoning model to "complete the code" gets back a whole method that
declares locals the prefix does not have.

**The two legacy generators.** `--fixcheck-assertions codellama` (or
`llama3.1`) still work, but both the endpoint and the model tag are
`private final` fields in
`fixcheck/src/main/java/org/imdea/fixcheck/assertion/CodeLlamaOllama.java`, so:

- **The endpoint is hardcoded to `http://localhost:11434`.**
- **The model name is hardcoded to the bare tag** (`codellama`), which Ollama
  resolves to `codellama:latest`. Having `codellama:7b` pulled is *not* enough.
  Alias it once:

  ```bash
  ollama pull codellama:7b
  ollama cp codellama:7b codellama:latest
  ```

Whichever is used, the daemon must be reachable *from inside the container*.
When it is on the host's loopback, `Experiment.py` starts the container with
`network_mode="host"` (`needs_host_network`); a generator naming a remote host
does not need that. The daemon and the exact tag are checked before the run
starts (`check_ollama_backend`), so a missing daemon or an unpulled tag
produces one clear message instead of an empty report for every trigger class.

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

### Running the whole benchmark

To run every bug of one or more projects — the full 854-bug campaign — use
`scripts/runCampaign.sh`, which submits **one SLURM job per project** (all of
that project's bugs sequentially on its GPU, several projects in parallel):

```bash
./scripts/runCampaign.sh --projects JacksonXml,Csv,Codec       # pilot: 40 bugs
./scripts/runCampaign.sh --projects all --minutes-per-bug 25   # all 854
./scripts/runCampaign.sh --projects all --dry-run              # show, don't submit
```

Each job runs `run_project.py`, which runs `Experiment.py` **once per bug** (as
opposed to `run_iterations.py`'s N times), takes the bug list from Defects4J's
own `active-bugs.csv` so deprecated ids are never attempted, isolates per-bug
failures, enforces a per-bug timeout, deletes each checkout when it is done,
and resumes by skipping bugs that already have a `result.json`. It can also be
run directly, outside SLURM:

```bash
python run_project.py --project Lang                  # all 61 active bugs
python run_project.py --project Lang --bug-id 1,3-5 --dry-run
```

`summarize_campaign.py` aggregates the whole campaign out of `results/`:

```bash
python summarize_campaign.py --list-failures
```

See [scripts/README.md](scripts/README.md) for the options and
[docs/campaign.md](docs/campaign.md) for the protocol.

### Re-measuring FixCheck on recorded patches

`replay_fixcheck.py` runs **only FixCheck** again on patches whose verdict is
already recorded. It re-applies the stored patch — it never asks the model for
a new one, since a re-run would draw a different patch — checks that it still
compiles and passes the trigger tests (recording `not_reproduced` otherwise),
and writes `fixcheck_v2.json` with the FixCheck result:

```bash
# a model's plausible patches, with that model as the assertion oracle
python replay_fixcheck.py --target plausible --model ollama/qwen3.6:35b --project Lang \
    --fixcheck-assertions ollama:qwen3.6:35b@11434
# the controls: developer fixes, and FixCheck's author's labelled DefectRepairing patches
python replay_fixcheck.py --target devfix --project Lang --fixcheck-assertions ollama:gpt-oss:120b@11434
python replay_fixcheck.py --target defectrepairing --config author --project Math \
    --fixcheck-assertions ollama:gpt-oss:120b@11434   # needs external/DefectRepairing
```

A model's replay lands next to its run in `results/<model>/<Project>/Bug_<id>/`;
the controls under `results/controls/devfix/<oracle>/` and
`results/controls/defectrepairing/<author|ours>/<oracle>/`. Mutations are
seeded by bug, test method and literal type, so every patch of one bug meets
the same variations. On the cluster, `scripts/runFixcheckReplay.sh` submits one
job per project, and `summarize_campaign.collect_fixcheck_v2` reads the records
back. The protocol and its status are in
[docs/fixcheck-v2-protocol.md](docs/fixcheck-v2-protocol.md).

### Issue reports are pre-downloaded

`--include-issue` reads each bug's issue report from `d4j/issues/<Project>/<bug_id>.txt`,
downloaded once and committed, so a run needs no network and always sees the
same text:

```bash
python -m d4j.fetch_issues              # all 854, skipping what is cached
python -m d4j.fetch_issues --project Lang --force
```

Fetching at run time does not scale: unauthenticated `api.github.com` allows
**60 requests/hour** and the 280 GitHub-tracked bugs need two calls each, while
the archived Google Code pages another 197 bugs point at render through
JavaScript and cannot be scraped at all. `fetch_issues.py` handles all five
tracker families (Jira, GitHub, Google Code JSON, Google Code archive pages,
SourceForge). Put a `GITHUB_TOKEN` in `.env` before running it — with one, the
allowance is 5000/hour and the whole download takes minutes.

**Only the reporter's original text reaches the prompt.** The issue is cut at
the first `Comment:` line: the maintainers' thread is written *after* the bug
was diagnosed and routinely discusses (sometimes states) the fix, which would
leak the answer. 436 of the 813 issues with content have such a thread, and
dropping it removes 42% of all issue text without leaving a single issue empty.
The comments stay in the cache for inspection.

**Not every bug has a usable issue**, so `result.json` records an
`issue_status` saying which case it was: `available` (814 bugs), `unusable`
(the 22 SourceForge ones, whose pages scrape to a navigation menu rather than
the ticket — Chart 8, Time 14), `empty` (18 Chart bugs with no URL, plus one
Jsoup issue GitHub no longer has), or `not-requested`. Chart therefore
contributes no issue at all. See
[docs/campaign.md](docs/campaign.md#40-bugs-carry-no-issue-for-three-different-reasons).

## Output

Artifacts are written to `results/<model>/<project>/Bug_<bug_id>/` (or
`results/<model>/<project>/Bug_<bug_id>/<iteration>/` when `--iteration` is
set), where `<model>` is `--model` with any `<provider>/` prefix stripped
(e.g. `ollama/qwen3.6:35b` → `qwen3.6:35b`):

- `prompt.txt` — the exact prompt sent to the LLM.
- `fix.diff` — the unified diff produced by the LLM.
- `raw_response.txt` — the raw LLM response before diff extraction.
- `result.json` — run summary: `applied`, `compiled_after`, `fixed`,
  `triggers_fixed`, the bug's
  `trigger_tests`, any `new_failures` the patch introduced, failing-test counts
  before and after, modified files, bug metadata, token usage, the raw LLM
  response, whether the regression test code/log/issue were included in the
  prompt (`included_test_code`, `included_test_log`, `included_issue`), and
  the FixCheck overfitting check's result (`fixcheck`, `fixcheck_suspicious`
  — see below). `included_issue` reflects whether an issue *actually* reached
  the prompt, and `issue_status` says why when it did not
  (`available` / `unusable` / `empty` / `fetch-failed` / `not-requested`);
  `fetch-failed` is kept apart from `empty` so a rate limit is never recorded
  as "this bug has no issue".

  It also records **why an empty patch is empty**, which used to be
  unknowable: `generation_status` (`ok` / `truncated` / `context_exhausted` /
  `empty_response`), `done_reason`, `response_truncated`, `context_exhausted`,
  `reasoning_chars`, and the SEARCH/REPLACE bookkeeping `blocks_parsed` /
  `blocks_applied` / `blocks_failed`. Without these, a model that hit its token
  ceiling mid-answer is indistinguishable from one that had no fix — 46 runs of
  the first campaign were the former and were reported as the latter.
  `unidentified_failing_lines` and `masked_triggers` flag a run whose verdict
  rests on failing-test lines the parser could not read.

  `post_fix_tests` is `"completed"`, `"did_not_terminate"` or `null` (no patch
  applied). The patched suite runs under a budget of 5x the pre-fix suite's
  duration, clamped to 30-90 min (`post_fix_test_budget`); a patch that makes it
  outrun that is recorded as not fixed, with `compiled_after` *measured* by a
  separate `defects4j compile` rather than assumed. Before this, such a patch
  ran into the per-bug timeout and the run left no `result.json` at all.

  Re-running a bug never deletes the previous attempt: its directory is moved to
  `results/old/superseded/<UTC timestamp>/...`, and a warning is printed when it
  held a verdict — a run with a verdict is the run of record and should not be
  re-run (see [docs/campaign.md](docs/campaign.md), "The verdict of record").
- `run_status.json` — only written by `run_project.py`: how the run itself
  went (`status` of `ok`/`error`/`timeout`, `exit_code`, `seconds`) alongside
  the outcome flags. This is what distinguishes "the model did not fix it"
  from "the run never completed", and what a resumed campaign consults.
- `test_before.log` / `test_after.log` — test suite output before and after the
  fix.
- `apply.log` — output of the `git apply` attempts.
- `regression_test.log` — Defects4J's `failing_tests` file content from
  running the regression (trigger) test(s) in isolation: one entry per
  failing test with the exception type, message, and full stack trace. Only
  written when `--include-test-log` is set.
- `issue.txt` — the fetched bug-tracker issue report; only written when
  `--include-issue` is set.
- `fixcheck/<TestClass>/<method>/<literal type>/` — only written when
  `--fixcheck` ran on a plausible patch, one directory per FixCheck run:
  `fixcheck.properties`, `fixcheck.log` (FixCheck's console output, with each
  prefix's mutation, outcome and similarity) and `fixcheck-output/`
  (`report.csv`, `scores-failing-tests.csv`, and the generated
  `passing-tests/`, `failing-tests/`, `non-compiling-tests/` and
  `timed-out-tests/` prefix sources; see
  `fixcheck/src/main/java/org/imdea/fixcheck/writer/`). `result.json`'s
  `fixcheck` block (`schema: fixcheck-v2`) mirrors it in structured form: the
  plan — `runs`, each with its literal counts, prefix share, seed, parsed report
  and per-prefix `variations`, and the `skipped` methods with their reason —
  and the aggregate: `analyzed_runs`, `analyzed_test_classes`,
  `failing_prefixes`, `scored_prefixes`, `timed_out_prefixes`,
  `identity_prefixes`/`identity_failing_prefixes` (mutations that left the
  literal unchanged: such a prefix failing measures the harness, not the
  patch), `max_failure_similarity` (`null` when nothing was scored) and the
  `suspicious` verdict (a failing variation scored at or above
  `--fixcheck-similarity-threshold`). `fixcheck_suspicious` is the top-level
  convenience boolean mirroring that verdict; both are `null`/`false` when
  `--fixcheck` wasn't set or the patch wasn't plausible. Check `analyzed_runs`
  before reading a `false` verdict as evidence of correctness — see *Not every
  bug is a FixCheck subject* above.
- `fixcheck_v2.json`, `fixcheck_v2/`, `replay_patch.diff`, `replay_apply.log` —
  written by `replay_fixcheck.py` (see *Re-measuring FixCheck on recorded
  patches*) next to a run of record, never over its `result.json`.
