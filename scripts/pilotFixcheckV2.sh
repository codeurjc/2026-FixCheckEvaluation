#!/bin/bash

# Submits the FixCheck v2 pilot (docs/fixcheck-v2-protocol.md, "Phase 3") through
# runFixcheckReplay.sh, one job per (target, oracle, config, project), since
# --bug-id applies to every project a submission lists.
#
# Usage:
#   ./scripts/pilotFixcheckV2.sh [--oracles qwen,gpt-oss] [--targets plausible,devfix,defectrepairing] [--dry-run]
#
# Re-running it is safe: replay_fixcheck.py resumes, so subjects already
# replayed are skipped.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ORACLES="qwen,gpt-oss"
TARGETS="plausible,devfix,defectrepairing"
DRY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --oracles) ORACLES="$2"; shift 2 ;;
        --targets) TARGETS="$2"; shift 2 ;;
        --dry-run) DRY="--dry-run"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

declare -A MODEL_OF=([qwen]="ollama/qwen3.6:35b" [gpt-oss]="ollama/gpt-oss:120b")

# The report's cases with a plausible patch of record, per model.
declare -A PLAUSIBLE_qwen=(
    [Cli]=35 [Math]=10,40,67,91 [JxPath]=8,13 [Gson]=11 [Chart]=3,8 [Lang]=5,61
    [Closure]=111 [Collections]=20 [Compress]=3 [Codec]=10
)
declare -A PLAUSIBLE_gptoss=(
    [Cli]=35 [Math]=10,67,91 [JxPath]=8,13 [Chart]=3,8 [Lang]=5,61 [Closure]=101,111
    [Mockito]=31 [Collections]=20 [JacksonDatabind]=6 [Compress]=3 [Codec]=10
)
# Developer fixes: the union of the two lists above.
declare -A DEVFIX=(
    [Cli]=35 [Math]=10,40,67,91 [JxPath]=8,13 [Gson]=11 [Chart]=3,8 [Lang]=5,61
    [Closure]=101,111 [Mockito]=31 [Collections]=20 [JacksonDatabind]=6 [Compress]=3 [Codec]=10
)
# 10 Correct + 10 Incorrect patches with an author configuration, random.Random(2026).
declare -A DEFECTREPAIRING=(
    [Chart]=Patch1,Patch4,Patch91 [Closure]=Patch96,Patch99 [Lang]=Patch190,Patch26
    [Math]=Patch194,Patch196,Patch197,Patch207,Patch209,Patch46,PatchHDRepair7,Patch48,Patch68,PatchHDRepair5,PatchHDRepair9
    [Time]=PatchHDRepair10,Patch183
)

submit() {
    echo ">>> $*"
    ./scripts/runFixcheckReplay.sh "$@" $DRY | grep -E "^[A-Za-z]+ +[0-9]+ +[0-9:]+ " || true
}

for oracle in ${ORACLES//,/ }; do
    model="${MODEL_OF[$oracle]}"
    [ -z "$model" ] && { echo "unknown oracle: $oracle" >&2; exit 1; }
    for target in ${TARGETS//,/ }; do
        case "$target" in
            plausible)
                if [ "$oracle" = "qwen" ]; then
                    for p in "${!PLAUSIBLE_qwen[@]}"; do
                        submit --target plausible --model "$model" --projects "$p" --bug-id "${PLAUSIBLE_qwen[$p]}"
                    done
                else
                    for p in "${!PLAUSIBLE_gptoss[@]}"; do
                        submit --target plausible --model "$model" --projects "$p" --bug-id "${PLAUSIBLE_gptoss[$p]}"
                    done
                fi ;;
            devfix)
                for p in "${!DEVFIX[@]}"; do
                    submit --target devfix --oracle "$model" --projects "$p" --bug-id "${DEVFIX[$p]}"
                done ;;
            defectrepairing)
                for config in author ours; do
                    for p in "${!DEFECTREPAIRING[@]}"; do
                        submit --target defectrepairing --config "$config" --oracle "$model" \
                            --projects "$p" --bug-id "${DEFECTREPAIRING[$p]}"
                    done
                done ;;
            *) echo "unknown target: $target" >&2; exit 1 ;;
        esac
    done
done
