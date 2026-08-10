PROJECT=Lang
BUG_ID=12
MODEL=ollama/gpt-oss:120b
OLLAMA_PORT=1995

# FixCheck writes each generated variation's assertions with the *same* model,
# through the generic Ollama generator. Note the two notations differ: the fix
# generator takes '<provider>/<model>' while FixCheck takes
# 'ollama:<model>[@[<host>:]<port>]' (the endpoint is split at '@' because the
# colon already belongs to Ollama's <model>:<version> tag), so derive one from
# the other rather than repeating the model name.
FIXCHECK_ASSERTIONS="ollama:${MODEL#ollama/}@${OLLAMA_PORT}"
# One model call per variation, so keep this modest while inspecting a run;
# Experiment.py's own default is 25.
FIXCHECK_PREFIXES=5

OLLAMA_BASE_URL=http://localhost:$OLLAMA_PORT \
.venv/bin/python Experiment.py \
    --project $PROJECT \
    --bug-id $BUG_ID \
    --workdir ./workspace \
    --model $MODEL \
    --temperature 0.0 \
    --include-test-code --include-test-log --include-issue \
    --fixcheck \
    --fixcheck-assertions "$FIXCHECK_ASSERTIONS" \
    --fixcheck-prefixes $FIXCHECK_PREFIXES
