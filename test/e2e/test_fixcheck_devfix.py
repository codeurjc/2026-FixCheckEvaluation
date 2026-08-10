"""
Integration test: run FixCheck against Defects4J bugs' *developer* fixes.

Unlike the LLM-generated fixes exercised by ``test_experiment_lang1.py`` /
``test_benchmark_lang1.py``, this applies the actual upstream patch (checked
out as the ``<id>f`` "fixed" revision and diffed against ``<id>b``) so the
result does not depend on LLM luck: a genuinely correct fix should never be
flagged suspicious. This is the deterministic alternative described in
docs/plan-add-fixcheck-step.md's verification checklist.

The test is parametrized over two axes:

- **the subject**, from :data:`FIXCHECK_BUGS` -- add a candidate by appending
  a ``(project, bug_id)`` pair there;
- **the assertion generator**, from ``--fixcheck-assertions`` (comma-separated,
  default ``previous-assertion``), so the same subjects can be compared under
  several generators in one go.

They are crossed, and each combination gets its own container run (~2 min,
more when the generator calls an LLM) shared by the four checks below.

Which generator is used is not a detail: with ``previous-assertion`` upstream
strips the original assertions and never adds them back (``InputTransformer``
compares ``ASSERTION_GENERATOR`` against the option key while
``FixCheckProperties`` stores the resolved class name, so the guard never
fires, and ``UsePreviousAssertGenerator``'s re-append is commented out).
Every prefix then runs without assertions, which makes ``passing`` counts
vacuous and leaves crashes as the only detectable failure -- worth keeping in
mind when reading a "not suspicious" verdict.

Not every Defects4J bug is a usable FixCheck subject (see *Not every bug is a
FixCheck subject* in README.md), so a listed bug has three possible outcomes:

- **passed** -- FixCheck analyzed at least one of the bug's trigger classes
  and did not flag the developer fix. Individual classes it declined to
  attempt are tolerated, since a bug can mix the two: Math 69's
  ``SpearmansRankCorrelationTest`` inherits ``testPValueNearZero`` from
  ``PearsonsCorrelationTest``, which is analyzed normally.
- **skipped** -- FixCheck ran cleanly but had nothing to analyze at all:
  *every* trigger class was declined because its literals only occur inside
  assertions (Lang 1's ``NumberUtilsTest``) or its trigger method is
  inherited and therefore invisible to FixCheck (Lang 10's
  ``FastDateFormat_ParserTest``). That is documented upstream behavior, not a
  defect, so it is reported as a skip with the reason rather than a failure.
- **failed** -- anything else: the integration, or FixCheck itself, broke.

Every run's artifacts are copied to
``logs/test/<Project>_<BugId>/<generator>/`` for manual inspection -- most
usefully ``<TestClass>/fixcheck.log`` (the prompts and the assertions the
generator produced) and ``<TestClass>/fixcheck-output/`` (the generated prefix
sources, ``report.csv`` and ``scores-failing-tests.csv``). Keying by generator
keeps two of them comparable side by side for the same bug; each directory is
wiped at the start of its own run so it never mixes results.

Requires a running Docker daemon with the ``defects4j:3.0.1`` image and the
FixCheck jar built (``bash scripts/buildFixcheck.sh``). Both are checked
independently of Ollama/any LLM backend -- with the default
``previous-assertion`` generator this test never calls one. Skipped
automatically when either prerequisite is missing.

Run with:

    .venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s

    # a single subject
    .venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s -k Lang-12

    # compare two assertion generators on every subject
    .venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s \
        --fixcheck-assertions previous-assertion,codellama
"""

import difflib
import json
import os
import shutil

import pytest

from Experiment import (
    DEFECTS4J_IMAGE,
    FIXCHECK_DIR,
    FIXCHECK_JAR,
    evaluate_fix,
    extract_trigger_method_sources_by_class,
    get_trigger_tests,
    locate_source_files,
    locate_test_files,
    parse_failing_test_names,
    read_sources,
    run_step,
    run_trigger_tests_raw,
    start_container,
)
from FixCheckWrapper import (
    FIXCHECK_ASSERTION_GENERATORS,
    FixCheckWrapper,
    fixcheck_failure_log_path,
    group_triggers_by_class,
    needs_host_network,
    write_fixcheck_failure_logs,
)

# Candidate subjects, as ``(project, bug_id)``. Extend this list to try a new
# bug; each entry becomes its own parametrized run (test id ``<Project>-<id>``).
#
# Lang 12 is the reference subject: its ``RandomStringUtilsTest`` declares
# inputs in ordinary statements, so it exercises the path that actually
# produces a report, and its second trigger method (``testLANG805``) has no
# mutable literal -- covering the per-method filtering that keeps one awkward
# method from aborting the whole class.
FIXCHECK_BUGS = [
    ("Lang", "12"),
    ("Math", "69"),
    # ("Lang", "1"),   # verified skip: NumberUtilsTest is all assertions
]

NUM_PREFIXES = 5
SIMILARITY_THRESHOLD = 0.8

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGS_DIR = os.path.join(_REPO_ROOT, "logs", "test")


def pytest_generate_tests(metafunc):
    """Turn ``--fixcheck-assertions`` into a second parametrization axis.

    Crossed with :data:`FIXCHECK_BUGS`, so ``--fixcheck-assertions
    previous-assertion,codellama`` runs every subject under both generators
    and each combination keeps its own log directory. Done here rather than
    with a static ``params=`` list because the choice is a run-time one: an
    LLM-backed generator costs a model call per prefix, so which generators
    are worth paying for depends on what is being investigated.
    """
    if "assertion_generator" not in metafunc.fixturenames:
        return
    raw = metafunc.config.getoption("--fixcheck-assertions")
    generators = [g.strip() for g in raw.split(",") if g.strip()]
    unknown = [g for g in generators if g not in FIXCHECK_ASSERTION_GENERATORS]
    if unknown:
        # Fail at collection rather than after a couple of minutes of
        # container setup, which is when FixCheck itself would reject it.
        raise pytest.UsageError(
            f"unknown --fixcheck-assertions value(s) {unknown}; "
            f"valid options are {FIXCHECK_ASSERTION_GENERATORS}"
        )
    metafunc.parametrize("assertion_generator", generators, scope="module")


def _docker_image_available():
    """True when the Docker daemon is reachable and the image is present."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        tags = [tag for image in client.images.list() for tag in image.tags]
        return DEFECTS4J_IMAGE in tags
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not os.path.isfile(FIXCHECK_JAR),
        reason=f"FixCheck jar not built; run: bash scripts/buildFixcheck.sh "
               f"(expected at {FIXCHECK_JAR})",
    ),
    pytest.mark.skipif(
        not _docker_image_available(),
        reason=f"Docker daemon or image {DEFECTS4J_IMAGE} not available",
    ),
]


def _developer_diff(buggy_sources, fixed_sources):
    """Build a unified diff from the buggy sources to the developer's fix."""
    fixed_by_path = dict(fixed_sources)
    diffs = []
    for rel_path, buggy_content in buggy_sources:
        fixed_content = fixed_by_path.get(rel_path, buggy_content)
        if fixed_content == buggy_content:
            continue
        diff = difflib.unified_diff(
            buggy_content.split("\n"), fixed_content.split("\n"),
            fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", lineterm="",
        )
        diffs.append("\n".join(diff))
    return "\n".join(d for d in diffs if d)


def _collect_logs(log_dir, workdir, result, trigger_tests, dev_diff):
    """Copy a run's artifacts out of the container volume into ``log_dir``.

    The checkout lives in a pytest ``tmp_path`` that is eventually recycled,
    so anything worth inspecting by hand has to be copied out while it still
    exists. Keeps the per-class layout FixCheck produced, plus the inputs
    needed to make sense of it: the patch under test and the *pre-fix* failure
    trace each generated prefix is compared against.
    """
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(log_dir, "developer.diff"), "w", encoding="utf-8") as f:
        f.write(dev_diff)

    for fqcn in group_triggers_by_class(trigger_tests):
        trace = fixcheck_failure_log_path(workdir, fqcn)
        if os.path.exists(trace):
            shutil.copy(trace, os.path.join(log_dir, f"{fqcn}.failing_tests"))

    for record in result["per_test_class"]:
        run_dir = record.get("run_dir")
        if not run_dir or not os.path.isdir(run_dir):
            continue
        dest = os.path.join(log_dir, record["test_class"].rsplit(".", 1)[-1])
        # copytree over the whole run dir: it holds fixcheck.properties,
        # fixcheck.log and fixcheck-output/ (report.csv, the scores file and
        # the generated prefix sources under passing-/failing-/non-compiling-tests).
        shutil.copytree(run_dir, dest, dirs_exist_ok=True)


@pytest.fixture(scope="module", params=FIXCHECK_BUGS,
                ids=[f"{p}-{b}" for p, b in FIXCHECK_BUGS])
def fixcheck_devfix(request, tmp_path_factory, assertion_generator):
    """Apply a bug's developer fix and run FixCheck against it.

    Checks out both the buggy (``<id>b``) and fixed (``<id>f``) revisions, builds
    a diff between them (the real developer patch, not an LLM guess), applies
    it to the buggy checkout, confirms it actually fixes the trigger tests
    (a precondition for the test's premise, not something FixCheck-related),
    and then runs FixCheck on it exactly as ``Experiment.py`` would with
    ``--fixcheck``. The container is always removed afterwards.

    Module-scoped, so the four checks below share one container run per
    (bug, generator) pair rather than paying for it four times.
    """
    import docker
    from Experiment import apply_diff

    project, bug_id = request.param
    mount_dir = str(tmp_path_factory.mktemp("workspace"))
    workdir_b = os.path.join(mount_dir, f"{project}_{bug_id}")
    workdir_f = os.path.join(mount_dir, f"{project}_{bug_id}_fixed")

    # Start from a clean slate so a stale directory can never be mistaken for
    # this run's output. Scoped to this (bug, generator) pair, so other
    # subjects keep theirs and two generators can be compared side by side.
    log_dir = os.path.join(LOGS_DIR, f"{project}_{bug_id}", assertion_generator)
    if os.path.isdir(log_dir):
        shutil.rmtree(log_dir)

    client = docker.from_env()
    extra_mounts = {FIXCHECK_DIR: {"bind": FIXCHECK_DIR, "mode": "ro"}}
    container = start_container(
        client, mount_dir, extra_mounts=extra_mounts,
        network_mode="host" if needs_host_network(assertion_generator) else None,
    )
    try:
        # 1. Check out both revisions of the bug.
        checkout_b = run_step(
            container, f"defects4j checkout -p {project} -v {bug_id}b -w {workdir_b}",
            workdir=None, description=f"Checking out {project} {bug_id}b",
        )
        assert checkout_b.ok, f"buggy checkout failed:\n{checkout_b.output}"
        checkout_f = run_step(
            container, f"defects4j checkout -p {project} -v {bug_id}f -w {workdir_f}",
            workdir=None, description=f"Checking out {project} {bug_id}f (developer fix)",
        )
        assert checkout_f.ok, f"fixed checkout failed:\n{checkout_f.output}"

        # 2. Compile + test the buggy revision (pre-fix).
        compile_before = run_step(
            container, "defects4j compile", workdir_b,
            description="Compiling buggy sources (pre-fix)",
        )
        assert compile_before.ok, f"pre-fix compilation failed:\n{compile_before.output}"
        test_before = run_step(
            container, "defects4j test", workdir_b,
            description="Running test suite (pre-fix)",
        )
        failing_before_names = parse_failing_test_names(test_before.output)

        # 3. Trigger tests + their pre-fix failure trace (must be captured
        #    now, before the patch is applied -- same reason Experiment.py
        #    does this in pipeline step 5d).
        trigger_tests = get_trigger_tests(container, workdir_b)
        assert trigger_tests, "no trigger tests found"
        trigger_raw = run_trigger_tests_raw(container, workdir_b, trigger_tests)
        write_fixcheck_failure_logs(workdir_b, trigger_tests, trigger_raw)

        test_classes = sorted({t.split("::")[0] for t in trigger_tests})
        test_files = locate_test_files(container, workdir_b, test_classes)
        trigger_method_sources = extract_trigger_method_sources_by_class(
            trigger_tests, read_sources(test_files)
        )

        # 4. Build the developer's diff (<id>b -> <id>f) and apply it to workdir_b.
        files_b = locate_source_files(container, workdir_b)
        buggy_sources = read_sources(files_b)
        assert buggy_sources, "no buggy source files could be read"
        files_f = [(rel, os.path.join(workdir_f, rel)) for rel, _ in files_b]
        fixed_sources = read_sources(files_f)
        dev_diff = _developer_diff(buggy_sources, fixed_sources)
        assert dev_diff.strip(), "developer fix produced an empty diff"

        applied, apply_log = apply_diff(container, workdir_b, dev_diff)
        assert applied, f"developer diff failed to apply:\n{apply_log}"

        # 5. Compile + test post-fix, and confirm the developer fix actually
        #    fixes the trigger tests -- a precondition for this test's
        #    premise, independent of FixCheck.
        compile_after = run_step(
            container, "defects4j compile", workdir_b,
            description="Compiling patched sources (post-fix)",
        )
        assert compile_after.ok, f"post-fix compilation failed:\n{compile_after.output}"
        test_after = run_step(
            container, "defects4j test", workdir_b,
            description="Running test suite (post-fix)",
        )
        failing_after_names = parse_failing_test_names(test_after.output)
        triggers_fixed, new_failures, _fixed = evaluate_fix(
            trigger_tests, failing_before_names, failing_after_names, applied
        )
        assert triggers_fixed, (
            "developer fix did not make the trigger tests pass -- "
            f"still failing: {sorted(set(trigger_tests) & set(failing_after_names))}"
        )

        # 6. Run FixCheck exactly as Experiment.py would with --fixcheck.
        fixcheck = FixCheckWrapper(
            num_prefixes=NUM_PREFIXES,
            assertion_generator=assertion_generator,
            similarity_threshold=SIMILARITY_THRESHOLD,
        )
        result = fixcheck.run(container, workdir_b, trigger_tests, trigger_method_sources)
        result["project"], result["bug_id"] = project, bug_id
        print(f"\n===== FixCheck result ({project} {bug_id}, {assertion_generator}, "
              "developer fix) =====\n")
        print(result)
        print("\n===== End of FixCheck result =====\n")

        # 7. Copy the artifacts out before the tmp checkout goes away. Done
        #    before any skip below, so a "not a FixCheck subject" verdict is
        #    just as inspectable as a real result.
        _collect_logs(log_dir, workdir_b, result, trigger_tests, dev_diff)
        print(f"[test] FixCheck artifacts kept in: {log_dir}")

        # A bug whose every trigger class was skipped is not a FixCheck
        # subject at all -- documented upstream behavior rather than a
        # defect, so report it as a skip carrying the reason.
        records = result["per_test_class"]
        if result["ok"] and records and all(r.get("skipped") for r in records):
            reasons = "; ".join(
                f"{r['test_class']}: {r.get('error')}" for r in records
            )
            pytest.skip(f"{project} {bug_id} is not a FixCheck subject -- {reasons}")

        yield result
    finally:
        container.stop()
        container.remove()


def test_fixcheck_runs_cleanly(fixcheck_devfix):
    """FixCheck completes without an orchestration-level error."""
    result = fixcheck_devfix
    assert result["ok"], f"FixCheck did not run cleanly: {result.get('error')}"


def _analyzed(result):
    """The trigger classes FixCheck actually attempted.

    Excludes the ones it declined up front (``skipped``): a trigger method
    whose literals all sit inside assertions, or that is inherited and so
    invisible to FixCheck, cannot yield a report by design. A bug can mix the
    two -- Math 69's ``SpearmansRankCorrelationTest`` inherits
    ``testPValueNearZero`` from ``PearsonsCorrelationTest``, which is analyzed
    normally -- so this has to be per class, not per bug. When *every* class
    is skipped the fixture skips the bug outright.
    """
    return [r for r in result["per_test_class"] if not r.get("skipped")]


def test_fixcheck_produced_a_report_for_every_analyzed_class(fixcheck_devfix):
    """Every trigger class FixCheck attempted got a parsed report.csv."""
    result = fixcheck_devfix
    assert result["per_test_class"], "no per-class FixCheck records"
    analyzed = _analyzed(result)
    assert analyzed, "FixCheck attempted no trigger class"
    for record in analyzed:
        assert record["ok"], (
            f"FixCheck({record['test_class']}) produced no usable report: "
            f"{record.get('error')}"
        )


def test_fixcheck_generated_usable_prefixes(fixcheck_devfix):
    """FixCheck actually built and ran variations, not just a report.

    Without this, ``test_developer_fix_is_not_suspicious`` would pass
    vacuously whenever every generated prefix failed to compile: the verdict
    would read "supported" purely because nothing was ever executed.
    """
    result = fixcheck_devfix
    for record in _analyzed(result):
        report = record["report"]
        assert report, f"no report for {record['test_class']}"
        executed = report["passing"] + report["crashing"] + report["assertion_failing"]
        assert executed > 0, (
            f"FixCheck({record['test_class']}) compiled none of its "
            f"{report['total']} generated prefixes, so the verdict carries no "
            "information (non_compiling="
            f"{report['non_compiling']})"
        )


def test_developer_fix_is_not_suspicious(fixcheck_devfix):
    """The real developer fix must not be flagged as an overfitting patch."""
    result = fixcheck_devfix
    assert result["analyzed_test_classes"] > 0, (
        "no trigger class was analyzed, so 'not suspicious' would be vacuous"
    )
    assert not result["suspicious"], (
        f"FixCheck flagged {result['project']} {result['bug_id']}'s actual "
        f"developer fix as suspicious "
        f"(failing_prefixes={result['failing_prefixes']}, "
        f"max_failure_similarity={result['max_failure_similarity']}); "
        "this is the fix that defines the bug as fixed, so this indicates a "
        "bug in the integration rather than a real overfitting patch."
    )
