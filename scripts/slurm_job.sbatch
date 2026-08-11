#!/bin/bash
#SBATCH --job-name=fixcheck-iterations
#SBATCH --gpus=L40S:1
#SBATCH --output=scripts/logs/%j/slurm.out
#SBATCH --error=scripts/logs/%j/slurm.err

# sbatch job: starts Ollama on the assigned GPU, runs
# scripts/runIterations.sh and releases the GPU when done (or on failure).
# Per-job logs go to scripts/logs/<job_id>/. SLURM does not create the parent
# directory of --output/--error, so it must exist before the job starts; the
# wrapper handles that (submit held -> mkdir -> release), which is one reason
# this is not meant to be submitted by hand. Use scripts/runWithSlurm.sh
# instead: it forwards PROJECT/BUG_ID/MODEL/ITERATIONS through here and can
# override the --gpus directive above via its own --gpu flag.

set -e

PROJECT_DIR="$HOME/2026-FixCheckEvaluation"

cd "$PROJECT_DIR"
source .venv/bin/activate

# Same defaults as runIterations.sh, resolved here too so the model is known
# up front for the Ollama startup below.
PROJECT="${PROJECT:-Chart}"
BUG_ID="${BUG_ID:-1}"
MODEL="${MODEL:-ollama/qwen3.6:35b}"
ITERATIONS="${ITERATIONS:-10}"
export PROJECT BUG_ID MODEL ITERATIONS

# The Ollama lifecycle is shared with scripts/project_job.sbatch. It matters
# here too: this job used to `pkill -f "ollama serve"` and bind a fixed port
# 1995, which on this single-node cluster would kill every concurrently running
# campaign job's daemon. start_ollama picks a free port and stop_ollama kills
# only its own PID.
source scripts/ollama_serve.sh
trap stop_ollama EXIT INT TERM

echo "========================================"
echo "FixCheckEvaluation - Job $SLURM_JOB_ID (node $SLURM_JOB_NODELIST)"
echo "Project=$PROJECT BugId=$BUG_ID Model=$MODEL Iterations=$ITERATIONS"
echo "========================================"

# Per-job log directory (created by the wrapper before release; mkdir -p here
# is idempotent and keeps the ollama log alongside slurm.out/err).
LOG_DIR="$PROJECT_DIR/scripts/logs/$SLURM_JOB_ID"
mkdir -p "$LOG_DIR"

echo "[1/2] Starting Ollama on a free port..."
start_ollama "$MODEL" "$LOG_DIR/ollama.log"

# runIterations.sh reads this to point Experiment.py at the right daemon; the
# port is no longer the historical fixed 1995.
export OLLAMA_BASE_URL

echo "[2/2] Running scripts/runIterations.sh..."
echo "====================================="

if bash scripts/runIterations.sh; then
    echo "====================================="
    echo "✓ Iterations completed"
    echo "====================================="
    EXIT_CODE=0
else
    echo "✗ Iterations failed"
    EXIT_CODE=1
fi

exit $EXIT_CODE
