#!/bin/bash
# Makes Defects4J's per-project directory-layout caches writable inside the
# defects4j:3.0.1 image, and retags the result in place.
#
#   bash scripts/patchDefects4jImage.sh
#
# Why this is needed
# ------------------
# Defects4J caches each project's source/test directory layout per revision in
# framework/projects/<P>/dir-layout.csv. On a cache *miss* it works the layout
# out from the checkout and appends it to that file (Project.pm's
# _add_to_layout_map -> Utils::append_to_file_unless_matches). In the image the
# file is root:root 644, and our containers run as the host uid so that files
# created on the shared volume stay host-owned (see Experiment.start_container),
# so the append dies with:
#
#   Cannot open file for appending .../Chart/dir-layout.csv: Permission denied
#
# Exactly one bug in the whole benchmark hits that cache miss -- Chart 26, whose
# buggy revision 102 is absent from Chart's layout map -- and it fails
# identically for every model, ~5 s in, with no result. Everything else is a
# cache hit and never writes.
#
# chmod rather than shipping a corrected CSV: this lets Defects4J compute the
# real layout itself instead of hardcoding an inferred one, and it closes the
# whole class of cache-miss failures. The entry it appends lives in the
# container's ephemeral layer and is recomputed each run -- about a second.
#
# The image is retagged in place, so nothing that names defects4j:3.0.1 has to
# change. IMPORTANT: rebuilding the base image (docker build -t defects4j:3.0.1
# ./defects4j) discards this patch -- re-run this script afterwards. Same trap
# as editing fixcheck/ without regenerating scripts/fixcheck-patches/.

set -euo pipefail

IMAGE="defects4j:3.0.1"
LABEL="org.fixcheckeval.layout-writable"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: image $IMAGE not found. Build it first with:" >&2
    echo "  docker build -t $IMAGE ./defects4j" >&2
    exit 1
fi

# Idempotent: the label is the marker, so re-running is a no-op rather than
# stacking another layer on every invocation.
if [ "$(docker image inspect -f "{{index .Config.Labels \"$LABEL\"}}" "$IMAGE" 2>/dev/null)" = "1" ]; then
    echo "$IMAGE is already patched (label $LABEL=1); nothing to do."
    exit 0
fi

OLD_ID=$(docker image inspect -f '{{.Id}}' "$IMAGE")
echo "Patching $IMAGE (current id: ${OLD_ID#sha256:})..."

# Built from stdin so no Dockerfile is left behind in the repo.
docker build -t "$IMAGE" - <<EOF
FROM $IMAGE
# Defects4J appends newly-determined directory layouts here; a root-owned 644
# file makes that impossible for the non-root uid our containers run as.
RUN chmod a+w /defects4j/framework/projects/*/dir-layout.csv
LABEL $LABEL="1"
EOF

NEW_ID=$(docker image inspect -f '{{.Id}}' "$IMAGE")
echo
echo "OK: $IMAGE is now ${NEW_ID#sha256:}"
if [ "$OLD_ID" != "$NEW_ID" ]; then
    echo "The previous image (${OLD_ID#sha256:}) is now untagged; remove it with:"
    echo "  docker rmi ${OLD_ID#sha256:}"
fi
