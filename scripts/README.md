# scripts/

Helper scripts for running experiments and tests. All of them are meant to
be run from the repository root.

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
  (`--gpus=L40S:1` by default), starts `ollama serve`, waits until it
  responds, runs `ollama pull` for the configured model if it isn't
  downloaded yet, and executes `runIterations.sh`. On exit (success or
  failure) it kills the Ollama process, and SLURM releases the GPU once the
  job finishes. It isn't meant to be submitted by hand — use
  `runWithSlurm.sh` instead.
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
