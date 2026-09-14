#!/bin/bash

# Submits one scripts/replay_fixcheck_job.sbatch per project (or chunk) to
# re-measure FixCheck with replay_fixcheck.py: the patches of record are
# re-applied, never regenerated.
#
# Usage:
#   ./scripts/runFixcheckReplay.sh --target plausible --model ollama/qwen3.6:35b --projects all
#   ./scripts/runFixcheckReplay.sh --target devfix --oracle ollama/gpt-oss:120b --projects Lang,Math
#   ./scripts/runFixcheckReplay.sh --target defectrepairing --config author \
#       --oracle ollama/qwen3.6:35b --projects Chart,Closure,Lang,Math,Mockito,Time
#   ./scripts/runFixcheckReplay.sh ... --projects Cli --bug-id 35 --dry-run   # show, don't submit
#
# For --target plausible the oracle is the model that wrote the patches. The
# models must already be pulled (see scripts/ollama_serve.sh).

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

TARGET=""
MODEL=""
ORACLE=""
CONFIG="ours"
PROJECTS="all"
BUG_IDS="all"
GPU=""
CPUS="8"
TIMEOUT="86400"
MINUTES_PER_SUBJECT="30"
CHUNKS="1"
EXTRA_ARGS=""
DRY_RUN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --oracle) ORACLE="$2"; shift 2 ;;
        --config) CONFIG="$2"; shift 2 ;;
        --projects) PROJECTS="$2"; shift 2 ;;
        --bug-id) BUG_IDS="$2"; shift 2 ;;
        --gpu) GPU="$2"; shift 2 ;;
        --cpus) CPUS="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --minutes-per-subject) MINUTES_PER_SUBJECT="$2"; shift 2 ;;
        --chunks) CHUNKS="$2"; shift 2 ;;
        --retry-errored) EXTRA_ARGS="$EXTRA_ARGS --retry-errored"; shift ;;
        --no-resume) EXTRA_ARGS="$EXTRA_ARGS --no-resume"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

case "$TARGET" in
    plausible)
        if [ -z "$MODEL" ]; then
            echo "--target plausible needs --model" >&2; exit 1
        fi
        if [ -n "$ORACLE" ] && [ "$ORACLE" != "$MODEL" ]; then
            echo "--target plausible uses --model as its oracle; drop --oracle" >&2; exit 1
        fi
        # The oracle is the model that wrote the patches.
        ORACLE="$MODEL"
        ;;
    devfix|defectrepairing)
        if [ -z "$ORACLE" ]; then
            echo "--target $TARGET needs --oracle" >&2; exit 1
        fi
        if [ -n "$MODEL" ]; then
            echo "--model only applies to --target plausible" >&2; exit 1
        fi
        ;;
    *)
        echo "--target must be plausible, devfix or defectrepairing" >&2; exit 1
        ;;
esac

# gpt-oss:120b needs an H100; qwen3.6:35b fits an L40S (docs/campaign.md).
if [ -z "$GPU" ]; then
    case "$ORACLE" in
        *gpt-oss*) GPU="H100:1" ;;
        *) GPU="L40S:1" ;;
    esac
fi

# The subject lists come from replay_fixcheck.py itself, so this plan and the
# jobs can never disagree about what is replayed.
PLAN=$(.venv/bin/python - "$TARGET" "$MODEL" "$CONFIG" "$PROJECTS" "$BUG_IDS" "$CHUNKS" "$MINUTES_PER_SUBJECT" <<'PY'
import sys
from d4j.defects4j_bugs import PROJECTS, chunk
import replay_fixcheck as replay

target, model, config, selection, bug_selection, chunks, minutes = sys.argv[1:8]
chunks, minutes = int(chunks), float(minutes)
names = list(PROJECTS) if selection == "all" else [p.strip() for p in selection.split(",") if p.strip()]
unknown = [n for n in names if n not in PROJECTS]
if unknown:
    sys.exit(f"unknown project(s): {', '.join(unknown)}. Known: {', '.join(PROJECTS)}")

for name in names:
    # The generator only names output directories here; the jobs derive the real one.
    argv = ["--target", target, "--project", name, "--config", config,
            "--fixcheck-assertions", "previous-assertion"]
    if model:
        argv += ["--model", model]
    if bug_selection != "all":
        argv += ["--bug-id", bug_selection]
    args = replay.build_parser().parse_args(argv)
    problems = replay.validate(args)
    if problems:
        sys.exit("; ".join(problems))
    subjects, _excluded = replay.enumerate_subjects(args)
    ids = [s["subject"] for s in subjects]
    if not ids:
        continue
    groups = chunk(ids, chunks) if chunks > 1 else [ids]
    for index, group in enumerate(groups, start=1):
        # The work itself plus an hour of slack, capped at the partition's 6 days.
        total_minutes = min(int(len(group) * minutes) + 60, 6 * 24 * 60)
        label = name if len(groups) == 1 else f"{name}-c{index}"
        print(f"{name}\t{label}\t{','.join(group)}\t{len(group)}\t{total_minutes // 60:02d}:{total_minutes % 60:02d}:00")
PY
)

echo "========================================"
echo "FixCheck replay: target=$TARGET oracle=$ORACLE model=${MODEL:--} config=$CONFIG gpu=$GPU"
echo "========================================"
printf "%-18s %-10s %-14s %s\n" "PROJECT" "SUBJECTS" "TIME" "JOB"
TOTAL=0

while IFS=$'\t' read -r project label subjects count walltime; do
    [ -z "$project" ] && continue
    TOTAL=$((TOTAL + count))

    if [ -n "$DRY_RUN" ]; then
        printf "%-18s %-10s %-14s %s\n" "$label" "$count" "$walltime" "(dry-run)"
        echo "    TARGET=$TARGET PROJECT=$project SUBJECTS=$subjects ORACLE=$ORACLE MODEL=$MODEL CONFIG=$CONFIG \\"
        echo "      sbatch --job-name=fcr-$TARGET-$label --gpus=$GPU --cpus-per-task=$CPUS --time=$walltime \\"
        echo "        --export=ALL scripts/replay_fixcheck_job.sbatch"
        continue
    fi

    mkdir -p scripts/logs
    # Through the environment, not --export=NAME=VALUE: SLURM would cut a
    # comma-separated SUBJECTS list at its first comma (see runCampaign.sh).
    export PROJECT_DIR TARGET PROJECT="$project" SUBJECTS="$subjects" ORACLE MODEL CONFIG \
           TIMEOUT EXTRA_ARGS
    JOB_ID=$(sbatch --parsable --hold \
        --job-name="fcr-$TARGET-$label" \
        --gpus="$GPU" \
        --cpus-per-task="$CPUS" \
        --time="$walltime" \
        --output="scripts/logs/%j/slurm.out" \
        --error="scripts/logs/%j/slurm.err" \
        --export=ALL \
        scripts/replay_fixcheck_job.sbatch)

    mkdir -p "scripts/logs/$JOB_ID/subjects"
    scontrol release "$JOB_ID"
    printf "%-18s %-10s %-14s %s\n" "$label" "$count" "$walltime" "$JOB_ID"
done <<< "$PLAN"

echo "========================================"
echo "Total subjects: $TOTAL"
if [ -n "$DRY_RUN" ]; then
    echo "(dry run: nothing was submitted)"
else
    echo "Logs: scripts/logs/<job_id>/  (slurm.out, ollama.log, status.jsonl, subjects/)"
fi
