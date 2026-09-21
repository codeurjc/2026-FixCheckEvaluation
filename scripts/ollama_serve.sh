#!/bin/bash
# Ollama lifecycle for SLURM jobs. Meant to be *sourced*, not executed:
#
#     source scripts/ollama_serve.sh
#     start_ollama "ollama/gpt-oss:120b" "$LOG_DIR/ollama.log"
#     trap stop_ollama EXIT INT TERM
#
# Sets OLLAMA_PORT, OLLAMA_HOST, OLLAMA_BASE_URL and OLLAMA_PID.
#
# This exists because the cluster is a single node: every job of a campaign
# lands on the same machine. The naive version of this logic -- `pkill -f
# "ollama serve"` and a hardcoded port -- means the second job kills the first
# job's daemon and then fails to bind. Both jobs then produce results that look
# fine and are silently wrong (or nothing at all). So:
#
#   - the port is derived from the job id and probed before use, with a retry
#     onto the next port if the daemon loses a bind race at startup;
#   - the daemon binds loopback only, so a sibling job cannot reach it;
#   - stop_ollama kills exactly one PID and never pattern-matches.

OLLAMA_PORT=""
OLLAMA_PID=""

# Well above the ephemeral range used by the projects' own test servers: every
# Experiment container runs with host networking (FixCheck's generator talks to
# 127.0.0.1), so the node's port space is shared with the test suites too.
_OLLAMA_PORT_BASE=21000
_OLLAMA_PORT_SPAN=900
_OLLAMA_START_ATTEMPTS=5

# True when nothing is listening on $1 and we can bind it.
_port_is_free() {
    python3 - "$1" <<'PY'
import socket, sys
s = socket.socket()
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

# First free port at or after $1.
_next_free_port() {
    local candidate="$1"
    local limit=$((candidate + 200))
    while [ "$candidate" -lt "$limit" ]; do
        if _port_is_free "$candidate"; then
            echo "$candidate"
            return 0
        fi
        candidate=$((candidate + 1))
    done
    echo "ERROR: no free port in [$1, $limit)" >&2
    return 1
}

# How much VRAM a model needs to run fully on the GPU, in MiB. A model that
# does not fit runs partly on the CPU at ~1% of the speed, which the replay
# driver's placement guard then rejects -- so choose a card that fits.
_required_vram_mib() {
    case "$1" in
        *gpt-oss*120b*) echo 70000 ;;   # 61.7 GB of weights plus the context
        *qwen3.6*35b*)  echo 26000 ;;
        *)              echo "${OLLAMA_MIN_FREE_MIB:-26000}" ;;
    esac
}

# Claim a GPU with enough free memory for $1 and pin this job's runner to it.
# Sets CUDA_VISIBLE_DEVICES to the card's UUID and holds a lock on it for as
# long as the job lives, so two of our own jobs never choose the same card.
# Nothing can stop another user's unpinned job from using it too: this node
# applies no device isolation (see start_ollama).
_pick_gpu() {
    local model_name="$1"
    local needed; needed=$(_required_vram_mib "$model_name")
    local lock_dir="${FIXCHECK_GPU_LOCK_DIR:-$HOME/.cache/fixcheck-gpu-locks}"
    mkdir -p "$lock_dir"

    local attempt uuid name total used free
    for attempt in $(seq 1 "${_OLLAMA_GPU_ATTEMPTS:-30}"); do
        # Smallest card that fits first, so qwen takes an L40S and leaves the
        # H100s for the models that need them; then the emptiest.
        while IFS=, read -r uuid name total used; do
            uuid="${uuid// /}"; total="${total// /}"; used="${used// /}"
            free=$(( total - used ))
            [ "$free" -ge "$needed" ] || continue
            exec {_OLLAMA_GPU_LOCK_FD}> "$lock_dir/$uuid.lock" || continue
            if flock -n "$_OLLAMA_GPU_LOCK_FD"; then
                export CUDA_VISIBLE_DEVICES="$uuid"
                unset CUDA_DEVICE_ORDER   # meaningless once we pin by UUID
                echo "[ollama] GPU:$name ($uuid), ${free} MiB free, needs ${needed} MiB"
                return 0
            fi
            exec {_OLLAMA_GPU_LOCK_FD}>&-
        done < <(nvidia-smi --query-gpu=uuid,name,memory.total,memory.used \
                     --format=csv,noheader,nounits | sort -t, -k3,3n -k4,4n)

        echo "[ollama] No free GPU with ${needed} MiB (attempt $attempt); waiting 60s"
        sleep 60
    done
    echo "[ollama] ERROR: no GPU with ${needed} MiB free for $model_name" >&2
    return 1
}

# start_ollama <model> <log_file>
start_ollama() {
    local model_name="${1#ollama/}"
    local log_file="$2"
    local seed=$(( _OLLAMA_PORT_BASE + (${SLURM_JOB_ID:-$$} % _OLLAMA_PORT_SPAN) ))

    # One runner, one model, and a context length matching what the fix
    # generator requests. FixCheck's OllamaGenerator sends no num_ctx (only
    # temperature and seed, which need no reload), so without this the server
    # would spin up a *second* runner at its default context for the assertion
    # calls -- and two runners of a 64 GB model do not
    # fit on a 96 GB H100, so the model would be unloaded and reloaded on every
    # alternation between fix and assertions.
    #
    # Read from FixGenerator rather than repeated here, because the two must
    # agree: a per-request num_ctx above what the model was loaded with is
    # silently clamped, which is how 16 runs of the first campaign lost their
    # answer to a truncated prompt. If the import fails, fall back to the
    # historical value and say so rather than guessing a new one.
    if [ -z "${OLLAMA_CONTEXT_LENGTH:-}" ]; then
        OLLAMA_CONTEXT_LENGTH=$(python -c \
            'from FixGenerator import FixGenerator; print(FixGenerator.DEFAULT_CONTEXT_LENGTH)' \
            2>/dev/null) || {
            echo "[ollama] WARNING: could not read FixGenerator.DEFAULT_CONTEXT_LENGTH;" \
                 "falling back to 49152. Set OLLAMA_CONTEXT_LENGTH explicitly." >&2
            OLLAMA_CONTEXT_LENGTH=49152
        }
    fi
    export OLLAMA_CONTEXT_LENGTH
    echo "[ollama] context length: $OLLAMA_CONTEXT_LENGTH tokens"
    # Force the CUDA backend. Ollama 0.32 turned its experimental Vulkan
    # support on by default, and Vulkan does **not** honour
    # CUDA_VISIBLE_DEVICES -- which is the only GPU isolation this cluster
    # applies, since it does not confine /dev/nvidia* per job. The result is
    # that every concurrent job enumerates all 9 GPUs and picks one by looking
    # at free memory, so jobs launched together all choose the same card and
    # the second one dies with
    #   ggml_gallocr_reserve_n_impl: failed to allocate Vulkan0 buffer
    # Under CUDA the runner sees exactly the GPU SLURM gave it. The cuda_v12 /
    # cuda_v13 backends ship with the same install, so this costs nothing.
    export OLLAMA_VULKAN=0
    # ... and pick the card ourselves, because SLURM's answer cannot be
    # trusted on this node (measured 2026-09-21, probe job 16599):
    #   - gres.conf maps the types to the wrong device files. It calls
    #     /dev/nvidia0,1,7,8 H100, but those minors are the L40S at 43, 44, 83
    #     and 84; the four H100s are minors 3,4,5,6. So a --gpus=H100:1
    #     allocation is not an H100 at all.
    #   - SLURM then exports CUDA_VISIBLE_DEVICES as its own GRES index (L40S
    #     first, H100 second), which is neither the minor nor the PCI order,
    #     so no CUDA_DEVICE_ORDER makes the two agree. PCI_BUS_ID used to be
    #     set here and sent 20 gpt-oss jobs of the Phase 4 batch onto 46 GB
    #     L40S cards; before that it put qwen jobs on the H100s that gpt-oss
    #     jobs had been given, which looked like "another user filled the card".
    #   - ConstrainDevices=yes has no effect: TaskPlugin is (null), so nothing
    #     confines /dev/nvidia* per job and any card is reachable anyway.
    # _pick_gpu therefore chooses a card of its own with enough free memory and
    # pins the runner to its UUID, which no ordering can reinterpret. Report
    # the gres.conf mismatch to the cluster admins: this is a workaround.
    _pick_gpu "$model_name" || return 1
    export OLLAMA_MAX_LOADED_MODELS=1
    export OLLAMA_NUM_PARALLEL=1
    export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"

    local attempt
    for attempt in $(seq 1 $_OLLAMA_START_ATTEMPTS); do
        OLLAMA_PORT=$(_next_free_port "$seed") || return 1
        export OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT"
        export OLLAMA_BASE_URL="http://127.0.0.1:$OLLAMA_PORT"

        echo "[ollama] Attempt $attempt: starting on $OLLAMA_HOST (log: $log_file)"
        ollama serve > "$log_file" 2>&1 &
        OLLAMA_PID=$!

        local waited=0
        while [ "$waited" -lt 60 ]; do
            if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
                # Died during startup -- almost always another job winning the
                # same port between our probe and this bind. Try the next one.
                echo "[ollama] Daemon exited during startup; trying the next port."
                seed=$((OLLAMA_PORT + 1))
                OLLAMA_PID=""
                break
            fi
            if curl -sf "http://$OLLAMA_HOST/api/tags" > /dev/null 2>&1; then
                echo "[ollama] Ready on $OLLAMA_HOST (PID $OLLAMA_PID)"
                _assert_model_present "$model_name" || return 1
                return 0
            fi
            sleep 2
            waited=$((waited + 2))
        done

        if [ -n "$OLLAMA_PID" ]; then
            echo "[ollama] Timed out waiting for $OLLAMA_HOST; giving up on this port."
            stop_ollama
            seed=$((OLLAMA_PORT + 1))
        fi
    done

    echo "[ollama] ERROR: could not start after $_OLLAMA_START_ATTEMPTS attempts" >&2
    return 1
}

# The model must already be in ~/.ollama. Pulling from inside a job is banned:
# the store is shared by every concurrent job, and two simultaneous pulls of the
# same multi-GB blob is the one way to actually corrupt it.
_assert_model_present() {
    local model_name="$1"
    local wanted="$model_name"
    case "$model_name" in *:*) ;; *) wanted="$model_name:latest" ;; esac
    if curl -sf "http://$OLLAMA_HOST/api/tags" | grep -q "\"$wanted\""; then
        echo "[ollama] Model $wanted is available"
        return 0
    fi
    echo "[ollama] ERROR: model '$wanted' is not available on this daemon." >&2
    echo "[ollama] Pull it once by hand first:  ollama pull $wanted" >&2
    return 1
}

stop_ollama() {
    if [ -n "$OLLAMA_PID" ] && kill -0 "$OLLAMA_PID" 2>/dev/null; then
        echo "[ollama] Stopping daemon (PID $OLLAMA_PID)"
        kill "$OLLAMA_PID" 2>/dev/null || true
        wait "$OLLAMA_PID" 2>/dev/null || true
    fi
    OLLAMA_PID=""
    # Release the GPU claimed by _pick_gpu, so the next job can take that card
    # without waiting for this shell to exit.
    if [ -n "${_OLLAMA_GPU_LOCK_FD:-}" ]; then
        exec {_OLLAMA_GPU_LOCK_FD}>&- 2>/dev/null || true
        _OLLAMA_GPU_LOCK_FD=""
    fi
}
