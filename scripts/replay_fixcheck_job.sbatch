#!/bin/bash
#SBATCH --job-name=fc-replay
#SBATCH --gpus=H100:1
#SBATCH --cpus-per-task=8
#SBATCH --signal=B:TERM@300
#SBATCH --output=scripts/logs/%j/slurm.out
#SBATCH --error=scripts/logs/%j/slurm.err

# One SLURM job = one project (or chunk) of a FixCheck replay: replay_fixcheck.py
# re-applies patches of record and runs FixCheck on them, with the oracle model
# served by an Ollama daemon of the job's own.
#
# Not meant to be submitted by hand: scripts/runFixcheckReplay.sh submits held,
# creates the log directory, then releases, and overrides --gpus/--time/--job-name.
#
# Everything it needs arrives through the environment: TARGET, PROJECT,
# SUBJECTS, ORACLE, MODEL (plausible only), CONFIG (defectrepairing only),
# TIMEOUT, EXTRA_ARGS.

set -e

PROJECT_DIR="${PROJECT_DIR:-$HOME/2026-FixCheckEvaluation}"
cd "$PROJECT_DIR"
source .venv/bin/activate

TARGET="${TARGET:?TARGET must be exported by runFixcheckReplay.sh}"
PROJECT="${PROJECT:?PROJECT must be exported by runFixcheckReplay.sh}"
ORACLE="${ORACLE:?ORACLE must be exported by runFixcheckReplay.sh}"
SUBJECTS="${SUBJECTS:-all}"
MODEL="${MODEL:-}"
CONFIG="${CONFIG:-ours}"
TIMEOUT="${TIMEOUT:-86400}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

LOG_DIR="$PROJECT_DIR/scripts/logs/${SLURM_JOB_ID:-manual}"
mkdir -p "$LOG_DIR/subjects"

# Per job, never shared: reap_containers() filters leftover containers by it.
WORKDIR="$PROJECT_DIR/workspace/replay_job_${SLURM_JOB_ID:-manual}"
mkdir -p "$WORKDIR"

source scripts/ollama_serve.sh

# As in project_job.sbatch: the run goes to the background so that SLURM's
# SIGTERM reaches it, and the daemon and the checkouts are only torn down once
# it has stopped using them.
RUN_PID=""
CLEANED=0
cleanup() {
    [ "$CLEANED" = "1" ] && return
    CLEANED=1
    if [ -n "$RUN_PID" ] && kill -0 "$RUN_PID" 2>/dev/null; then
        echo "[job] signal received; forwarding TERM to replay_fixcheck.py (pid $RUN_PID)"
        kill -TERM "$RUN_PID" 2>/dev/null || true
        wait "$RUN_PID" 2>/dev/null || true
        echo "[job] replay_fixcheck.py stopped; tearing down"
    fi
    stop_ollama
    rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

echo "========================================"
echo "FixCheck replay - job ${SLURM_JOB_ID:-manual} (node ${SLURM_JOB_NODELIST:-local})"
echo "Target=$TARGET Project=$PROJECT Subjects=$SUBJECTS Oracle=$ORACLE Model=${MODEL:--} Config=$CONFIG"
echo "Workdir=$WORKDIR"
echo "========================================"

start_ollama "$ORACLE" "$LOG_DIR/ollama.log"

# Derived from the oracle and this job's own daemon, as project_job.sbatch
# does, so the generator can never name another model or another job's port.
FIXCHECK_ASSERTIONS="ollama:${ORACLE#ollama/}@${OLLAMA_PORT}"
echo "[job] FixCheck assertions generator: $FIXCHECK_ASSERTIONS"

ARGS=(--target "$TARGET" --project "$PROJECT" --config "$CONFIG"
      --workdir "$WORKDIR" --log-dir "$LOG_DIR" --timeout "$TIMEOUT"
      --fixcheck-assertions "$FIXCHECK_ASSERTIONS")
if [ "$SUBJECTS" != "all" ]; then
    ARGS+=(--bug-id "$SUBJECTS")
fi
if [ -n "$MODEL" ]; then
    ARGS+=(--model "$MODEL")
fi

python replay_fixcheck.py "${ARGS[@]}" $EXTRA_ARGS &
RUN_PID=$!
wait "$RUN_PID"
RUN_STATUS=$?
cleanup
exit "$RUN_STATUS"
