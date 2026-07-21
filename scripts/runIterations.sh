PROJECT=${PROJECT:-Chart}
# BUG_ID may be a single id, or a space-separated list/range (e.g. "1-5 8").
# It is passed unquoted below so it expands into multiple --bug-id values.
BUG_ID=${BUG_ID:-1}
MODEL=${MODEL:-ollama/qwen3.6:35b} #ollama/gpt-oss:120b
ITERATIONS=${ITERATIONS:-10}

OLLAMA_BASE_URL=http://localhost:1995 \
.venv/bin/python run_iterations.py \
    --project $PROJECT \
    --bug-id $BUG_ID \
    --workdir ./workspace \
    --iterations $ITERATIONS \
    --model $MODEL \
    --temperature 0.0 \
    --include-test-code --include-test-log --include-issue
