#!/bin/bash
# One-time setup for `--fixcheck`: builds fixcheck/build/libs/fixcheck-all-1.0.0.jar
# inside a throwaway defects4j:3.0.1 container (the image has JDK 11; the Gradle
# wrapper downloads Gradle 8.0.2 itself, so network access is required).
#
# Run from the repository root:
#   bash scripts/buildFixcheck.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXCHECK_DIR="$REPO_ROOT/fixcheck"
DEFECTS4J_DIR="$REPO_ROOT/defects4j"
DEFECTS4J_IMAGE="defects4j:3.0.1"

# The defects4j:3.0.1 image is built from this checkout; clone it if it
# isn't already there.
if [ ! -d "$DEFECTS4J_DIR" ]; then
  echo "Cloning defects4j into $DEFECTS4J_DIR ..."
  git clone git@github.com:rjust/defects4j.git "$DEFECTS4J_DIR"
fi

# Build the image if it isn't already available locally (the build itself
# takes a while, so skip it once it's there).
if ! docker image inspect "$DEFECTS4J_IMAGE" >/dev/null 2>&1; then
  echo "Building $DEFECTS4J_IMAGE from $DEFECTS4J_DIR ..."
  docker build -t "$DEFECTS4J_IMAGE" "$DEFECTS4J_DIR"
fi

# fixcheck/ is vendored but gitignored, so a fresh checkout of this repo
# won't have it; clone it if it isn't already there.
if [ ! -d "$FIXCHECK_DIR" ]; then
  echo "Cloning fixcheck into $FIXCHECK_DIR ..."
  git clone git@github.com:facumolina/fixcheck.git "$FIXCHECK_DIR"
fi

# --network=host: the build downloads Gradle 8.0.2 and the Maven dependencies,
# and Docker's default bridge DNS does not resolve on every host (the
# defects4j image ships a nameserver that may not be reachable). Sharing the
# host's network namespace makes the build work wherever the host itself has
# network access.
docker run --rm --network=host \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$FIXCHECK_DIR":"$FIXCHECK_DIR" -w "$FIXCHECK_DIR" \
  "$DEFECTS4J_IMAGE" ./gradlew --no-daemon shadowJar

# Smoke-test: the jar must start under the image's Java 11.
docker run --rm \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$FIXCHECK_DIR":"$FIXCHECK_DIR":ro -w "$FIXCHECK_DIR" \
  "$DEFECTS4J_IMAGE" java -cp build/libs/fixcheck-all-1.0.0.jar \
  org.imdea.fixcheck.FixCheck --help

echo "OK: $FIXCHECK_DIR/build/libs/fixcheck-all-1.0.0.jar"
