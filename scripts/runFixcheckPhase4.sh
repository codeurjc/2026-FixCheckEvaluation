#!/bin/bash

# Submits the whole FixCheck v2 measurement (docs/fixcheck-v2-protocol.md,
# "Phase 4") through runFixcheckReplay.sh: every plausible patch of record with
# its own model as oracle, every bug's developer fix and every DefectRepairing
# patch, the last two under both oracles.
#
#   ./scripts/runFixcheckPhase4.sh --dry-run
#   ./scripts/runFixcheckPhase4.sh
#   ./scripts/runFixcheckPhase4.sh --targets devfix --oracles qwen
#
# Each project is split into jobs of about SUBJECTS_PER_JOB subjects, so one
# large project cannot hold a card for days: Closure's 174 developer fixes are
# 3.7 days in one job and ~12 h in five. Everything resumes, so a job that dies
# (or is stopped by the GPU placement guard) is fixed by running this again.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ORACLES="qwen,gpt-oss"
TARGETS="plausible,devfix,defectrepairing"
DRY=""
EXTRA=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --oracles) ORACLES="$2"; shift 2 ;;
        --targets) TARGETS="$2"; shift 2 ;;
        --retry-errored) EXTRA="$EXTRA --retry-errored"; shift ;;
        --dry-run) DRY="--dry-run"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

declare -A MODEL_OF=([qwen]="ollama/qwen3.6:35b" [gpt-oss]="ollama/gpt-oss:120b")
# From the pilot: qwen averaged 20.6 min per subject, gpt-oss 6.8 min.
declare -A SUBJECTS_PER_JOB=([qwen]=25 [gpt-oss]=40)
declare -A MINUTES_PER_SUBJECT=([qwen]=30 [gpt-oss]=20)

DR_PROJECTS="Chart,Closure,Lang,Math,Mockito,Time"

# Prints "<project> <chunks>" per project that has subjects for this selection.
plan() {
    .venv/bin/python - "$@" <<'PY'
import sys
import replay_fixcheck as replay
from d4j.defects4j_bugs import PROJECTS

target, model, config, projects, per_job = sys.argv[1:6]
names = list(PROJECTS) if projects == "all" else projects.split(",")
per_job = int(per_job)
for name in names:
    argv = ["--target", target, "--project", name, "--config", config,
            "--fixcheck-assertions", "previous-assertion"]
    if model:
        argv += ["--model", model]
    args = replay.build_parser().parse_args(argv)
    if replay.validate(args):
        continue
    subjects, _ = replay.enumerate_subjects(args)
    if subjects:
        print(name, max(1, -(-len(subjects) // per_job)))
PY
}

submit() {
    echo ">>> $*"
    ./scripts/runFixcheckReplay.sh "$@" $EXTRA $DRY | grep -E "^[A-Za-z]+(-c[0-9]+)? +[0-9]+ +[0-9:]+ " || true
}

for oracle in ${ORACLES//,/ }; do
    model="${MODEL_OF[$oracle]}"
    [ -z "$model" ] && { echo "unknown oracle: $oracle" >&2; exit 1; }
    per_job="${SUBJECTS_PER_JOB[$oracle]}"
    minutes="${MINUTES_PER_SUBJECT[$oracle]}"
    for target in ${TARGETS//,/ }; do
        case "$target" in
            plausible)
                while read -r project chunks; do
                    [ -z "$project" ] && continue
                    submit --target plausible --model "$model" --projects "$project" \
                        --chunks "$chunks" --minutes-per-subject "$minutes"
                done < <(plan plausible "$model" ours all "$per_job") ;;
            devfix)
                while read -r project chunks; do
                    [ -z "$project" ] && continue
                    submit --target devfix --oracle "$model" --projects "$project" \
                        --chunks "$chunks" --minutes-per-subject "$minutes"
                done < <(plan devfix "" ours all "$per_job") ;;
            defectrepairing)
                for config in author ours; do
                    while read -r project chunks; do
                        [ -z "$project" ] && continue
                        submit --target defectrepairing --config "$config" --oracle "$model" \
                            --projects "$project" --chunks "$chunks" --minutes-per-subject "$minutes"
                    done < <(plan defectrepairing "" "$config" "$DR_PROJECTS" "$per_job")
                done ;;
            *) echo "unknown target: $target" >&2; exit 1 ;;
        esac
    done
done
