PROJECT=Lang
BUG_ID=1
MODEL=ollama/gpt-oss:120b

OLLAMA_BASE_URL=http://localhost:1995 \
.venv/bin/python Experiment.py \
    --project $PROJECT \
    --bug-id $BUG_ID \
    --workdir ./workspace \
    --model $MODEL \
    --include-test-code --include-test-log --include-issue