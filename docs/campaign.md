# Full-benchmark campaign protocol

What was run, with which knobs, so the numbers in `results/` stay interpretable
after the fact. Fill in the launch dates and git shas as each wave goes out.

## Protocol

| | |
|---|---|
| Subjects | all **854 active** Defects4J bugs, 17 projects |
| Runs per bug | **1** (no `run_iterations.py`) |
| Model (fix) | `ollama/gpt-oss:120b`, `--temperature 0.0` |
| Model (FixCheck assertions) | the **same** model, as `ollama:gpt-oss:120b@<port>` |
| Prompt context | `--include-test-code --include-test-log --include-issue` |
| FixCheck | on, `--fixcheck-prefixes 10`, similarity threshold `0.8` (default) |
| Generation budget | `--max-tokens 32768`, `--context-length 131072` |
| Per-bug timeout | 10800 s |
| Hardware | one H100 per project job; jobs queue behind the node's 4 H100s |

Bug ids come from `defects4j/framework/projects/<P>/active-bugs.csv` via
`defects4j_bugs.py`, so deprecated ids (Lang 2/18/25/48, Cli 6, Closure 63/93,
JacksonDatabind 65/89, Time 21) are never attempted.

## Waves

| Wave | Projects | Bugs | Submitted | Git sha |
|---|---|---|---|---|
| Archived campaign | all 17 | 854 x 2 | 2026-08-11 .. 2026-09-06 | mixed (4 SHAs; see audit-2026-09.md) |
| Post-audit rerun | all 17 | 854 x 2 | 2026-09-09 | `803bbb1` **+ 24 uncommitted files** |
| Rerun completion | 8 project/model pairs | 159 + 71 | 2026-09-10 | `2f6511d` + fixes below (uncommitted) |

### Post-audit rerun, 2026-09-09

Jobs `16175`-`16200` are `ollama/gpt-oss:120b` on `H100:1`; `16201`-`16226` are
`ollama/qwen3.6:35b` on `L40S:1`. 26 jobs per model, one per project except the
five largest, which are chunked so no single job holds the tail:

| Project | Bugs | Chunks |
|---|---:|---:|
| Closure | 174 | 4 |
| JacksonDatabind | 110 | 3 |
| Math | 106 | 3 |
| Jsoup | 93 | 2 |
| Lang | 61 | 2 |
| the other 12 | 310 | 1 each |

Verified before launch by recomputing the plan through `resolve_bug_ids` +
`chunk`: 26 jobs, 854/854 bugs, nothing lost to chunking.

`--minutes-per-bug 15`, `--timeout 10800`, `--fixcheck-prefixes 10`,
`--max-tokens 32768`, `--context-length 131072`, `--temperature 0.0`,
`--include-test-code --include-test-log --include-issue`.

**Provenance caveat**: the manifests record `git_sha=803bbb1`, but the tree had
24 uncommitted files at launch — every fix from the September audit. The sha
alone does **not** identify the code that produced these results. Commit before
relying on it.

### Completion pass, 2026-09-10

The rerun did not finish on its own. Two further defects surfaced while it ran
(audit §1.12 and §1.13), both fixed before resubmitting what was left:

- **Wrong physical GPU.** CUDA numbered devices `FASTEST_FIRST` while SLURM
  numbers them in PCI order, so gpt-oss jobs given H100 0 or 1 ran on 46 GB L40S
  cards at ~0.5 tok/s; five hit SLURM's wall clock (`TIMEOUT` in sacct) with
  few bugs done, and two jobs fell back to CPU. Fixed with
  `CUDA_DEVICE_ORDER=PCI_BUS_ID`, plus a per-bug GPU-placement check in
  `run_project.py`.
- **FixCheck hangs erased verdicts.** Math 10 and 13 (both models) passed every
  test, then FixCheck hung until the per-bug timeout killed the run and no
  `result.json` survived. Fixed with a 1800 s FixCheck budget and by writing
  the verdict before FixCheck starts.

Cancelled as broken: `16193`, `16203` (wrong device) and `16226` (qwen Time,
fell back to CPU after a collision with `16193`). Verified on gpt-oss Math 10
(`16230`) before resubmitting. Resubmitted with explicit bug lists and
`--retry-errored`: `16231`-`16237` (gpt-oss, 159 runs) and `16238`-`16241`
(qwen, 71 runs); `16225` (qwen Mockito) was left running on its correct card.

**FixCheck verdicts degraded by the slow device.** FixCheck's assertion
generator calls the same model; at 0.2 tok/s (job `16193`, gpt-oss on an L40S)
it hit `SocketTimeoutException` on gpt-oss Compress 3, 4, 6 and 7, and three of
the four ended vacuous. Their `fixed` verdicts were correct, but their FixCheck
verdicts were not evidence, and `--retry-errored` would not have redone them
(status `ok`). Rerun with `--no-resume` as job `16242`; the degraded runs are
kept in `results/old/degraded-fixcheck-2026-09-10/`. The archived campaign has
the same symptom on gpt-oss Chart 8, 11, 13 and Compress 46 (under Vulkan);
left as is, since the rerun supersedes it.

**Checked and cleared**: re-deriving all 1,525 stored verdicts from the raw logs
found no contradiction, so neither defect wrote a wrong `fixed`; they only
slowed runs down or left them without a result.

**Consequences for the analysis**

- **Mixed code within jobs.** `run_project.py` starts a fresh `Experiment.py`
  per bug, so jobs already running when the fixes landed used the new code for
  their remaining bugs. Verdicts are computed identically in both versions;
  only the FixCheck timeout and the provisional `result.json` differ.
- **Do not use wall-clock time as a cost measure** for this campaign: 96
  completed runs generated on the wrong card type and many more on a card of
  the right type but not the one allocated, possibly shared. Use tokens.
- **FixCheck timeouts are a vacuous-verdict category of their own**
  (`timed_out_test_classes > 0`), distinct from crashes and skips.

**Two false starts precede this one**, both worth knowing when reading the logs:
jobs `16121`-`16172` were cancelled minutes in (the Vulkan GPU-isolation defect,
audit §1.11), and `16173`/`16174` are the single-bug smoke test on Lang 1 that
validated the fix. Lang 1's results come from those two jobs, since `--resume`
skipped it afterwards; they ran the same configuration.

```bash
# pilot
./scripts/runCampaign.sh --projects JacksonXml,Csv,Codec --minutes-per-bug 20
# measure, then
.venv/bin/python summarize_campaign.py --list-failures
# full launch, with the measured p90
./scripts/runCampaign.sh --projects all --minutes-per-bug <measured>
```

Each job writes `scripts/logs/<job_id>/manifest.json` with the git sha, argv,
model, Ollama port and resolved bug list. **Copy those somewhere durable**:
both `results/` and `scripts/logs/` are gitignored, so they are the only record
tying a number to the code that produced it.

## How to read the results

Three fields answer different questions and are routinely conflated:

- `applied` — the diff was applicable at all.
- `triggers_fixed` — the bug's trigger tests pass again.
- `fixed` — `triggers_fixed` **and** no test that passed before now fails.
  `fixed` never depends on FixCheck.

`fixcheck_suspicious` is **advisory and a single sample**. The model is
non-deterministic even at temperature 0, and FixCheck's own mutation and
assertion generation are random per run, so one run per bug cannot measure an
overfitting *rate* — it observes one draw. Read
[fixcheck-verdict-limitations.md](fixcheck-verdict-limitations.md) before
reporting any verdict: a `suspicious: false` is weak evidence, not proof the
patch is good, and `analyzed_test_classes > 0` is necessary for it to mean
anything at all.

Also worth knowing when comparing projects: `included_issue` is now
`bool(issue_text)`, so a bug whose issue is genuinely unavailable records
`false` rather than claiming it was in the prompt.

## The verdict of record

**The first run whose patch was generated and evaluated is the run of record.
Only runs the harness broke *before* producing a verdict are re-run.**

Re-running a run that already has a verdict does not re-measure it. The model
is not deterministic even at temperature 0, so a re-run draws a *different*
patch -- all four Compress re-runs below did. Worse, the reason for re-running
is rarely independent of the outcome: FixCheck only runs on plausible patches,
so every run re-rolled because of FixCheck was a success, and re-rolling only
successes can lower a model's count but never raise it. It did, on 2026-09-10:

| Run | First verdict | Re-roll |
|---|---|---|
| gpt-oss Compress 3, 4 | fixed | **not fixed** |
| gpt-oss Compress 6, 7 | fixed | fixed |
| gpt-oss Math 10, 13; qwen Math 10 | fixed (job log) | fixed |
| qwen Math 13 | fixed (job log) | **not fixed** |

`scripts/apply_first_verdict_rule.py` (dry run by default, idempotent) restored
the verdicts of record and moved the re-rolls to `results/old/rerolled-2026-09-10/`:

- **gpt-oss Compress 3, 4, 6, 7** -- originals restored. Their FixCheck is
  marked `fixcheck_degraded` (its assertion generator timed out while the model
  ran on the wrong GPU), which `collect_project` reads as *no measurement*.
- **qwen Math 13** -- the first run's verdict, reconstructed from its job log
  (`Failing tests after fix: 0`, then FixCheck hung). `verdict_source:
  "job_log"`; its patch is lost because the re-run cleared the directory.
- **gpt-oss Closure 74** -- the patch applied and the test suite never
  finished within 3 h. That is the model's outcome, recorded as not fixed with
  `post_fix_tests: "did_not_terminate"`, not re-run: re-running only failures
  would bias the other way. Compilation was *measured* by re-applying the saved
  patch (it compiles), not assumed.

What the rule allows re-running: timeouts and errors where no verdict was
produced (a generation cut off by a wrong device, a killed job, a harness
crash). `--retry-errored` does exactly that. **`--no-resume` re-rolls every
run it touches and must not be used on runs that have a verdict.**

`audit.rederive` lists reconstructed verdicts separately from divergences,
since they have no run artifacts to re-derive from.

**Hardened afterwards**, so neither repair should be needed again: `Experiment.py`
now moves a previous attempt to `results/old/superseded/` instead of deleting it
(which is how qwen Math 13's first patch was lost), and bounds the patched test
suite so a hang like Closure 74's is recorded by the pipeline itself -- with the
marker `POST-FIX TESTS DID NOT TERMINATE` in `test_after.log`, which
`audit.rederive` reads -- instead of being reconstructed by hand.

## Issue reports

The issues are **not** fetched during the campaign. `python -m d4j.fetch_issues`
downloads all 854 once into `d4j/issues/<Project>/<bug_id>.txt` (committed), and
`Experiment.py` reads from there. Fetching at run time was not viable:

| Tracker | Bugs | Why it needed handling |
|---|---:|---|
| Jira (`issues.apache.org`) | 337 | fine over the API |
| GitHub | 280 | 2 calls each vs **60/hour** unauthenticated → needs `GITHUB_TOKEN` |
| Google Code JSON (Closure) | 174 | JSON, not HTML; the generic scraper produced raw JSON |
| Google Code archive pages (Mockito) | 23 | JavaScript-only: scraping returns 210 bytes of boilerplate, so the URL is rewritten to the archive's JSON |
| SourceForge (Chart, Time) | 22 | **excluded** — see below |
| `report.url = UNKNOWN` (Chart) | 18 | no issue exists; cached as empty |

Re-running the fetcher only downloads what is missing, so a failed tracker can
be retried without touching the rest.

### The prompt carries the original report only

The issue is cut at the first `Comment:` line before it reaches the prompt
(`strip_issue_comments`). The maintainers' thread is written *after* the bug was
diagnosed and routinely discusses — sometimes states — the fix, so including it
would be answer leakage: at report time no repair tool could have had it.

| | |
|---|---|
| Issues with a comment thread | **436 of 813 (54%)** |
| Issue text dropped | 1471 KB → 859 KB (**−42%**) |
| Issues left empty by the cut | **0** |
| Jira-tracked bugs affected | 0 — they never had comments |

Per project: Closure 692 comments, JacksonDatabind 618, Jsoup 224, Mockito 201,
JacksonCore 72, Gson 56, JacksonXml 14, Time 10. The eight Jira projects (Cli,
Codec, Collections, Compress, Csv, JxPath, Lang, Math) are untouched, which also
removes an asymmetry: until now only the GitHub and Google Code bugs carried any
discussion at all.

**Residual leakage this does not fix**, and which should be stated when
reporting results:

- **15 bugs name the fix in the report body itself** (a patch, a commit hash or
  "fixed in X") — Closure 5, JacksonDatabind 3, Math 3, Compress 2, Cli 1,
  Gson 1. That text is part of the original report; removing it would mean
  hand-curating each one.
- **A few "issues" are pull requests** whose title already states the fix, e.g.
  Gson 6 (*"Fixed a regression in Gson 2.6 where…"*) and JacksonCore 13
  (*"Fix UTF8JsonGenerator to allow QUOTE_FIELD_NAMES to be toggled"*).

### 41 bugs carry no issue, for three different reasons

`--include-issue` is not uniform across the benchmark, and `result.json`'s
`issue_status` says which case each bug is:

| `issue_status` | Bugs | Meaning |
|---|---:|---|
| `available` | 813 | real text went into the prompt |
| `unusable` | 22 | SourceForge (Chart 8, Time 14). Its tickets render inside a navigation shell and the HTML-to-text extraction keeps all of it, so the cached files open with ~40 lines of *"Join/Login / Business Software / Open Source Software / …"* before any ticket text. Feeding that to a model is worse than feeding nothing, so it is excluded by `UNUSABLE_ISSUE_HOSTS`. The files stay on disk — the exclusion is policy, not deletion. |
| `empty` | 19 | 18 Chart bugs Defects4J has no URL for, plus Jsoup 45, whose GitHub issue was deleted |
| `not-requested` | — | the run did not pass `--include-issue` |

**Chart therefore contributes no issue at all** (18 blank + 8 unusable = its 26
bugs), and 14 of Time's 26 are in the same position. Worth remembering before
comparing per-project fix rates: those projects' prompts are strictly smaller
than the rest.

## Infrastructure fix: Chart 26

Chart 26 was the only bug of the 854 that never produced a result, failing
identically for every model ~5 s in:

```
Cannot open file for appending .../Chart/dir-layout.csv: Permission denied
```

Defects4J caches per-revision source/test directory layouts in
`framework/projects/<P>/dir-layout.csv` and, on a cache **miss**, appends the
layout it just determined. Chart 26's buggy revision `102` is the only revision
in the whole benchmark absent from its project's layout map, so it is the only
bug that ever writes — and the file is `root:root 644` in the image while the
containers run as the host uid.

`scripts/patchDefects4jImage.sh` makes those CSVs writable (one derived layer,
retagged in place, seconds to build) so Defects4J computes and caches the layout
itself instead of us hardcoding a guess at the missing entry. **Re-run it after
any rebuild of the base image**; `run_project.py` warns when the image lacks the
`org.fixcheckeval.layout-writable` label.

Note this is a *run-completeness* fix, not an issue-context one: Chart still
contributes no issue text at all (18 bugs with no URL + 8 unusable SourceForge).

## Evaluation fix: patches that never compiled

`defects4j test` prints no `Failing tests:` line when the patched sources fail
to compile, so `parse_failing_tests` returns `-1` and the parsed failure list
comes back **empty** — which is indistinguishable from "no test fails". The
original `evaluate_fix` therefore scored a patch that does not even build as a
perfect `fixed`.

Measured over the campaign: **203 runs** (104 qwen3.6:35b, 99 gpt-oss:120b)
applied but never compiled, and *every one of them* had been recorded as fixed
— about **20% of every reported fix**.

`evaluate_fix` now takes an `evaluated` flag and `result.json` records
`compiled_after`. No re-run was needed: `failing_tests_after == -1` identifies
the affected runs exactly (verified against the stored `test_after.log` of all
1546 applied runs — 1343 compiled and carry the line, 203 did not and do not,
with zero ambiguous cases), so `summarize_campaign.collect_project` re-derives
the flag for older results and reports the corrected `fixed`, keeping
`fixed_as_recorded` for audit.

Corrected headline over the 847 paired bugs: **qwen3.6:35b 400 (47.2%)**,
**gpt-oss:120b 425 (50.2%)** — down from 493/516. The gap between the models is
essentially unchanged; the absolute rates are not.


## Generation budget: why 32768 / 131072

The first campaign ran with `num_predict=24576` and `num_ctx=49152`, neither
reachable from the command line, and **the budget decided 46 runs' outcomes**:
30 stopped at the output cap and 16 exhausted the context window, every one of
them recorded as the model failing to fix the bug. All 46 are `qwen3.6:35b` and
none is `gpt-oss:120b`, because Ollama's `eval_count` counts a reasoning model's
chain of thought as output: qwen's median is 5829 output tokens against
gpt-oss's 1694. **A flat token budget is not neutral between a reasoning model
and a concise one.**

Both new values are sized from the campaign, not guessed:

| | Measured | Chosen |
|---|---|---|
| Output | largest *successful* generation used 18595 tokens | **32768** (~75% headroom) |
| Context | largest prompt is ~136k tokens; 17 of 854 exceed 49152, 10 exceed 65536, 3 exceed 98304 | **131072** |

### Why 131072 exactly

Not a round number picked for headroom: **it is `gpt-oss:120b`'s native context
length.** Measured from the daemon:

| Model | Native context | Params |
|---|---:|---:|
| `qwen3.6:35b` | 262144 | 36.0 B |
| `gpt-oss:120b` | **131072** | 116.8 B |

qwen could take twice as much, but the two models must get the *same* window or
the comparison reacquires the asymmetry this change exists to remove — and
asking gpt-oss for more than 131072 would be clamped silently, which is the
exact class of defect being fixed. So the shared window is the smaller model's
ceiling.

Three bugs therefore still have prompts too large to leave the full output
budget, and `JacksonDatabind/Bug_30` (~136k tokens, a single 205 KB source file)
does not fit at all — for `gpt-oss` it *cannot*, at any setting. Those are now
**recorded** as `context_exhausted` rather than counted as failed repairs, and
the prompt itself is deliberately unchanged so results stay comparable with the
archived campaign.

### The new ceiling is also reached, and that is not a reason to raise it

Two of the first 31 runs of the new campaign hit `num_predict=32768` --
`qwen3.6:35b` on JacksonDatabind 2 and 77, both with `done_reason='length'` and
an empty answer after ~135k characters of reasoning. The instinct is to raise
the budget again; the data says otherwise. **32768 is already 2.7x qwen's
largest *successful* generation** (12043 output tokens; median 5857), the
context window is not the binding constraint there (input + output = 46807 of
131072), and at the measured ~135 tok/s a bigger ceiling would only spend more
GPU to reach the same place.

What changed is that those runs are now recorded as `truncated` instead of
counted as the model failing to fix the bug -- which is the whole point of the
audit fix. Reasoning and the rule for future campaigns:
[generation-budget.md](generation-budget.md).

### Measured VRAM (2026-09-09, `scripts/probeContextVram.sh`)

Both fit entirely on GPU, with no CPU spill, and the window is close to free:

| Model | GPU | 49152 | 131072 | KV cost of the change |
|---|---|---:|---:|---:|
| `qwen3.6:35b` | L40S (46068 MiB) | 22910 MiB | **24830 MiB** | +1920 MiB |
| `gpt-oss:120b` | H100 NVL (95830 MiB) | 61540 MiB | **61700 MiB** | +160 MiB |

Re-measure on different hardware before assuming this still holds.

**The daemon and the client must agree.** `scripts/ollama_serve.sh` reads
`OLLAMA_CONTEXT_LENGTH` from `FixGenerator.DEFAULT_CONTEXT_LENGTH` instead of
repeating it, because a per-request `num_ctx` above what the model was loaded
with is silently clamped — and because FixCheck's `OllamaGenerator` sends no
options at all, a mismatch makes Ollama start a *second* runner and thrash.
Measure before committing to a window on new hardware:

```bash
bash scripts/probeContextVram.sh qwen3.6:35b 49152 131072
bash scripts/probeContextVram.sh gpt-oss:120b 131072
```

A non-zero `CPU_SPLIT` means the model did not fit and would run partly on CPU
— which does not fail, it just gets slow enough to hit the per-bug timeout, far
from the cause.

## Archived campaigns

`results/old/9-Sep/` holds the first full campaign (848 gpt-oss + 852 qwen
runs), audited in [audit-2026-09.md](audit-2026-09.md). The audit tooling reads
it directly:

```bash
.venv/bin/python -m audit.rederive --results-dir results/old/9-Sep
```
