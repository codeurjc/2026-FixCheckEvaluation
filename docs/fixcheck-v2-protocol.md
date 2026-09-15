# FixCheck v2: protocol, status and handoff

Status on 2026-09-15: **Phases 1 and 2 are done and committed. Phase 3 (the
pilot) is in progress** (see "Pilot log"). This document is what is needed to
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

## Phase 3: pilot (in progress since 2026-09-15; see "Pilot log")

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

## Later phases (not started)

- **Phase 4.** The full run:
  - the plausible patches with their own oracle;
  - `devfix` for 854 bugs × 2 oracles;
  - DefectRepairing × {author, ours} × 2 oracles;
  - `--resume` throughout.
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
