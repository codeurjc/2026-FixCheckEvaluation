# Plan: add a FixCheck validation step to the patch-generation pipeline

## Goal

Integrate [FixCheck](https://github.com/facumolina/fixcheck) (vendored in `fixcheck/`)
as an **additional, optional validation step** in `Experiment.py`. Today a patch is
labeled `fixed` when the trigger tests pass and no new failures appear. That criterion
accepts *overfitting* patches (patches that make the trigger test pass without actually
fixing the defect). FixCheck attacks exactly that gap: starting from the bug-revealing
test, it generates small variations of it ("prefixes" with mutated inputs plus
regenerated/reused assertions), runs them against the **patched** program, and ranks the
failing variations by similarity to the original failure. Failing variations that look
like the original failure are strong evidence the patch is incorrect.

The integration must **not** change the existing `fixed` verdict. FixCheck produces a
new, independent signal (`fixcheck` section in `result.json`) that flags plausible
patches as *suspicious* or *supported*.

---

## Context you need before implementing

### How the current pipeline works (`Experiment.py`)

Pipeline steps (all Defects4J commands run inside an ephemeral Docker container from
image `defects4j:3.0.1`, with the host `--workdir` bind-mounted at the same absolute
path; see `start_container()` and `exec_in_container()` in `docker_utils.py`):

1. `defects4j checkout -p <project> -v <bug>b -w <workdir>` (buggy version).
2. `defects4j compile`, then `defects4j test` (pre-fix) → `test_before.log`.
3. `defects4j info` (metadata), optional issue fetch.
4. `locate_source_files()` / `read_sources()` → buggy sources.
5. `get_trigger_tests()` → list of `"FQCN::method"` strings (via
   `defects4j export -p tests.trigger`). Optional: `extract_trigger_test_code()`
   (trigger method source) and `run_trigger_tests()` (isolated failure logs — reads the
   `failing_tests` file Defects4J writes into the workdir after each
   `defects4j test -t <t>` run).
6. `FixGenerator.generate()` → unified diff.
7. `apply_diff()` (git apply / patch fallbacks), then `defects4j test` (post-fix,
   which also recompiles) → `test_after.log`.
8. `evaluate_fix()` → `(triggers_fixed, new_failures, fixed)`; artifacts written to
   `results/<model>/<project>/Bug_<bug_id>/[<iteration>/]`.

`run_iterations.py` shells out to `Experiment.py` N times per bug, forwards the
fix-generation flags, and aggregates `summary.json`.

Useful helpers already present: `export_property(container, workdir, prop)` (clean
`defects4j export` values), `run_step()`, `write_text()`.

### How FixCheck works (`fixcheck/`)

- Java tool, main class `org.imdea.fixcheck.FixCheck`, built with
  `./gradlew shadowJar` → `build/libs/fixcheck-all-1.0.0.jar` (Gradle 8.0.2 wrapper;
  bytecode target is **1.8**, so the jar itself runs fine on the Java 11 that ships in
  the `defects4j:3.0.1` image — this was checked: the image has OpenJDK 11 only).
- Invocation: `java -cp <fixcheck-all jar>:<classpath-of-patched-project> org.imdea.fixcheck.FixCheck -p <properties-file>`.
  The classpath **must contain the patched project's classes, test classes, and all
  test dependencies** — FixCheck compiles and runs the generated test variations
  in-process against that classpath (see `runner/PrefixRunner.java`: it uses the JDK
  compiler with `-classpath java.class.path` and JUnit 4's `JUnitCore`).
- Required properties (see `properties/FixCheckProperties.java` — all are mandatory,
  it `System.exit(1)`s on any missing one):

  | Property | Meaning | Where we get it |
  |---|---|---|
  | `test-classes-path` | dir with compiled test classes | `<workdir>/` + `defects4j export -p dir.bin.tests` |
  | `test-classes-src` | dir with test sources | `<workdir>/` + `defects4j export -p dir.src.tests` |
  | `test-class` | FQCN of the bug-revealing test class | trigger tests (`FQCN::method` → FQCN) |
  | `test-methods` | bug-revealing method names, `:`-separated | trigger tests |
  | `test-failure-trace-log` | file with the **original** (pre-patch) failure trace | captured pre-fix from Defects4J's `failing_tests` file |
  | `inputs-class` | type of the literals to mutate (`int`, `long`, `double`, `boolean`, `java.lang.String`, `Object`, …) | heuristic over the trigger-test source (see below) |
  | `number-of-prefixes` | how many test variations to generate | CLI flag, small default |
  | `assertion-generator` | one of `assert-true`, `previous-assertion`, `replit-code-llm`, `gpt-3.5`, `codellama`, `llama3.1` (see `properties/AssertionGeneratorProperty.java`) | CLI flag, default `previous-assertion` |

- Outputs, written **relative to the process CWD**:
  - `fixcheck-output/report.csv` — one data row; header:
    `test_class, input_prefixes, inputs_class, target_class, prefixes_gen_time, assertions_gen_time, prefixes_running_time, output_prefixes, passing_prefixes, crashing_prefixes, assertion_failing_prefixes`.
  - `fixcheck-output/scores-failing-tests.csv` — failing variation → similarity score
    (0..1, Levenshtein-based similarity between the variation's failure trace and the
    original failure trace; see `checker/FailureChecker.java`). **High score ⇒ the
    variation reproduces (a failure very similar to) the original bug on the patched
    code ⇒ the patch is likely incorrect.**
  - `fixcheck-output/{passing-tests,failing-tests,non-compiling-tests}/` — generated
    test sources (`.java`).
  - stdout is verbose and worth keeping as `fixcheck.log`.
- `FixCheck.main()` returns exit code 0 even when generation partially fails, so
  **success must be judged by the presence/content of `report.csv`, not the exit code**.
- One FixCheck run handles **one test class**. Bugs whose trigger tests span several
  classes need one run per class.
- `fixcheck.sh` is only a thin wrapper that writes the properties file and uses
  CWD-relative paths (`build/libs/...`); **do not use it** — generate the properties
  file from Python and call `java` directly. That avoids CWD coupling and races.
- Constraints to respect:
  - The LLM assertion generators (`codellama`, `llama3.1`) have the Ollama URL
    **hardcoded to `http://localhost:11434`** (see `assertion/Llama3_1Ollama.java`,
    `assertion/CodeLlamaOllama.java`). Inside the container, `localhost` is the
    container itself, and this project's Ollama listens on a non-standard port
    (`localhost:1995`). So the LLM generators **cannot work out of the box** →
    default to `previous-assertion` (reuses the original test's assertions; fully
    offline) and leave LLM assertions as an optional phase 2.
  - `previous-assertion` also has a special interaction with the transformer: when it
    is selected, the original assertions are kept in the generated prefix (see
    `transform/input/InputTransformer.java`), which is exactly the "small input change,
    same expectations" behavior we want.
  - `inputs-class` must name a literal type that actually occurs in the trigger test
    method, otherwise no transformation can be applied. Valid keys are in
    `transform/input/InputHelper.java` (`boolean`, `int`, `long`, `double`,
    `java.lang.String`, `Object`, boxed variants).
  - `FixCheckProperties.loadFailureLog()` reads the failure-trace file as-is (it only
    truncates at `at sun.reflect.NativeMethodAccessorImpl.invoke0` and strips the first
    line for DefectRepairing paths). Give it a **clean Defects4J `failing_tests`
    excerpt**, with no shell-command headers prepended (unlike what
    `run_trigger_tests()` currently returns).

---

## Design decisions (summary)

1. **Where it runs:** inside the same ephemeral container, after the post-fix test run,
   **only when the patch applied and all trigger tests pass** (`triggers_fixed`).
   Running it on non-plausible patches wastes minutes per bug and adds no information.
2. **Off by default**, enabled with `--fixcheck`. It is an *extra* validation.
3. **No changes to FixCheck's Java code** in phase 1. The jar is built once (setup
   step) and mounted read-only into the container.
4. **Properties file + `java -cp` invocation generated from Python**, CWD set to a
   per-run scratch directory inside the workdir so outputs never collide.
5. **Verdict is advisory:** new `fixcheck` block in `result.json` +
   `fixcheck_suspicious` boolean; `fixed` is untouched.

---

## Implementation steps

### Step 0 — One-time setup: build the FixCheck jar

Add `scripts/buildFixcheck.sh` (mirroring the style of the existing `scripts/*.sh`,
which are run from the repo root):

```bash
#!/bin/bash
# Builds fixcheck/build/libs/fixcheck-all-1.0.0.jar inside a throwaway
# defects4j:3.0.1 container (the image has JDK 11; the gradle wrapper
# downloads Gradle 8.0.2 itself, so network access is required).
set -euo pipefail
FIXCHECK_DIR="$(cd "$(dirname "$0")/../fixcheck" && pwd)"
docker run --rm \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$FIXCHECK_DIR":"$FIXCHECK_DIR" -w "$FIXCHECK_DIR" \
  defects4j:3.0.1 ./gradlew --no-daemon shadowJar
# Smoke-test: the jar must start under the image's Java 11.
docker run --rm \
  -u "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$FIXCHECK_DIR":"$FIXCHECK_DIR":ro -w "$FIXCHECK_DIR" \
  defects4j:3.0.1 java -cp build/libs/fixcheck-all-1.0.0.jar \
  org.imdea.fixcheck.FixCheck --help
echo "OK: $FIXCHECK_DIR/build/libs/fixcheck-all-1.0.0.jar"
```

Notes:
- `sourceCompatibility = 1.8` in `fixcheck/build.gradle`, so JDK 11 both builds and
  runs it. If the gradle build fails under 11 for any reason, fall back to building
  with any host JDK 17 (`cd fixcheck && ./gradlew shadowJar`) — the produced jar is
  identical for our purposes.
- Document this as a prerequisite of `--fixcheck` (README, see step 7).

### Step 1 — Mount the FixCheck repo into the experiment container

In `Experiment.py`:

- Add a module-level constant `FIXCHECK_DIR = os.path.join(HERE-equivalent, "fixcheck")`
  (use `os.path.dirname(os.path.abspath(__file__))`) and
  `FIXCHECK_JAR = os.path.join(FIXCHECK_DIR, "build", "libs", "fixcheck-all-1.0.0.jar")`.
- Extend `start_container(client, mount_dir)` with an optional parameter
  `extra_mounts=None` (dict of `{host_path: {"bind": ..., "mode": ...}}`) merged into
  `volumes`. When `--fixcheck` is set, pass
  `{FIXCHECK_DIR: {"bind": FIXCHECK_DIR, "mode": "ro"}}` (same absolute path inside,
  consistent with the workdir convention).
- In `main()`, before starting the container, fail fast if `--fixcheck` is set and
  `FIXCHECK_JAR` does not exist, with a message pointing at
  `bash scripts/buildFixcheck.sh`.

### Step 2 — Capture the original (pre-fix) failure traces per test class

FixCheck needs the buggy version's failure trace, but by the time FixCheck runs the
patch is applied and `failing_tests` has been overwritten. Capture it right after the
trigger tests are known (pipeline step 5b), before fix generation:

- Refactor `run_trigger_tests(container, workdir, trigger_tests)` into a lower-level
  helper that returns structured data, e.g.
  `run_trigger_tests_raw(container, workdir, trigger_tests) -> dict[test, str]`
  mapping each `"FQCN::method"` to the raw content of the `failing_tests` file for its
  isolated run. Rebuild the existing log format (`"$ cmd\n<content>"` joined blocks)
  on top of it so `--include-test-log` output does not change.
- Add `group_triggers_by_class(trigger_tests) -> dict[fqcn, list[method]]` (pure
  helper, trivially unit-testable).
- When `--fixcheck` is set (and, to avoid double execution, also reusing the runs when
  `--include-test-log` is set), write one clean trace file per trigger class:
  `<workdir>/.fixcheck/<FQCN>.failing_tests` containing the concatenated **raw**
  `failing_tests` contents of that class's trigger methods (no `$ cmd` headers).
  Create the `.fixcheck` directory with `os.makedirs(..., exist_ok=True)` from the
  host (shared volume — same path both sides).

### Step 3 — New FixCheck helpers in `Experiment.py`

Add a clearly-delimited section (`# ------------------------------ fixcheck`) with:

1. `infer_inputs_class(test_method_source: str) -> str` — pure heuristic over the
   trigger method source (already obtainable via `extract_java_method`; reuse it):
   - count literal occurrences with regexes: string literals `"..."` →
     `java.lang.String`; float literals (`\b\d+\.\d+[fFdD]?\b`) → `double`; integer
     literals (`\b\d+[lL]?\b`, excluding those inside float matches) → `int` (`long`
     if the `l`/`L` suffix dominates); `\btrue\b|\bfalse\b` → `boolean`.
   - return the most frequent type; tie-break preferring `java.lang.String` > `int` >
     `double` > `long` > `boolean`; default `java.lang.String` when nothing matches.
   - `--fixcheck-inputs-class` overrides the heuristic entirely.
2. `build_fixcheck_properties(...) -> str` — pure function returning the properties
   file text given `test_classes_path`, `test_class`, `test_methods`,
   `test_classes_src`, `failure_log_path`, `inputs_class`, `num_prefixes`,
   `assertion_generator` (mirror the key names from the table above exactly).
3. `parse_fixcheck_report(report_csv_text: str) -> dict | None` — parse the one-row
   `report.csv` into
   `{"total": int, "passing": int, "crashing": int, "assertion_failing": int, "non_compiling": int, ...}`.
   Note `report.csv` has **no non-compiling column**; compute it as
   `total - passing - crashing - assertion_failing`. Return `None` on
   missing/malformed content.
4. `parse_fixcheck_scores(scores_csv_text: str) -> list[float]` — parse
   `scores-failing-tests.csv` (rows of `<test-name>,<score>`; be lenient about a
   header row and blank lines) into a list of floats.
5. `run_fixcheck(container, workdir, trigger_tests, test_method_sources, args) -> dict`
   — the orchestrator:
   - `dir.bin.tests = export_property(container, workdir, "dir.bin.tests")` and
     `dir.src.tests = export_property(container, workdir, "dir.src.tests")`;
     `cp_test = export_property(container, workdir, "cp.test")` (full test classpath
     of the compiled, patched checkout — Defects4J guarantees it contains classes,
     test classes and dependencies).
   - Ensure the patched checkout is compiled: the post-fix `defects4j test` already
     compiled it; still, run `defects4j compile` defensively (cheap, idempotent) if
     FixCheck is invoked in a path where the post-fix test did not run.
   - For each `(fqcn, methods)` from `group_triggers_by_class(...)`:
     - scratch dir `run_dir = <workdir>/.fixcheck/run_<simple-class-name>/`
       (host-side `os.makedirs`).
     - write the properties text (item 2) to `run_dir/fixcheck.properties` from the
       host.
     - execute inside the container with
       `exec_in_container(container, f"java -cp {FIXCHECK_JAR}:{cp_test} org.imdea.fixcheck.FixCheck -p {props_path}", workdir=run_dir)`
       — `workdir=run_dir` makes `fixcheck-output/` land inside the scratch dir.
     - read back `run_dir/fixcheck-output/report.csv` and
       `scores-failing-tests.csv` from the host; build a per-class record:
       `{"test_class": fqcn, "ok": report is not None, "report": report,
       "scores": scores, "max_score": max(scores, default 0.0)}`.
   - Aggregate into the returned dict:

     ```json
     {
       "ran": true,
       "assertion_generator": "...",
       "num_prefixes": N,
       "inputs_class": {"<fqcn>": "..."},
       "per_test_class": [ ... records ... ],
       "failing_prefixes": <sum of crashing+assertion_failing>,
       "max_failure_similarity": <max over classes>,
       "suspicious": <failing_prefixes > 0 and max_failure_similarity >= threshold>
     }
     ```
   - Any exception or missing report must **not** abort the experiment: log a
     `[experiment] WARNING`, and return `{"ran": true, "ok": false, "error": ...}`-style
     data instead. FixCheck is advisory.

### Step 4 — Wire it into `main()`

- After `evaluate_fix(...)` (pipeline step 7→8 boundary):

  ```python
  fixcheck_result = None
  if args.fixcheck and applied and triggers_fixed:
      fixcheck_result = run_fixcheck(container, workdir, trigger_tests,
                                     trigger_method_sources, args)
  ```

  where `trigger_method_sources` maps each trigger class to its extracted trigger
  method source (reuse the `--include-test-code` machinery:
  `locate_test_files` + `read_sources` + `extract_java_method`; run it
  unconditionally when `--fixcheck` is set, independently of the prompt flag).
- Artifacts: copy each `run_dir/fixcheck-output/` to
  `<results_dir>/fixcheck/<simple-class-name>/` (`shutil.copytree`), plus write the
  container stdout of each run as `fixcheck/<simple-class-name>/fixcheck.log`.
- `result.json`: add `"fixcheck": fixcheck_result` (or `None` when not run) and a
  top-level convenience boolean
  `"fixcheck_suspicious": bool(fixcheck_result and fixcheck_result.get("suspicious"))`.
- Summary printout: add a line, e.g.
  `[experiment] FixCheck: 3/25 variations failing, max similarity 0.91 -> SUSPICIOUS`
  (or `not run`).

### Step 5 — CLI flags

Add to `Experiment.py`'s parser (and mirror all of them in `run_iterations.py`'s
"forwarded flags" block — extend `experiment_args()` the same way the existing
`--include-*` flags are handled):

| Flag | Default | Meaning |
|---|---|---|
| `--fixcheck` | off | run FixCheck on plausible patches |
| `--fixcheck-prefixes` | `25` | `number-of-prefixes` (100 upstream is too slow per bug for iteration runs) |
| `--fixcheck-assertions` | `previous-assertion` | `assertion-generator` option key |
| `--fixcheck-inputs-class` | `None` (heuristic) | force `inputs-class` |
| `--fixcheck-similarity-threshold` | `0.8` | min `max_failure_similarity` to mark the patch suspicious |

In `run_iterations.py`, also extend the per-bug/global summaries and `summary.json`
with a `fixcheck_suspicious` count (same pattern as `fixed`/`triggers_fixed` via the
`flag()` helper — it reads the top-level boolean added in step 4).

### Step 6 — Unit tests (mandatory per CLAUDE.md)

New file `test/unit/test_fixcheck_units.py` covering the pure helpers:

- `group_triggers_by_class`: mixed classes, ordering, duplicate methods.
- `infer_inputs_class`: string-dominated method → `java.lang.String`; int-only →
  `int`; float → `double`; boolean; empty/no literals → default; suffix `L` → `long`.
- `build_fixcheck_properties`: exact key names, `:`-joined methods, all values
  present.
- `parse_fixcheck_report`: happy path from a verbatim sample row (take the header from
  `ReportWriter.java`), malformed/empty → `None`, non-compiling arithmetic.
- `parse_fixcheck_scores`: with/without header, blank lines, empty file → `[]`.

Run `.venv/bin/python -m pytest test/unit -v` and make it pass before finishing.

Optional (nice to have): an e2e test `test/e2e/test_fixcheck_lang1.py` marked like the
benchmark test (skipped unless the jar exists) that runs FixCheck with
`previous-assertion` on Lang-1's *developer* fix; keep it out of the default run.

### Step 7 — Documentation (mandatory per CLAUDE.md)

- `README.md`:
  - *How it works*: new step 7b "FixCheck overfitting check (optional)" describing the
    run condition (`applied && triggers_fixed`), the generator default, and the
    verdict semantics (high similarity failing variation ⇒ suspicious patch).
  - *Requirements*: the FixCheck jar prerequisite + `bash scripts/buildFixcheck.sh`.
  - *Usage* table: the five new flags.
  - *Output*: `fixcheck/` artifact directory, new `result.json` fields
    (`fixcheck`, `fixcheck_suspicious`).
  - `run_iterations.py` section: note the new forwarded flags and summary field.
- `CLAUDE.md`: mention the FixCheck integration under Architecture (one bullet:
  Experiment.py optionally runs the vendored `fixcheck/` jar inside the container on
  plausible patches) and the build script under Commands.
- `scripts/README.md`: document `buildFixcheck.sh`.

### Step 8 — Phase 2 (optional, out of scope for the first PR)

Only after phase 1 works end-to-end:

- LLM-generated assertions: the Ollama-backed generators need
  (a) a configurable base URL (patch `Llama3_1Ollama`/`CodeLlamaOllama` to read an
  `OLLAMA_BASE_URL` env var with `http://localhost:11434` fallback, rebuild the jar), and
  (b) container network access to the host daemon (`extra_hosts={"host.docker.internal": "host-gateway"}`
  in `containers.run`, then `OLLAMA_BASE_URL=http://host.docker.internal:1995`).
- Possibly a `defects4j-fixcheck` derived Docker image with the jar baked in, if
  mounting proves annoying on the SLURM cluster.

---

## Verification checklist (for the implementer)

1. `bash scripts/buildFixcheck.sh` produces the jar and the smoke test prints
   FixCheck's help.
2. `.venv/bin/python -m pytest test/unit -v` — all green, including the new
   `test_fixcheck_units.py`.
3. Manual end-to-end (needs Docker + Ollama):
   `python Experiment.py --project Lang --bug-id 1 --workdir ./workspace --fixcheck --fixcheck-prefixes 5`
   - If the LLM patch is plausible: `results/.../fixcheck/<class>/report.csv` exists,
     `result.json` has a populated `fixcheck` block, the summary line prints.
   - If the patch is not plausible: `fixcheck.ran` is absent/`None` and nothing broke.
   - A deterministic alternative that avoids LLM luck: temporarily check out Lang-1,
     apply the developer fix, and run only the FixCheck step (or use the optional e2e
     test from step 6).
4. `python run_iterations.py --project Lang --bug-id 1 --iterations 2 --fixcheck ...`
   forwards the flags and `summary.json` contains the `fixcheck_suspicious` count.
5. Failure injection: point `FIXCHECK_JAR` at a bogus path with `--fixcheck` →
   `Experiment.py` exits early with the "run scripts/buildFixcheck.sh" message;
   delete `report.csv` mid-parse (unit test) → warning, run completes, `fixed`
   unaffected.
