cd /home/maes/2026-FixCheckEvaluation
PROJECT=Lang
BUG_ID=1
MODEL=ollama/gpt-oss:120b
ITERATIONS=5

OLLAMA_BASE_URL=http://localhost:1995 \
.venv/bin/python run_iterations.py \
    --project $PROJECT \
    --bug-id $BUG_ID \
    --workdir ./workspace \
    --iterations $ITERATIONS \
    --model $MODEL \
    --include-test-code --include-test-log --include-issue
