#!/bin/bash

# Submits scripts/slurm_job.sbatch (Ollama + scripts/runIterations.sh) as an
# sbatch job. Unlike srun, sbatch runs detached from the terminal: you can
# close it and the job keeps running, and the GPU is freed on its own once
# the job finishes (or fails).
#
# Usage: ./scripts/runWithSlurm.sh [--project P] [--bug-id N] [--model M] [--iterations N] [--gpu TYPE:COUNT]
# Any option left out falls back to runIterations.sh's own defaults, except
# --gpu which defaults to L40S:1.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PROJECT=""
BUG_ID=""
MODEL=""
ITERATIONS=""
GPU="L40S:1"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project) PROJECT="$2"; shift 2 ;;
        --bug-id) BUG_ID="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --iterations) ITERATIONS="$2"; shift 2 ;;
        --gpu) GPU="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

mkdir -p scripts/logs

# --gpus on the sbatch command line overrides the #SBATCH --gpus directive
# inside slurm_job.sbatch, which is why this is set here rather than there.
JOB_ID=$(sbatch --parsable \
    --gpus="$GPU" \
    --export=ALL,PROJECT="$PROJECT",BUG_ID="$BUG_ID",MODEL="$MODEL",ITERATIONS="$ITERATIONS" \
    scripts/slurm_job.sbatch)

echo "========================================"
echo "Submitted job with sbatch: $JOB_ID"
echo "Project=${PROJECT:-<default>} BugId=${BUG_ID:-<default>} Model=${MODEL:-<default>} Iterations=${ITERATIONS:-<default>} GPU=$GPU"
echo "You can close the terminal, the job will keep running."
echo "========================================"
echo "Output:     scripts/logs/slurm_${JOB_ID}.out"
echo "Errors:     scripts/logs/slurm_${JOB_ID}.err"
echo "Ollama log: scripts/logs/ollama_${JOB_ID}.log"
echo
echo "Useful commands:"
echo "  squeue -u \$USER                        # check job status"
echo "  tail -f scripts/logs/slurm_${JOB_ID}.out # follow the output live"
echo "  scancel $JOB_ID                          # cancel the job and free the GPU"
