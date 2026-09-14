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

# Apply the local patches, in order (what each one fixes and why is in
# scripts/fixcheck-patches/README.md). The clone is gitignored, so the patches
# live in scripts/fixcheck-patches/ and are re-applied here after every fresh
# clone. Idempotent: a patch that already reverse-applies is skipped.
for patch in "$REPO_ROOT"/scripts/fixcheck-patches/*.patch; do
  name="$(basename "$patch")"
  if git -C "$FIXCHECK_DIR" apply --reverse --check "$patch" >/dev/null 2>&1; then
    echo "Patch already applied: $name"
  elif git -C "$FIXCHECK_DIR" apply --check "$patch" >/dev/null 2>&1; then
    echo "Applying patch: $name"
    git -C "$FIXCHECK_DIR" apply "$patch"
  else
    echo "ERROR: $name neither applies nor reverse-applies in $FIXCHECK_DIR" >&2
    exit 1
  fi
done

# --network=host: the build downloads Gradle 8.0.2 and the Maven dependencies,
# and Docker's default bridge DNS does not resolve on every host (the
# defects4j image ships a nameserver that may not be reachable). Sharing the
# host's network namespace makes the build work wherever the host itself has
# network access.
#
# FixCheck's own unit tests run first: the patches change how prefixes are
# compiled, loaded and run, and a jar that builds but no longer does that
# correctly would only show up as silently different verdicts.
docker run --rm --network=host \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$FIXCHECK_DIR":"$FIXCHECK_DIR" -w "$FIXCHECK_DIR" \
  "$DEFECTS4J_IMAGE" ./gradlew --no-daemon test shadowJar

# The jar must not carry third-party libraries under their own package names:
# placed on a class path next to a subject that is one of them, FixCheck's copy
# shadows the program under analysis (Defects4J's Cli, Lang and Collections all
# ran against it before 0004-relocate-dependencies.patch).
JAR="$FIXCHECK_DIR/build/libs/fixcheck-all-1.0.0.jar"
unrelocated=$(unzip -Z1 "$JAR" \
  | grep -E '^(org/apache/commons|com/google|com/github/javaparser|javassist|com/opencsv|org/json)/.*\.class$' \
  | head -5 || true)
if [ -n "$unrelocated" ]; then
  echo "ERROR: $JAR carries unrelocated dependency classes, e.g.:" >&2
  echo "$unrelocated" >&2
  exit 1
fi

# Smoke-test: the jar must start under the image's Java 11.
docker run --rm \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$FIXCHECK_DIR":"$FIXCHECK_DIR":ro -w "$FIXCHECK_DIR" \
  "$DEFECTS4J_IMAGE" java -cp build/libs/fixcheck-all-1.0.0.jar \
  org.imdea.fixcheck.FixCheck --help

echo "OK: $FIXCHECK_DIR/build/libs/fixcheck-all-1.0.0.jar"
