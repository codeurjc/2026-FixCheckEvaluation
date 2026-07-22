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

# Per-job logs live in scripts/logs/<job_id>/. SLURM opens --output/--error
# when the job starts but does NOT create their parent directory, and the job
# id is only known after submission — so submit held, create the directory,
# then release. --gpus/--output/--error on the command line override the
# matching #SBATCH directives inside slurm_job.sbatch.
JOB_ID=$(sbatch --parsable --hold \
    --gpus="$GPU" \
    --output="scripts/logs/%j/slurm.out" \
    --error="scripts/logs/%j/slurm.err" \
    --export=ALL,PROJECT="$PROJECT",BUG_ID="$BUG_ID",MODEL="$MODEL",ITERATIONS="$ITERATIONS" \
    scripts/slurm_job.sbatch)

mkdir -p "scripts/logs/$JOB_ID"
scontrol release "$JOB_ID"

LOG_DIR="scripts/logs/$JOB_ID"
echo "========================================"
echo "Submitted job with sbatch: $JOB_ID"
echo "Project=${PROJECT:-<default>} BugId=${BUG_ID:-<default>} Model=${MODEL:-<default>} Iterations=${ITERATIONS:-<default>} GPU=$GPU"
echo "You can close the terminal, the job will keep running."
echo "========================================"
echo "Logs:       $LOG_DIR/"
echo "  Output:     $LOG_DIR/slurm.out"
echo "  Errors:     $LOG_DIR/slurm.err"
echo "  Ollama log: $LOG_DIR/ollama.log"
echo
echo "Useful commands:"
echo "  squeue -u \$USER                    # check job status"
echo "  tail -f $LOG_DIR/slurm.out # follow the output live"
echo "  scancel $JOB_ID                      # cancel the job and free the GPU"
