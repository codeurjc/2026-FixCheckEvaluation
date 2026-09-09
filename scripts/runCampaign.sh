#!/bin/bash

# Submits one scripts/project_job.sbatch per Defects4J project, so a whole
# benchmark campaign runs as several independent jobs that SLURM schedules as
# GPUs free up. Each job walks its project's bugs sequentially on one GPU.
#
# Usage:
#   ./scripts/runCampaign.sh --projects JacksonXml,Csv,Codec       # pilot
#   ./scripts/runCampaign.sh --projects all --minutes-per-bug 25   # everything
#   ./scripts/runCampaign.sh --projects all --bug-id 1             # one bug each: smoke test
#   ./scripts/runCampaign.sh --projects Closure --chunks 2         # split a big one
#   ./scripts/runCampaign.sh --projects all --dry-run              # show, don't submit
#
# The model must already be pulled: jobs never `ollama pull` (see
# scripts/ollama_serve.sh for why).

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PROJECTS="all"
BUG_IDS="all"
MODEL="ollama/gpt-oss:120b"
GPU="H100:1"
CPUS="8"
FIXCHECK_PREFIXES="10"
# Per-bug wall clock. Raised from 7200 after the first campaign: 6 runs died at
# exactly 7200.3 s, and re-running them at the same limit would reproduce them
# identically. They are test-suite-bound, not generation-bound -- the median run
# is 2.6 min and the LLM is ~1 min of it -- so this is cheap insurance: only ~9
# runs of 1700 ever exceeded 60 min.
TIMEOUT="10800"
MINUTES_PER_BUG="20"
TIME_OVERRIDE=""
CHUNKS="1"
EXTRA_ARGS=""
DRY_RUN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --projects) PROJECTS="$2"; shift 2 ;;
        --bug-id) BUG_IDS="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --gpu) GPU="$2"; shift 2 ;;
        --cpus) CPUS="$2"; shift 2 ;;
        --fixcheck-prefixes) FIXCHECK_PREFIXES="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --minutes-per-bug) MINUTES_PER_BUG="$2"; shift 2 ;;
        --time) TIME_OVERRIDE="$2"; shift 2 ;;
        --chunks) CHUNKS="$2"; shift 2 ;;
        --retry-errored) EXTRA_ARGS="$EXTRA_ARGS --retry-errored"; shift ;;
        --no-resume) EXTRA_ARGS="$EXTRA_ARGS --no-resume"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# The bug lists (and therefore the --time estimates) come from the same module
# run_project.py uses, so the campaign can never disagree with the jobs about
# which bugs exist.
PLAN=$(.venv/bin/python - "$PROJECTS" "$BUG_IDS" "$CHUNKS" "$MINUTES_PER_BUG" <<'PY'
import sys
from d4j.defects4j_bugs import PROJECTS, chunk, resolve_bug_ids

selection, bug_selection, chunks, minutes = sys.argv[1:5]
chunks, minutes = int(chunks), float(minutes)
names = list(PROJECTS) if selection == "all" else [p.strip() for p in selection.split(",") if p.strip()]

unknown = [n for n in names if n not in PROJECTS]
if unknown:
    sys.exit(f"unknown project(s): {', '.join(unknown)}. Known: {', '.join(PROJECTS)}")
# A bug selection applies to every named project, which is what makes
# `--projects all --bug-id 1` a one-bug-per-project smoke test of the whole
# campaign. resolve_bug_ids validates the ids against each project's own
# active list, so asking for one a project does not have fails by name here
# rather than after minutes of container time.

for name in names:
    ids = resolve_bug_ids(name, None if bug_selection == "all" else [bug_selection])
    groups = chunk(ids, chunks) if chunks > 1 else [ids]
    for index, group in enumerate(groups, start=1):
        # Wall clock: the work itself plus an hour of slack, capped at the
        # partition's 6-day limit. Asking for only what a project needs keeps
        # the short ones backfill-friendly instead of every job wanting 6 days.
        total_minutes = min(int(len(group) * minutes) + 60, 6 * 24 * 60)
        label = name if len(groups) == 1 else f"{name}-c{index}"
        print(f"{name}\t{label}\t{','.join(group)}\t{len(group)}\t{total_minutes // 60:02d}:{total_minutes % 60:02d}:00")
PY
)

echo "========================================"
echo "Campaign: model=$MODEL gpu=$GPU prefixes=$FIXCHECK_PREFIXES timeout=${TIMEOUT}s"
echo "========================================"
printf "%-18s %-10s %-14s %s\n" "PROJECT" "BUGS" "TIME" "JOB"
TOTAL_BUGS=0

while IFS=$'\t' read -r project label bug_ids count walltime; do
    [ -z "$project" ] && continue
    TOTAL_BUGS=$((TOTAL_BUGS + count))

    if [ -n "$DRY_RUN" ]; then
        printf "%-18s %-10s %-14s %s\n" "$label" "$count" "$walltime" "(dry-run)"
        echo "    PROJECT=$project BUG_IDS=$bug_ids MODEL=$MODEL TIMEOUT=$TIMEOUT FIXCHECK_PREFIXES=$FIXCHECK_PREFIXES \\"
        echo "      sbatch --job-name=fc-$label --gpus=$GPU --cpus-per-task=$CPUS --time=$walltime \\"
        echo "        --export=ALL scripts/project_job.sbatch"
        continue
    fi

    mkdir -p scripts/logs
    # The job's inputs travel through the *environment*, not through
    # `--export=NAME=VALUE`. SLURM separates that list with commas, so a value
    # containing one is silently truncated at the first: BUG_IDS=1,2,3 arrived
    # as BUG_IDS=1 and every project ran only its first bug. Exporting here and
    # passing a bare `--export=ALL` propagates the values intact.
    export PROJECT_DIR PROJECT="$project" BUG_IDS="$bug_ids" MODEL TIMEOUT \
           FIXCHECK_PREFIXES EXTRA_ARGS
    # Submit held so the per-job log directory can be created before SLURM
    # opens --output/--error: it will not create the parent itself, and the job
    # id is only known once the job has been submitted.
    JOB_ID=$(sbatch --parsable --hold \
        --job-name="fc-$label" \
        --gpus="$GPU" \
        --cpus-per-task="$CPUS" \
        --time="$walltime" \
        --output="scripts/logs/%j/slurm.out" \
        --error="scripts/logs/%j/slurm.err" \
        --export=ALL \
        scripts/project_job.sbatch)

    mkdir -p "scripts/logs/$JOB_ID/bugs"
    scontrol release "$JOB_ID"
    printf "%-18s %-10s %-14s %s\n" "$label" "$count" "$walltime" "$JOB_ID"
done <<< "$PLAN"

echo "========================================"
echo "Total bugs: $TOTAL_BUGS"
if [ -n "$DRY_RUN" ]; then
    echo "(dry run: nothing was submitted)"
else
    echo "Logs: scripts/logs/<job_id>/  (slurm.out, ollama.log, status.jsonl, bugs/)"
    echo
    echo "Useful commands:"
    echo "  squeue -u \$USER                                  # job status"
    echo "  tail -f scripts/logs/<job_id>/status.jsonl       # one line per finished bug"
    echo "  .venv/bin/python summarize_campaign.py           # aggregate so far"
    echo "  scancel -n fc-<Project>                          # cancel one project"
fi
