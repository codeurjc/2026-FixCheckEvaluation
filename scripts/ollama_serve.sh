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

# start_ollama <model> <log_file>
start_ollama() {
    local model_name="${1#ollama/}"
    local log_file="$2"
    local seed=$(( _OLLAMA_PORT_BASE + (${SLURM_JOB_ID:-$$} % _OLLAMA_PORT_SPAN) ))

    # One runner, one model, and a context length matching what
    # llms/ollama_llm.py requests (num_ctx=49152). FixCheck's OllamaGenerator
    # sends no options at all, so without this the server would spin up a
    # *second* runner at its default context for the assertion calls -- and two
    # runners of a 64 GB model do not fit on a 96 GB H100, so the model would be
    # unloaded and reloaded on every alternation between fix and assertions.
    export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-49152}"
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
}
