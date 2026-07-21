#!/bin/bash
#SBATCH --job-name=fixcheck-iterations
#SBATCH --gpus=L40S:1
#SBATCH --output=scripts/logs/slurm_%j.out
#SBATCH --error=scripts/logs/slurm_%j.err

# sbatch job: starts Ollama on the assigned GPU, runs
# scripts/runIterations.sh and releases the GPU when done (or on failure).
# Not meant to be submitted by hand: use scripts/runWithSlurm.sh instead,
# which forwards PROJECT/BUG_ID/MODEL/ITERATIONS through here and can
# override the --gpus directive above via its own --gpu flag.

set -e

PROJECT_DIR="$HOME/2026-FixCheckEvaluation"
OLLAMA_PORT="1995"
OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT"

cd "$PROJECT_DIR"
source .venv/bin/activate

export OLLAMA_HOST="$OLLAMA_HOST"

# Same defaults as runIterations.sh, resolved here too so the model is known
# up front for the "ollama pull" step below.
PROJECT="${PROJECT:-Chart}"
BUG_ID="${BUG_ID:-1}"
MODEL="${MODEL:-ollama/qwen3.6:35b}"
ITERATIONS="${ITERATIONS:-10}"
export PROJECT BUG_ID MODEL ITERATIONS

OLLAMA_PID=""
cleanup() {
    if [ -n "$OLLAMA_PID" ] && kill -0 "$OLLAMA_PID" 2>/dev/null; then
        echo "Stopping Ollama (PID $OLLAMA_PID)..."
        kill "$OLLAMA_PID" 2>/dev/null || true
        wait "$OLLAMA_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "========================================"
echo "FixCheckEvaluation - Job $SLURM_JOB_ID (node $SLURM_JOB_NODELIST)"
echo "Project=$PROJECT BugId=$BUG_ID Model=$MODEL Iterations=$ITERATIONS"
echo "========================================"

echo "[1/4] Cleaning up previous Ollama processes..."
pkill -f "ollama serve" || true
sleep 2

echo "[2/4] Starting Ollama..."
ollama serve > "$PROJECT_DIR/scripts/logs/ollama_${SLURM_JOB_ID}.log" 2>&1 &
OLLAMA_PID=$!
echo "Ollama PID: $OLLAMA_PID"

max_retries=30
retry_count=0
echo "Waiting for Ollama to be ready..."
while [ $retry_count -lt $max_retries ]; do
    if curl -s "http://$OLLAMA_HOST/api/tags" > /dev/null 2>&1; then
        echo "✓ Ollama is up"
        break
    fi
    echo "Attempt $((retry_count + 1))/$max_retries..."
    sleep 2
    retry_count=$((retry_count + 1))
done

if [ $retry_count -eq $max_retries ]; then
    echo "✗ Error: Ollama did not respond"
    exit 1
fi

# Strip the "ollama/" provider prefix to get the model name for the ollama CLI
MODEL_NAME="${MODEL#ollama/}"
echo "[3/4] Checking/pulling model: $MODEL_NAME"
if ! ollama list | grep -q "$MODEL_NAME"; then
    echo "Pulling model $MODEL_NAME..."
    ollama pull "$MODEL_NAME"
else
    echo "✓ Model $MODEL_NAME already present"
fi

echo "[4/4] Running scripts/runIterations.sh..."
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
