# FixCheck v2: protocol, status and handoff

Status on 2026-09-16: **Phases 1-3 are done and committed. Phase 4 (the full
run) has not started; it awaits the user's decision** (see "Pilot log"). This document is what is needed to
pick the work up on another machine.

## Why a v2

The archived campaign's FixCheck verdicts were dominated by artifacts of how
FixCheck was run, not by the patches. A manual inspection (Spanish notes for
the meeting with FixCheck's author, `notes/informe-fixcheck-reunion.md`) found:

- The fat jar shaded commons-cli, commons-lang3 and commons-collections. Cli,
  Lang and Collections subjects therefore ran against FixCheck's own copies,
  not the patched program.
- Prefixes were loaded by a separate class loader, so a package-private access
  threw `IllegalAccessError`. That caused 42% of all failing prefixes.
- JUnit 3 classes were run whole for each prefix, so failures of sibling tests
  were scored.
- One non-compiling prefix aborted the run.
- The Defects4J `--- Class::method` header stayed in the original trace.
- The working directory was a temporary one, which broke relative resources.
- The `inputs-class` heuristic was poor, and only 10 prefixes were used, with a
  threshold of 0.8.

FixCheck's author reviewed those notes. His two requests were the parameters of
his own study: **threshold 0.4** and **100 prefixes**. He found the rest of the
proposals acceptable.

## Protocol

| Item | Decision |
|---|---|
| Patches | The **patches of record are re-applied, never regenerated**; results go to `fixcheck_v2.json`, and `result.json` is never touched |
| Oracle | FixCheck's assertions are written by **the model that generated the patch** (`ollama:<model>@<port>`) |
| Controls | Developer fixes (all 854 bugs, with **both** oracles). The 94 plausible patches that break other tests. FixCheck's author's DefectRepairing patches with his configuration (`author`) and with ours (`ours`), both oracles |
| Prefixes | **100 per trigger method**, split among the literal types present (String, int, long, double, boolean) in proportion to their mutable literals, at least 1 per type. **One FixCheck run per (class, method, type)** |
| Similarity | FixCheck's metric unchanged; only the Defects4J header is stripped from the original trace; **suspicious ⇔ some scored prefix ≥ 0.4** |
| Timeouts | **60 s per prefix**, 300 s per model call (120 s until 2026-09-15, see "Pilot log"), 4 h per run. A prefix that times out is recorded as `timed_out`, one whose model call fails as `assertion_generation_failed`: neither is scored or failing |
| Reasoning | The oracle keeps the model's default reasoning, as when it generated the patch (decided 2026-09-15) |
| Determinism | Seed derived from `(bug, class, method, type)`, **never the model**, so every patch of a bug meets the same mutations. The LLM oracle gets `temperature 0` and the same seed |
| Mutations | Null and repeated mutations are **kept**. Identity mutations (a literal replaced by itself) are reported as a free noise control: one of them failing measures the harness, not the patch |

### What changed in FixCheck (Phase 1, commit `1ac9735`)

The patches are in `scripts/fixcheck-patches/`; their README gives the evidence
for each one.

| Patch | Change |
|---|---|
| 0004 | Relocate dependencies (Shadow plugin) |
| 0005 | Child-first subject class loader per prefix |
| 0006 | Run only the mutated method |
| 0007 | A non-compiling prefix no longer aborts the run |
| 0008 | Per-prefix timeout |
| 0009 | Seed |
| 0010 | `output-dir`, and Ollama `options` |
| 0011 | Qualified `junit.framework.TestSuite` |
| 0012 | A failed or timed-out assertion-generator call no longer aborts the run (added in Phase 3) |

Each patch has JUnit tests. `scripts/buildFixcheck.sh` runs them and rejects a
jar that still carries unrelocated dependencies. They are written to be
proposed upstream; whether to submit them is the user's decision.

### Integration (Phase 2)

- **`FixCheckWrapper.py`**
  - Plans the runs: `count_mutable_literals`, `allocate_prefixes`,
    `plan_fixcheck_runs` and `derive_seed`.
  - Writes one header-free trace per trigger method, and one directory per run.
  - Parses `timed_out_prefixes` and each prefix's mutation, outcome and score.
  - `summarize_fixcheck_runs` produces the result, with schema `fixcheck-v2`.
- **`Experiment.py`, `experiment_runner.py` and the campaign scripts** use the
  new defaults and flags: `--fixcheck-prefixes 100`,
  `--fixcheck-similarity-threshold 0.4`, `--fixcheck-timeout 14400`,
  `--fixcheck-prefix-timeout 60` and `--fixcheck-llm-timeout 300`.
- **`replay_fixcheck.py`**, with `scripts/runFixcheckReplay.sh` and
  `scripts/replay_fixcheck_job.sbatch`.
  - Targets `plausible`, `devfix` and `defectrepairing` (`--config author|ours`).
  - For each subject:
    1. Check out `<id>b` and write the pre-fix traces.
    2. Apply the patch.
    3. Compile, then check the triggers pass. If not, record `not_reproduced`
       and stop there.
    4. Run FixCheck and write `fixcheck_v2.json` atomically.
  - With `--config author` it also:
    - uses his test, methods and inputs-class from
      `fixcheck/experiments/defect-repairing-subjects.csv`;
    - uses his trace (the buggy full suite's `failing_tests`);
    - copies in the test sources he adapted by hand
      (`fixcheck/experiments/defects-repairing/<PatchId>/`, 6 patches).
- **`d4j/developer_fix.py`**: `developer_diff`, shared with the e2e pipeline.
- **`summarize_campaign.collect_fixcheck_v2`** reads the records back, with
  `None` for anything not measured.

Output layout:

```
results/<model>/<Project>/Bug_<id>/fixcheck_v2.json                     # plausible
results/controls/devfix/<oracle>/<Project>/Bug_<id>/fixcheck_v2.json
results/controls/defectrepairing/<author|ours>/<oracle>/<Project>/<PatchId>/fixcheck_v2.json
    …/fixcheck_v2/<TestClass>/<method>/<type>/{fixcheck.properties,fixcheck.log,fixcheck-output/}
```

## Validation done so far

| Check | Result |
|---|---|
| FixCheck JUnit tests, jar inspection (`buildFixcheck.sh`) | green; no unrelocated class |
| `pytest test/unit` | 275 passed |
| e2e `test_experiment_fixcheck_integration.py` (Lang 12, `previous-assertion`) | 6 passed. Runs split across String, int and boolean. `testLANG805` skipped with its reason; one identity mutation detected |
| e2e `test_fixcheck_devfix.py` (Lang 12, Math 69) | 8 passed |
| Replay `devfix` Cli 35 | `ok`. The traces show the checkout's `DefaultParser.java:398/371/239`, so shading is gone. Flagged at 0.919 |
| Replay `defectrepairing --config author` Lang Patch151 | `ok`: 5 prefixes, 4 failing, max 0.325, not flagged. Without the author's adapted test it had no String local outside assertions (`No locals of type java.lang.String`) |
| `python -m audit.rederive` | only the known divergence (gpt-oss Mockito 15, masked trigger, docs/audit-2026-09.md §1.2); no verdict computation changed |

## Known items to carry into the pilot

- **Developer fixes do get flagged**, and not because of an artifact:
  - Math 69 scores 0.863 because a mutated data point (`1 → 96`) drives the
    p-value into the documented MATH-371 underflow.
  - Cli 35 scores 0.919 on a genuinely ambiguous option.

  The `devfix` control exists to measure this rate. The e2e test now checks
  that a verdict is well-formed, not that it is negative. It keeps a stricter
  threshold of 0.8, because it tests the integration, not the method.
- **`replay_status: ok` means FixCheck ran, not that it analysed something.**
  Always read `fixcheck.analyzed_runs`. A `suspicious: false` with
  `analyzed_runs == 0` is not a measurement.
- **The LLM oracle may not be bit-reproducible** despite `temperature 0` and a
  seed, because of GPU nondeterminism. The mutations are reproducible. The
  pilot measures how often the assertions are too (criterion 7).
- `runFixcheckReplay.sh --bug-id` applies the same ids to every listed
  project, so submit a cross-project pilot one project at a time.
- For a model's plausible patches, `plausible_subjects` excludes the runs whose
  verdict comes only from a job log (`verdict_source: job_log`) or that have
  no `fix.diff`. The exclusions and their reasons are listed in the job's
  `manifest.json`.

## Handoff: continuing on another machine

1. **Code.** Commit `1ac9735` (Phase 1) and the Phase 2 commit are on local
   `main` and **not pushed**: push them, or copy the repository.
2. **Environment.**
   ```bash
   python -m venv .venv && .venv/bin/pip install -r requirements.txt
   docker build -t defects4j:3.0.1 ./defects4j && bash scripts/patchDefects4jImage.sh
   ```
3. **FixCheck.** `fixcheck/` is gitignored. `bash scripts/buildFixcheck.sh`
   clones it (upstream `9503ba2` when the patches were generated), applies
   0001–0012, runs its tests and builds `fixcheck/build/libs/fixcheck-all-1.0.0.jar`.
   Never edit `fixcheck/` without regenerating the patch it belongs to.
4. **DefectRepairing.** Gitignored as well; clone it:
   ```bash
   git clone https://github.com/Ultimanecat/DefectRepairing external/DefectRepairing   # used at aa519d5
   ```
   It has 178 patches with an author configuration: 34 Correct, 130 Incorrect
   and 14 Unknown.
5. **Results.** `results/` is not versioned. The `plausible` target needs
   `results/qwen3.6:35b/` and `results/gpt-oss:120b/` (≈2.6 GB together with
   `results/old/`); copy them over. `devfix` and `defectrepairing` need nothing
   from `results/`.
6. **Models.** `qwen3.6:35b` (fits an L40S) and `gpt-oss:120b` (needs an H100)
   must be pulled; see `scripts/ollama_serve.sh`. The GPU guards in CLAUDE.md
   apply unchanged.
7. **Smoke check** before submitting anything:
   ```bash
   .venv/bin/python -m pytest test/unit -q
   .venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s -k Lang-12
   ./scripts/runFixcheckReplay.sh --target devfix --oracle ollama/gpt-oss:120b --projects Cli --bug-id 35 --dry-run
   ```

## Phase 3: pilot (completed 2026-09-16; Phase 4 awaits the user's go-ahead)

### Subjects

**Report cases, each with the model that wrote its patch as oracle.** Only
plausible patches can be replayed:

| Model | Bugs |
|---|---|
| qwen3.6:35b (16) | Cli 35, Math 40, JxPath 8, Gson 11, Chart 8, Math 91, Lang 61, Lang 5, Chart 3, Math 10, Closure 111, JxPath 13, Math 67, Collections 20, Compress 3, Codec 10 |
| gpt-oss:120b (17) | Cli 35, JxPath 8, Chart 8, Math 91, Lang 61, Lang 5, Chart 3, Math 10, Closure 111, Mockito 31, JxPath 13, Math 67, Collections 20, JacksonDatabind 6, Compress 3, Closure 101, Codec 10 |

**Developer fixes** of the union of those 19 bugs, with both oracles.

**DefectRepairing:** 10 Correct and 10 Incorrect patches, sampled with
`random.Random(2026)` from the patches that have an author configuration. Each
is run with both configurations and both oracles.

| Project | Patches |
|---|---|
| Chart | Patch1, Patch4, Patch91 |
| Closure | Patch96, Patch99 |
| Lang | Patch190, Patch26 |
| Math | Patch194, Patch196, Patch197, Patch207, Patch209, Patch46, PatchHDRepair7, Patch48, Patch68, PatchHDRepair5, PatchHDRepair9 |
| Time | PatchHDRepair10, Patch183 |

### Commands

`scripts/pilotFixcheckV2.sh` submits exactly the subjects above, one job per
(target, oracle, config, project), since `--bug-id` applies to every listed
project; it resumes, so re-running it is safe. `--oracles qwen` or
`--targets devfix` narrow it; add `--dry-run` first. What it runs, by hand:

```bash
R=./scripts/runFixcheckReplay.sh
# report cases (plausible), e.g.
$R --target plausible --model ollama/qwen3.6:35b  --projects Math --bug-id 10,40,67,91
$R --target plausible --model ollama/gpt-oss:120b --projects Math --bug-id 10,67,91
# … Cli 35; JxPath 8,13; Gson 11; Chart 3,8; Lang 5,61; Closure 101,111; Mockito 31;
#   Collections 20; JacksonDatabind 6; Compress 3; Codec 10
# developer fixes, both oracles
for O in ollama/qwen3.6:35b ollama/gpt-oss:120b; do
  $R --target devfix --oracle $O --projects Math --bug-id 10,40,67,91
done
# DefectRepairing, both configs × both oracles
for O in ollama/qwen3.6:35b ollama/gpt-oss:120b; do for C in author ours; do
  $R --target defectrepairing --config $C --oracle $O --projects Math \
     --bug-id Patch194,Patch196,Patch197,Patch207,Patch209,Patch46,PatchHDRepair7,Patch48,Patch68,PatchHDRepair5,PatchHDRepair9
done; done
```

### Acceptance criteria

All are measured. If one fails, it is fixed before Phase 4.

1. **No shading.** The jar has no unrelocated commons classes, and the Cli 35
   traces show the checkout's line numbers (398/371/239). *Already seen in
   Phase 2; confirm under both oracles.*
2. **No package-access `IllegalAccessError`** in Closure 101/111 and Lang 61.
3. **No sibling tests.** Every scored failure belongs to the mutated method
   (JacksonDatabind 6, Lang 61).
4. **No run aborted by a non-compiling prefix**, and Mockito 31 compiles.
5. **No `FileNotFoundException`** on `src/test/resources` paths (Compress 3).
6. **Math 10 finishes** within budget, with `timed_out` prefixes.
7. **Reproducibility.** With the same seed, the mutations are identical in two
   runs. The share of identical assertions is reported.
8. **Background noise.** The failure rate of identity mutations is reported
   next to the report's 37.7%. The target is < 10%; above that, investigate
   before going on.
9. **DefectRepairing `author`.** At least 90% of subjects get a report.
   Compare with the author's predictions if he shares his outputs.
10. **Cost.** Extrapolate from the measured times; the earlier estimate was
    ~600–700 GPU h. **Launching Phase 4 with that figure is the user's
    decision.**

### Checking the criteria

```bash
.venv/bin/python scripts/analyze_fixcheck_pilot.py            # criteria 1-6 and 8-10 over results/
.venv/bin/python scripts/analyze_fixcheck_pilot.py --compare \
    <dir>/fixcheck_v2.json.previous <dir>/fixcheck_v2.json      # criterion 7
```

### Pilot log

**2026-09-15, submission** (commit `39ffa9f`): 65 jobs, 32 for qwen on L40S
and 33 for gpt-oss on H100 (`scripts/logs/pilot_submission_*.txt`).
- All of them sat in `Priority` with the L40S cards idle. With no `--mem`,
  this cluster's `DefMemPerNode=UNLIMITED` made each job request the node's
  whole 1.5 TB, which cannot fit next to another user's 576 GB.
- The pending jobs were lowered with `scontrol update MinMemoryNode=` (which
  takes MB): 96000 for qwen, 160000 for gpt-oss.
- `runFixcheckReplay.sh` now always passes `--mem`. The campaign scripts never
  did; they ran on an otherwise empty node.
- The first 5 jobs (qwen, plausible) started at 15:50 when the other user's H100
  reservation began. Each loaded 42/42 layers on its own L40S (PCI 43, 44, 83,
  84; checked against `nvidia-smi`).

**First observations** (to confirm in the analysis):
- *Codec 10, qwen's plausible patch (fixed):* `ok` in 24 s, 100/100 prefixes
  failing, max similarity 0.869, flagged, no oracle call at all.
  - `testEndMb` is a table of {input, expected encoding} String pairs. FixCheck
    mutates either role (`"MPM1111111"` → `"Kyl&en"`), so every prefix fails
    the original assertion.
  - The failure always goes through the same helper
    (`StringEncoderAbstractTest.checkEncoding`) with the same
    `ComparisonFailure`, so its trace resembles the original bug's.
  - This is the role-blind mutation limitation, not a harness artifact: a
    false positive the controls must quantify.
- *Collections 20, qwen's plausible patch (fixed):* `ok` in 24 s, 100/100
  prefixes crashed, including both identity mutations, all with
  `IllegalStateException` at `TreeListIterator.remove`. Not flagged (0.30).
  - **A new, undocumented artifact.** With every generator but
    `previous-assertion`, FixCheck deletes the assertions before running a
    prefix. The trigger test's assertions carry the iterator's moves inside
    them: `assertEquals("A", li.next())`, `assertEquals("B", li.next())`,
    `assertEquals("B", li.previous())`. Deleting them deletes the calls, so
    `li.remove()` fails on every prefix whatever the mutation.
  - The oracle is never called, and the run measures nothing.
  - `assertEquals(expected, obj.call())` is a very common shape, so this may
    be widespread. `analyze_fixcheck_pilot.py` now reports runs whose failures
    are mutation-independent, to measure it before proposing anything (e.g.
    keeping the call as a statement when its assertion is removed).

**2026-09-15, smoke job 16307** (developer fix of Cli 35, qwen3.6:35b oracle
on an L40S, 100 prefixes):
- `ok` in 35 min, 1 run analysed (String, 100 prefixes), flagged.
- 72 model calls, 28.4 s mean, 47 s max, no errors.

Two findings before submitting the rest:

- **The oracle reasons before answering.** qwen3.6:35b decodes ~4,200–5,200
  tokens per call to write one assertion. In the archived campaign's logs
  (494 calls):

  | Oracle | Median | p90 | Max |
  |---|---|---|---|
  | qwen3.6:35b | 17 s | 51 s | 135 s |
  | gpt-oss:120b (662 calls) | 4.4 s | 10.7 s | 82 s |

  The cost estimate assumed 6.7 s per call, which only holds for gpt-oss;
  qwen's oracle time is ~4× that. **Decision (user): keep the reasoning**, as
  when the model generated the patch.
- **A failed model call aborted the whole run.** `OllamaGenerator` throws on a
  timeout, and nothing caught it: with 120 s, qwen's tail would have lost whole
  runs. **Decision (user): patch 0012, and 300 s per call.** A prefix whose call
  fails is recorded as `assertion_generation_failed` (not scored, not failing)
  and the run goes on.

  0012 was verified end to end against a stand-in daemon that never answers
  (Lang 12, 2 s limit). `buildFixcheck.sh` now records the patches it applied,
  because 0010 edits a file 0003 adds, so 0003 no longer reverse-applies on a
  patched clone.

The smoke record was measured before 0012 with a 120 s limit. No call failed,
so 0012 would not have changed it. Once the rest of the pilot has finished, it
is re-run with `--no-resume` on the new jar as the **reproducibility subject**
(criterion 7): same seed, compared with its `.previous` record. It is not re-run
earlier because the pilot's own Cli job could race on the renamed record. The
first run's artifacts are kept in `fixcheck_v2.run1/` and
`fixcheck_v2.run1.json`.

**2026-09-16, the 65 jobs finished** (62 completed, 2 aborted by the GPU
guard, 2 at walltime). There are 147 records: 143 `ok` and 4 `not_reproduced`
(DefectRepairing Time Patch183, under both configs and both oracles: its
trigger tests still fail on Defects4J 3.0.1). `scripts/analyze_fixcheck_pilot.py`:

| # | Criterion | Result |
|---|---|---|
| 1 | No shading | ✅ 0 frames from relocated packages. Cli 35 traces show the checkout's `DefaultParser.java:398/371/239` under every oracle |
| 2 | No `IllegalAccessError` | ✅ 0 occurrences |
| 3 | No sibling tests | ✅ 0 failures outside the mutated method (JacksonDatabind 6, Lang 61 included) |
| 4 | No aborted run; Mockito 31 compiles | ✅ 196/198 runs analysed. The 2 misses are Math Patch196 under the author's config (`No locals of type double`: his CSV names `double` for `testReciprocalZero`, which has none outside assertions). Mockito 31 compiled on demand, 0 non-compiling |
| 5 | No `FileNotFoundException` | ✅ 0, Compress 3 included |
| 6 | Math 10 finishes | ✅ 25–32 min, 21/100 prefixes `timed_out`, no run timed out |
| 7 | Reproducibility | ✅ Cli 35's developer fix, re-run with `--no-resume` under each oracle (jobs 16378-16379): 100/100 identical mutations, outcomes and scores, and **72/72 identical oracle responses** for qwen and gpt-oss alike (`--compare`, and the logged responses). The qwen pair spans the jar before and after 0012. The mutations are also identical across oracles and across developer fix vs plausible patch (same max 0.919) |
| 8 | Background noise < 10% | ⚠️ Final: 38/362 identity prefixes fail (**10.5%**), vs 37.7% archived. **20 of the 38 come from mutation-independent runs** (Collections 20, Math 67, Chart 8...). Without those runs: 18/342 (**5.3%**). 18/137 analysed records (13.1%) have such a run, and 8 of them are flagged. The harness itself is below target; the excess is the assertion-removal artifact |
| 9 | DefectRepairing `author` ≥ 90% with a report | ✅ 18/20 under each oracle (Patch183 not reproduced, Patch196 above) |
| 10 | Cost | See below; your decision |

**GPU placement.** 61 jobs loaded the whole model on their own card: L40S
~153 tokens/s, H100 ~179. Four gpt-oss jobs did not:
- 16340, 16341, 16344 and 16345 loaded 0/37 layers at 1–9 tokens/s. SLURM gave
  them an H100 (PCI 03, 04) that something else filled: 2.7 of 93 GiB free.
- Closure 101 and Chart 3 (plausible, gpt-oss) were measured with the oracle
  on the CPU, and 14 of their 100 prefixes each lost the oracle call.
- Closure 111, Chart 8, Compress 3 and JacksonDatabind 6 were never measured.

The replay driver now pre-loads the model and checks placement before *and
after* every subject (`bdfc90a`). The 6 subjects were resubmitted with
`--no-resume` (jobs 16374-16377).
- All four loaded 37/37 layers at ~178 tokens/s and every subject finished
  `ok`: Closure 101 took 107 s instead of 71 min, Chart 3 254 s instead of
  106 min.
- The CPU-measured records are kept as `fixcheck_v2.json.previous`.
- **The pilot is complete: 151 records, 147 `ok`, 4 `not_reproduced`.**
  The figures below are final.

**Does FixCheck discriminate?** Flag rates by threshold, over analysed records
(`none` = flagged only by prefixes that never reached the oracle):

| Group | Oracle | n | ≥0.4 | ≥0.6 | ≥0.8 | ≥0.9 | none |
|---|---|---|---|---|---|---|---|
| developer fix | gpt-oss | 18 | 14 | 13 | 10 | 3 | 9 |
| developer fix | qwen | 18 | 16 | 15 | 11 | 3 | 10 |
| DR author, Correct | both | 9 | 5 | 1–2 | 0 | 0 | 3 |
| DR author, Incorrect | both | 9 | 5 | 3 | 2–3 | 0 | 1 |
| DR ours, Correct | both | 8 | 5 | 2 | 0 | 0 | 4 |
| DR ours, Incorrect | both | 9 | 6 | 5 | 3–4 | 1 | 1 |
| plausible, fixed | gpt-oss | 14 | 11 | 10 | 7 | 1 | 5 |
| plausible, fixed | qwen | 13 | 11 | 10 | 8 | 1 | 6 |
| plausible, regressing | gpt-oss | 2 | 2 | 2 | 1 | 1 | 2 |
| plausible, regressing | qwen | 2 | 2 | 2 | 1 | 1 | 2 |

At the agreed 0.4, FixCheck flags 78–89% of developer fixes. It flags
DefectRepairing's correct and incorrect patches at the same rate (5/9 vs 5/9
under the author's config). Only from 0.8 do the DR groups separate (0/9
correct vs 2–3/9 incorrect), but then half the developer fixes are still
flagged.

Most developer-fix flags come from prefixes that crashed or failed *before*
the oracle was called. Their similarity is structural: the same assertion
helper, exception type and frames. They come from three sources:
- role-blind mutation (Codec 10);
- inputs that violate a precondition and crash in the same frames as the
  original bug (Math 40: a mutated interval whose endpoints no longer bracket
  a root);
- mutation-independent failures:
  - Collections 20;
  - Math 67: removing `assertEquals(..., minimizer.optimize(...))` removed the
    `optimize` call, so the later `getOptima()` throws `no optimum computed yet`
    whatever the mutation.

Oracle choice does not matter here: all 35 DR subjects analysed under both
oracles got the same verdict from each.

The pilot's subjects were chosen from the report's hard cases, so these rates
are not estimates for the benchmark. They do say that a Phase 4 verdict at 0.4
would not separate correct from incorrect patches without the controls.

**Cost of Phase 4.** From the pilot's GPU-placed subjects: qwen 20.6 min and
gpt-oss 6.8 min per subject on average. That is an upper bound, since pilot
subjects all had mutable literals and many benchmark bugs have none.

| Target | Subjects | GPU-hours |
|---|---|---|
| plausible, qwen | 445 | 153 |
| plausible, gpt-oss | 473 | 54 |
| developer fix × 2 oracles | 854 | 390 |
| DR author × 2 oracles | 178 | 81 |
| DR ours × 2 oracles | 220 | 100 |
| **Total** | | **≈ 780** |

- qwen's ≈ 580 h on 5 L40S is ≈ 5 days of wall time.
- gpt-oss's ≈ 200 h on 4 H100 is ≈ 2 days, if the H100s are free (another
  user's jobs held them for most of the pilot).

## Phase 4: the full run (submitted 2026-09-21)

`scripts/runFixcheckPhase4.sh` submits all of it: **158 jobs, 3422 subjects**
(`scripts/logs/phase4_submission_*.txt`).

| Target | Subjects |
|---|---|
| plausible, qwen (its own patches) | 445 |
| plausible, gpt-oss | 473 |
| developer fix × 2 oracles | 854 × 2 |
| DefectRepairing `author` × 2 oracles | 178 × 2 |
| DefectRepairing `ours` × 2 oracles | 220 × 2 |

- Each project is split into jobs of ~25 subjects (qwen) or ~40 (gpt-oss), so
  no single job holds a card for days: Closure's 174 developer fixes are 7 jobs
  of ~13.5 h instead of one of 3.7 days.
- **The pilot's 151 records were archived to
  `results/old/fixcheck-v2-pilot/`** so that every measurement of record comes
  from this one batch, as the user asked. They were all measured with the
  current jar, so this costs the 33.5 GPU-hours of the pilot and nothing else.
  Their numbers stay in this document.
- `--resume` is left on: a job stopped by the GPU placement guard, by walltime
  or by a crash is fixed by running the script again, which re-submits only
  what has no record. `--retry-errored` also redoes the errored ones, including
  any marked `gpu_degraded`.

### The node's GPU types are mislabelled (2026-09-21)

Within hours, 20 of the gpt-oss jobs aborted at the placement guard with the
model ~38 GB off the GPU. **No measurement was contaminated: the guard stops
before the first subject.** The cause is a cluster misconfiguration, measured
with probe job 16599 and `nvidia-smi`'s minor numbers:

| Device file | Card it really is | `gres.conf` says |
|---|---|---|
| `/dev/nvidia0,1,2` | L40S (43, 44, 45) | H100, H100, L40S |
| `/dev/nvidia3,4` | **H100** (03, 04) | L40S |
| `/dev/nvidia5,6` | **H100** (C3, C4) | L40S |
| `/dev/nvidia7,8` | L40S (83, 84) | H100 |

- A `--gpus=H100:1` allocation is therefore not an H100.
- SLURM exports `CUDA_VISIBLE_DEVICES` as its own GRES index (L40S first, H100
  second), which is neither the minor nor the PCI order, so no
  `CUDA_DEVICE_ORDER` reconciles them: under `PCI_BUS_ID` an "H100" job opened
  the L40S at 83:00.0.
- `ConstrainDevices=yes` does nothing, because `TaskPlugin` is `(null)`.

This also re-reads two earlier incidents: the pilot's "H100 filled by another
process" was **our own qwen jobs**, sent there by the same mismatch, and
docs/audit-2026-09.md §1.11-1.12 attributed the symptom to CUDA's ordering
alone.

`scripts/ollama_serve.sh` now picks a card itself: enough free VRAM for the
model (70 GB for gpt-oss:120b, 26 GB for qwen3.6:35b), smallest card that fits
first so qwen leaves the H100s alone, pinned by **GPU UUID** — which no
ordering can reinterpret — under a `flock` per card so two of our jobs never
share one (`64216f3`). Verified: two gpt-oss jobs took distinct H100s (C3, C4)
with 37/37 layers on the GPU. Nothing prevents another user's unpinned job
from using the same card; **the real fix is for the admins to correct
gres.conf and set `TaskPlugin=task/cgroup`**.

The 20 failed jobs were resubmitted with their exact subject lists
(`scripts/logs/phase4_refill_*.txt`), so the batch still covers 3422 subjects.

### Later phases (not started)
- **Phase 5.**
  - A "FixCheck v2" section in `analysis/analysis.ipynb`:
    - flags at 0.4 per model, with a threshold sweep;
    - the false-positive rate on developer fixes;
    - sensitivity on the 94 regressing patches;
    - precision and recall on DefectRepairing, excluding Unknown, `author`
      vs `ours`;
    - identity noise, v1 → v2, and McNemar between the models.
  - Update `docs/fixcheck-verdict-limitations.md` and
    `docs/fixcheck-vacuous-verdicts.md`.
  - A Spanish report, `notes/informe-fixcheck-v2.md`.
  - Final upstream-ready descriptions of the patches.
