#!/bin/bash
#SBATCH --job-name=fc-project
#SBATCH --gpus=H100:1
#SBATCH --cpus-per-task=8
#SBATCH --signal=B:TERM@300
#SBATCH --output=scripts/logs/%j/slurm.out
#SBATCH --error=scripts/logs/%j/slurm.err

# One SLURM job = one Defects4J project (or one chunk of a big one). Starts an
# Ollama daemon on a port of its own, then runs run_project.py over that
# project's bugs, one run per bug.
#
# Not meant to be submitted by hand: SLURM does not create the parent directory
# of --output/--error and the job id is only known after submission, so
# scripts/runCampaign.sh submits held, creates the directory, then releases.
# runCampaign.sh also overrides --gpus/--time/--job-name on the command line.
#
# Everything it needs arrives through --export: PROJECT, BUG_IDS, MODEL,
# TIMEOUT, FIXCHECK_PREFIXES, EXTRA_ARGS.

set -e

PROJECT_DIR="${PROJECT_DIR:-$HOME/2026-FixCheckEvaluation}"
cd "$PROJECT_DIR"
source .venv/bin/activate

PROJECT="${PROJECT:?PROJECT must be exported by runCampaign.sh}"
BUG_IDS="${BUG_IDS:-all}"
MODEL="${MODEL:-ollama/gpt-oss:120b}"
TIMEOUT="${TIMEOUT:-7200}"
FIXCHECK_PREFIXES="${FIXCHECK_PREFIXES:-10}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

LOG_DIR="$PROJECT_DIR/scripts/logs/${SLURM_JOB_ID:-manual}"
mkdir -p "$LOG_DIR/bugs"

# Mount root is per JOB, not per project: --chunks can split one project across
# two jobs, and reap_containers() filters leftover containers by this path, so
# it must never be shared with a sibling.
WORKDIR="$PROJECT_DIR/workspace/job_${SLURM_JOB_ID:-manual}"
mkdir -p "$WORKDIR"

source scripts/ollama_serve.sh

cleanup() {
    stop_ollama
    rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

echo "========================================"
echo "FixCheckEvaluation campaign - job ${SLURM_JOB_ID:-manual} (node ${SLURM_JOB_NODELIST:-local})"
echo "Project=$PROJECT Bugs=$BUG_IDS Model=$MODEL Timeout=${TIMEOUT}s Prefixes=$FIXCHECK_PREFIXES"
echo "Workdir=$WORKDIR"
echo "========================================"

start_ollama "$MODEL" "$LOG_DIR/ollama.log"

# Same model for the fix and for FixCheck's assertions, but the two take
# different notations -- '<provider>/<model>' vs 'ollama:<model>@[host:]port'.
# Deriving the second from the first (as scripts/runExperiment.sh does) is what
# keeps them from ever naming different models or, worse, different daemons.
FIXCHECK_ASSERTIONS="ollama:${MODEL#ollama/}@${OLLAMA_PORT}"
echo "[job] FixCheck assertions generator: $FIXCHECK_ASSERTIONS"

python run_project.py \
    --project "$PROJECT" \
    --bug-id "$BUG_IDS" \
    --workdir "$WORKDIR" \
    --log-dir "$LOG_DIR" \
    --timeout "$TIMEOUT" \
    --model "$MODEL" \
    --temperature 0.0 \
    --include-test-code --include-test-log --include-issue \
    --fixcheck \
    --fixcheck-prefixes "$FIXCHECK_PREFIXES" \
    --fixcheck-assertions "$FIXCHECK_ASSERTIONS" \
    $EXTRA_ARGS
