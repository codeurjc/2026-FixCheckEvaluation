# Investigation: `test_benchmark_lang1.py` failing while manual runs succeeded

**Date:** 2026-07-06
**Symptom:** `test/e2e/test_benchmark_lang1.py::test_trigger_tests_pass` failed
(the LLM's fix for Defects4J Lang 1 did not make the trigger test pass), while
running the same bug manually via `scripts/runExperiment.sh` — and 10x via
`scripts/runIterations.sh` — always produced a fix that passed (`results/Lang/1/summary.json`:
`"fixed": 10` out of 10).

## Root cause #1 (real config mismatch, fixed)

The `lang1_pipeline` fixture in `test/e2e/conftest.py` did **not** send the same
prompt context as `Experiment.py`:

| | `Experiment.py` (manual, via `runExperiment.sh`) | `lang1_pipeline` fixture (before fix) |
|---|---|---|
| `--include-test-code` / `test_sources` | included | included |
| `--include-issue` / `issue_text` | included | included |
| `--include-test-log` / `test_log` | **included** | **missing** — never called `run_trigger_tests` |
| `max_tokens` | default `8192` (no CLI flag exists) | explicitly `-1` (unlimited) |

Missing the regression test log meant the model never saw the actual
`NumberFormatException` trace, and unlimited `max_tokens` let the model ramble
differently than the bounded manual runs.

**Fix applied:** `test/e2e/conftest.py` now calls `run_trigger_tests(...)` and
passes `test_log=test_log` to `FixGenerator.generate(...)`, and no longer
overrides `max_tokens` (uses the same `8192` default as `Experiment.py`).

## Root cause #2 (genuine LLM non-determinism, mitigated not fixed)

After aligning the config, the benchmark **still failed** across 4 consecutive
automated attempts (1 in the first aligned run, then 3/3 in a follow-up run
with retries). Investigation steps:

1. Compared `result.json` across all 10 manual iterations
   (`results/Lang/1/1..10/result.json`): `input_tokens` was **identical
   (14899)** in all 10, and all 10 were `"triggers_fixed": true`.
2. Instrumented `FixGenerator.generate()` to write the exact rendered prompt
   to `results_dir/prompt.txt` (see `FixGenerator.py`, `README.md` Output
   section), and made the fixture write the same via
   `results_dir=results/_debug/lang1_pipeline/attempt_N/`.
3. Ran one fresh manual experiment and one fresh automated benchmark run with
   the instrumentation, then `diff`'d the two `prompt.txt` files
   (~58.6 KB each): **the only difference was the absolute workdir path**
   embedded in the regression-test-log command echo (`.../workspace/Lang_1`
   vs. a pytest tmp dir) — i.e. the prompts are effectively identical.
4. That run's automated benchmark **passed** on attempt 1/3, confirming the
   pipeline itself was not the problem.
5. Reproduced the model's failure mode in isolation (plain Java, no
   Docker/Defects4j needed): the LLM's "count hex digits, manually check the
   most-significant digit" strategy for `NumberUtils.createNumber` correctly
   handles the `Integer`/`Long` boundary (8 hex digits) but — in some
   generations — forgets the symmetric `Long`/`BigInteger` boundary (16 hex
   digits, e.g. `0xFFFFFFFFFFFFFFFF`), calling `Long.decode()` on a value that
   overflows `Long`, which throws instead of promoting to `BigInteger`.
   The manual runs' successful fix instead used a `try createInteger → catch →
   try createLong → catch → createBigInteger` cascade, which sidesteps the
   arithmetic entirely by letting the JDK's own parsers detect overflow.

**Conclusion:** this is not a bug in `Experiment.py`, `FixGenerator.py`, or the
test fixture. It's real output variance from `ollama/gpt-oss:120b` at
`temperature=0.0` on this specific edge case — the model sometimes reaches for
the robust try/catch cascade and sometimes reaches for the fragile
digit-counting approach (which itself is sometimes complete and sometimes
missing the 16-digit special case). 10/10 manual success suggests the failure
mode is uncommon but not impossible; a handful of consecutive automated
failures was an unlucky streak, not a systemic issue.

## Update 2026-07-07: the variance is per GPU-session, not per call

New data point: `scripts/runIterations.sh` (5 iterations) was run on 2026-07-06
(`results/Lang_6Jul/`) and again on 2026-07-07 (`results/Lang_7Jul/`), same
model, same flags, same machine. Result: **5/5 fixed on the 6th, 0/5 fixed on
the 7th.**

This is a stronger signal than plain sampling noise — within each day's 5
runs, the model's answer was essentially frozen:

| | 6 Jul (`Lang_6Jul`) | 7 Jul (`Lang_7Jul`) |
|---|---|---|
| `input_tokens` (all 5 runs) | 14899 | 14899 (**same prompt**) |
| `output_tokens` | 2145 (4/5), 1810 (1/5) | 1473 (**5/5 identical**) |
| 16-hex-digit boundary check present in the fix (`grep` on `raw_response.txt`) | 5/5 present | 5/5 **absent** |

So the prompt didn't change (`input_tokens` identical), but the model's
behavior flipped consistently for an entire day's worth of runs. Checked the
Ollama serving infrastructure (`ps aux`, `sacct -u maes`) and found:

- `ollama serve` runs as a **Slurm job** on a shared, multi-tenant GPU cluster
  (node `damo`, other users' jobs — `dslab`, `mgarcia` — run concurrently on
  the same node).
- Each day's session was a **different Slurm job** (`14892` on 07-06,
  `14905` on 07-07), each requesting `gres/gpu:h100=1` — Slurm hands out
  *any* free H100 on the node, not a pinned physical card across job
  submissions. `nvidia-smi` at the time of the 07-07 run showed two other
  H100s on the node already at 63-85 GB used by other jobs.

GPU inference for a 116.8B-parameter MXFP4-quantized model is **not
bit-reproducible across different physical GPUs or process restarts**, even
at `temperature=0.0`: cuBLAS/attention kernel selection and floating-point
reduction order depend on the specific device and its current memory/kernel
cache state, and are not associative. For most prompts this is invisible, but
this particular bug sits right on a knife's edge (whether the model adds one
extra boundary-check `if`), so the low-level numerical noise between GPU
sessions is enough to flip the outcome consistently for an entire session.

**Practical implication:** "determinism" from `temperature=0.0` only holds
*within* a single long-lived Ollama server/GPU session — restarting the
Ollama Slurm job (which happens implicitly every time you start a fresh
`ollama serve`) can shift the model's answer distribution for edge-case bugs
like this one, independent of any code change in this repo.

## Mitigation applied

`lang1_pipeline` (in `test/e2e/conftest.py`) now retries generation up to
`FIXGEN_BENCHMARK_ATTEMPTS` (env var, default `3`) times **only** when
`--run-benchmark` is passed (mechanics-only runs still use a single attempt to
stay fast), and considers the run a success as soon as one attempt fixes the
trigger tests. This mirrors how the pipeline would actually be used in
practice (retry until it works) rather than treating a single non-deterministic
sample as the verdict on model quality.

## Artifacts for future reference

- `results/Lang/1/prompt.txt` — a full manual-run prompt (58635 bytes).
- `results/_debug/lang1_pipeline/attempt_1/prompt.txt` — the equivalent
  automated-run prompt (58631 bytes), for diffing against future manual runs
  if this needs to be re-verified. (`results/` and `results/_debug/` are
  gitignored — regenerate by re-running `scripts/runExperiment.sh` and
  `pytest test/e2e --run-benchmark -v -s`.)

## If this resurfaces

- Re-diff a fresh manual `prompt.txt` against a fresh
  `results/_debug/lang1_pipeline/attempt_N/prompt.txt` first, to rule out a
  new config drift before assuming it's model variance again.
- If `input_tokens` matches across "before/after" but `output_tokens` /
  the actual fix strategy is frozen-but-different per session, check
  `sacct -u <user> --format=JobID,JobName,Start,End,State,NodeList` for the
  Ollama Slurm job: a *new* job id spanning the change window means a fresh
  GPU allocation, which is consistent with the infra-driven variance
  documented above rather than a code regression.
- If failures cluster within one GPU/server session (many in a row, same
  session), consider adding a fixed `seed` to `llms/ollama_llm.py`'s
  `options` for more reproducible (though not guaranteed-deterministic)
  sampling.
- Increasing `FIXGEN_BENCHMARK_ATTEMPTS` trades CI time for a lower chance of
  a flaky red benchmark on this specific bug.
