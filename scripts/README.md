# scripts/

Helper scripts for running experiments and tests. All of them are meant to
be run from the repository root.

## buildFixcheck.sh

One-time setup for `--fixcheck` (see the root `README.md`'s *Requirements*
section): builds `fixcheck/build/libs/fixcheck-all-1.0.0.jar` from the
vendored `fixcheck/` sources inside a throwaway `defects4j:3.0.1` container,
then smoke-tests the jar under that image's Java 11. Needs Docker and network
access (the Gradle wrapper downloads Gradle 8.0.2 on first use).

```bash
bash scripts/buildFixcheck.sh
```

Re-run it whenever `fixcheck/` changes; the jar isn't rebuilt automatically.

## runExperiment.sh

Runs a single execution of `Experiment.py` for one Defects4J bug. Assumes an
Ollama instance is already reachable at `http://localhost:1995` (see
[runWithSlurm.sh and slurm_job.sbatch](#runwithslurmsh-and-slurm_jobsbatch)).

```bash
bash scripts/runExperiment.sh
```

Edit the `PROJECT`, `BUG_ID` and `MODEL` variables at the top of the script
to change the bug or the model to use.

## runIterations.sh

Same as `runExperiment.sh`, but repeats the experiment `ITERATIONS` times
(via `run_iterations.py`) to mitigate LLM non-determinism. Also assumes
Ollama is already running on `localhost:1995`.

```bash
bash scripts/runIterations.sh
```

`PROJECT`, `BUG_ID`, `MODEL` and `ITERATIONS` default to the values hardcoded
at the top of the script, but each can be overridden by exporting an
environment variable of the same name before calling it (this is how
`runWithSlurm.sh` forwards its own arguments through to this script):

```bash
PROJECT=Lang BUG_ID=1 ITERATIONS=5 bash scripts/runIterations.sh
```

## runTest.sh

Shortcut for running the pytest suite.

```bash
bash scripts/runTest.sh [path_or_marker]
# e.g. bash scripts/runTest.sh test/unit
```

## runWithSlurm.sh and slurm_job.sbatch

On the cluster, Ollama needs a GPU and is launched through SLURM. These two
files automate the whole lifecycle (start Ollama, wait until it's ready,
pull the model if missing, run `runIterations.sh`, and release the GPU when
done) so Ollama doesn't have to be managed by hand.

- **`slurm_job.sbatch`** is the actual job: it requests a GPU
  (`--gpus=L40S:1` by default), starts `ollama serve` through
  [`ollama_serve.sh`](#ollama_servesh), waits until it responds, and executes
  `runIterations.sh`. On exit (success or failure) it kills *its own* Ollama
  process, and SLURM releases the GPU once the job finishes. It isn't meant to
  be submitted by hand — use `runWithSlurm.sh` instead.
- **`runWithSlurm.sh`** is a thin wrapper that submits `slurm_job.sbatch`
  with `sbatch` and returns immediately. Unlike `srun`, an `sbatch` job runs
  detached from the terminal: you can close it without cancelling the run.

```bash
./scripts/runWithSlurm.sh [--project P] [--bug-id N] [--model M] [--iterations N] [--gpu TYPE:COUNT]
```

`--project`, `--bug-id`, `--model` and `--iterations` are optional and fall
back to `runIterations.sh`'s own defaults when omitted. `--gpu` defaults to
`L40S:1` and is passed straight to `sbatch --gpus`, overriding the
`#SBATCH --gpus` directive inside `slurm_job.sbatch` (available GPU types on
this cluster: `L40S`, `H100` — see `notes/cluster.txt`).

The wrapper forwards `PROJECT`/`BUG_ID`/`MODEL`/`ITERATIONS` through
`sbatch --export`, which `slurm_job.sbatch` resolves (using the same
defaults as `runIterations.sh`, to know the model to pull upfront) and
exports again for `runIterations.sh` to pick up. For example:

```bash
./scripts/runWithSlurm.sh --project Lang --bug-id 1 --model ollama/qwen3.6:35b --iterations 1 --gpu L40S:1
```

On submission it prints the job id and the relevant log paths:

```bash
squeue -u $USER                              # check job status
tail -f scripts/logs/<job_id>/slurm.out       # follow the output live
scancel <job_id>                             # cancel the job and free the GPU
```

Each run's logs are grouped in a per-job directory
`scripts/logs/<job_id>/`, containing `slurm.out` (job stdout), `slurm.err`
(stderr) and `ollama.log` (the `ollama serve` log). Because SLURM opens its
output files when the job starts but won't create their parent directory —
and the job id is only known after submission — the wrapper submits the job
held (`--hold`), creates `scripts/logs/<job_id>/`, and then releases it
(`scontrol release`), so the directory always exists before the job writes to
it.

## ollama_serve.sh

Shared Ollama lifecycle for every SLURM job, meant to be **sourced**:

```bash
source scripts/ollama_serve.sh
start_ollama "ollama/gpt-oss:120b" "$LOG_DIR/ollama.log"
trap stop_ollama EXIT INT TERM
```

It exists because this cluster is a **single node**: every job of a campaign
lands on the same machine, so the two habits the old inline version had were
fatal once more than one job ran at a time.

- **No `pkill -f "ollama serve"`.** That pattern kill took out the sibling
  jobs' daemons; the victims then produce empty results with no obvious cause.
  `stop_ollama` kills exactly one recorded PID.
- **No fixed port.** `start_ollama` derives a candidate from the job id
  (`21000 + job_id % 900`), probes it with a real `bind()`, and — the part that
  makes it race-free rather than merely unlikely to collide — **retries on the
  next port if the daemon dies during startup**, which is what losing a bind
  race looks like. It binds loopback only, so no sibling can reach it, and it
  exports `OLLAMA_PORT`, `OLLAMA_HOST` and `OLLAMA_BASE_URL`.
- **It never `ollama pull`s.** The model store is shared by every concurrent
  job, and two simultaneous pulls of the same multi-GB blob is the one way to
  corrupt it. A missing model is a hard error telling you to pull it by hand.
- It sets `OLLAMA_CONTEXT_LENGTH=49152` to match what `llms/ollama_llm.py`
  requests. FixCheck's `OllamaGenerator` sends no options, so without this the
  server would spin up a *second* runner at its default context — and two
  runners of a 64 GB model do not fit on a 96 GB H100, so the model would be
  unloaded and reloaded on every switch between fix and assertion generation.

## runCampaign.sh and project_job.sbatch

Run `Experiment.py` across whole projects — the full-benchmark campaign. One
sbatch job per project, all of that project's bugs sequentially on its GPU;
parallelism comes from several project jobs running at once. See
[docs/campaign.md](../docs/campaign.md) for the protocol.

```bash
./scripts/runCampaign.sh --projects JacksonXml,Csv,Codec       # pilot: 40 bugs
./scripts/runCampaign.sh --projects all --minutes-per-bug 25   # all 854
./scripts/runCampaign.sh --projects Closure --chunks 2         # split a big one
./scripts/runCampaign.sh --projects all --dry-run              # show, don't submit
```

| Option | Meaning | Default |
|---|---|---|
| `--projects` | `all`, or a comma-separated list | `all` |
| `--bug-id` | `all`, or ids/ranges (`1,3-5`); only with a single project | `all` |
| `--model` | used for the fix **and** for FixCheck's assertions | `ollama/gpt-oss:120b` |
| `--gpu` | passed to `sbatch --gpus` | `H100:1` |
| `--fixcheck-prefixes` | variations FixCheck generates per bug | `10` |
| `--timeout` | per-bug wall-clock limit, in seconds | `7200` |
| `--minutes-per-bug` | sizes each job's `--time` | `20` |
| `--chunks` | split each project's bugs across N jobs | `1` |
| `--retry-errored` / `--no-resume` | forwarded to `run_project.py` | off |
| `--dry-run` | print the `sbatch` lines and exit | off |

`--gpu` defaults to `H100:1` deliberately: `gpt-oss:120b` needs ~64 GB and does
not fit on this cluster's 46 GB L40S cards. Each job's `--time` is
`bugs × minutes + 1 h`, capped at the partition's 6-day limit, so short projects
stay backfill-friendly instead of every job asking for the maximum.

**`project_job.sbatch`** is the job itself: it starts Ollama on its own port,
derives `--fixcheck-assertions` from `$MODEL` and that port (so the two
notations for the same model can never disagree), and runs `run_project.py`
against a **per-job** mount root `workspace/job_<job_id>/`. Per-job, not
per-project, because `--chunks` can split one project over two jobs and the
container reaper filters by that path. Submitted by `runCampaign.sh`, which
reuses the held→mkdir→release trick described above.

Logs land in `scripts/logs/<job_id>/`: `slurm.out`, `ollama.log`,
`manifest.json` (git sha, argv, model, port, resolved bug ids),
`status.jsonl` (one line per finished bug — `tail -f` this) and
`bugs/<Project>_<id>.log` (one full `Experiment.py` log per bug, because a
single Closure test run would otherwise drown the shared log).

## patchDefects4jImage.sh

Makes Defects4J's per-project directory-layout caches writable inside the
`defects4j:3.0.1` image, and retags the result **in place** (so nothing that
names the image has to change).

```bash
bash scripts/patchDefects4jImage.sh
```

Defects4J caches each revision's source/test directory layout in
`framework/projects/<P>/dir-layout.csv`, and on a cache *miss* appends the
layout it just worked out. That file is `root:root 644` in the image while our
containers run as the host uid (so files on the shared volume stay host-owned),
so the append fails with `Permission denied`. Exactly one bug in the benchmark
reaches that path — **Chart 26**, whose buggy revision `102` is missing from
Chart's layout map — and it dies ~5 s in with no result, identically for every
model. The script `chmod a+w`s those CSVs so Defects4J can compute and cache the
layout itself, rather than shipping a hardcoded guess at the missing entry.

Idempotent: it marks the image with the label
`org.fixcheckeval.layout-writable=1` and exits early if it is already there.
`run_project.py` checks that label and warns when it is missing.

**Re-run it after any `docker build -t defects4j:3.0.1 ./defects4j`** — a base
rebuild discards the patch. Same trap as editing `fixcheck/` without
regenerating `scripts/fixcheck-patches/`.
