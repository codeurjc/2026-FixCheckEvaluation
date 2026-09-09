#!/usr/bin/env bash
#
# probeContextVram.sh -- does this model fit on this GPU at this context length?
#
# The campaign raised the context window from 49152 to 131072 tokens because 17
# of the 854 prompts exceeded the old one and were silently truncated. A bigger
# window costs KV-cache VRAM, and the cost is not something to assume: a model
# that does not fit gets partially offloaded to CPU (slow, but it runs, so
# nothing fails visibly) or fails to load at all.
#
# Worse, it must fit *once*: FixCheck's OllamaGenerator sends no options, so if
# the daemon's OLLAMA_CONTEXT_LENGTH does not already cover what the fix
# generator asks for, Ollama starts a second runner and the model thrashes
# between the two. That is why ollama_serve.sh derives the daemon's value from
# FixGenerator.DEFAULT_CONTEXT_LENGTH.
#
# This measures the real thing: load the model at the target context, generate
# once, and report GPU memory and whether any of it landed on the CPU.
#
#   bash scripts/probeContextVram.sh qwen3.6:35b
#   bash scripts/probeContextVram.sh qwen3.6:35b 131072 65536
#   bash scripts/probeContextVram.sh gpt-oss:120b 131072
#
# Run it on the node that will run the campaign (srun/salloc on the target GPU),
# not on the login node.

set -uo pipefail

MODEL="${1:-}"
if [ -z "$MODEL" ]; then
    echo "usage: bash scripts/probeContextVram.sh <model> [context_length ...]" >&2
    echo "  e.g. bash scripts/probeContextVram.sh qwen3.6:35b 49152 131072" >&2
    exit 2
fi
shift
CONTEXTS=("$@")
if [ ${#CONTEXTS[@]} -eq 0 ]; then
    DEFAULT_CTX=$(python -c \
        'from FixGenerator import FixGenerator; print(FixGenerator.DEFAULT_CONTEXT_LENGTH)' \
        2>/dev/null || echo 131072)
    CONTEXTS=(49152 "$DEFAULT_CTX")
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "[probe] nvidia-smi not found -- run this on a GPU node." >&2
    exit 1
fi

echo "[probe] GPU(s) visible here:"
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader | sed 's/^/         /'
echo

# shellcheck source=scripts/ollama_serve.sh
source "$(dirname "${BASH_SOURCE[0]}")/ollama_serve.sh"

printf '%-12s  %-10s  %-12s  %-9s  %-8s  %s\n' \
    CONTEXT LOADED_OK GPU_MEM_MiB CPU_SPLIT GEN_TOK VERDICT

for ctx in "${CONTEXTS[@]}"; do
    export OLLAMA_CONTEXT_LENGTH="$ctx"
    if ! start_ollama "$MODEL" "/tmp/probe_ollama_${ctx}.log" >/dev/null 2>&1; then
        printf '%-12s  %-10s  %-12s  %-9s  %-8s  %s\n' \
            "$ctx" no - - - "daemon did not start (see /tmp/probe_ollama_${ctx}.log)"
        continue
    fi

    # One real generation, so the KV cache is actually allocated. A bare /api/ps
    # before any request reports the model's weights only.
    gen=$(curl -sf -m 900 "http://localhost:${OLLAMA_PORT}/api/chat" \
        -d "{\"model\":\"${MODEL}\",\"stream\":false,
             \"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: ok\"}],
             \"options\":{\"num_ctx\":${ctx},\"num_predict\":16}}" 2>/dev/null)
    gen_tok=$(printf '%s' "$gen" | python -c \
        'import json,sys; print((json.load(sys.stdin) or {}).get("eval_count","?"))' \
        2>/dev/null || echo "?")

    # /api/ps reports what the loaded runner occupies, and splits it into the
    # part on GPU and the part on CPU. Any CPU share means it did not fit.
    ps_json=$(curl -sf -m 30 "http://localhost:${OLLAMA_PORT}/api/ps" 2>/dev/null)
    read -r loaded total_mib gpu_mib <<<"$(printf '%s' "$ps_json" | python -c '
import json, sys
try:
    models = (json.load(sys.stdin) or {}).get("models") or []
except Exception:
    models = []
if not models:
    print("no 0 0")
else:
    m = models[0]
    total = m.get("size") or 0
    gpu = m.get("size_vram") or 0
    print(f"yes {total // (1024*1024)} {gpu // (1024*1024)}")
' 2>/dev/null || echo "no 0 0")"

    cpu_mib=$(( total_mib - gpu_mib ))
    if [ "$loaded" != "yes" ]; then
        verdict="model not resident -- load failed"
    elif [ "$cpu_mib" -gt 0 ]; then
        verdict="DOES NOT FIT: ${cpu_mib} MiB on CPU (will be slow)"
    elif [ "$gen_tok" = "?" ] || [ "$gen_tok" = "0" ]; then
        verdict="loaded on GPU but generation failed"
    else
        verdict="fits entirely on GPU"
    fi

    printf '%-12s  %-10s  %-12s  %-9s  %-8s  %s\n' \
        "$ctx" "$loaded" "$gpu_mib" "$cpu_mib" "$gen_tok" "$verdict"

    stop_ollama >/dev/null 2>&1
done

echo
echo "[probe] A non-zero CPU_SPLIT means the model did not fit at that context"
echo "        length. Either lower FixGenerator.DEFAULT_CONTEXT_LENGTH, or use a"
echo "        GPU with more memory -- do not just let it spill, because the"
echo "        campaign's per-bug timeout is what would fail, silently and far"
echo "        from the cause."
