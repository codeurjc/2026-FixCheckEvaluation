"""
Integration test: run FixCheck against a Defects4J bug's *developer* fix.

Unlike the LLM-generated fixes exercised by ``test_experiment_lang1.py`` /
``test_benchmark_lang1.py``, this applies the actual upstream patch (checked
out as the ``<id>f`` "fixed" revision and diffed against ``<id>b``) so the
result does not depend on LLM luck: a genuinely correct fix should never be
flagged suspicious. This is the deterministic alternative described in
docs/plan-add-fixcheck-step.md's verification checklist.

The subject is **Lang 12**, not Lang 1, because most Defects4J bugs are not
usable FixCheck subjects at all (see *Not every bug is a FixCheck subject* in
README.md):

- Lang 1's ``TestLang747`` is nothing but ``assertEquals(...)`` lines, and
  FixCheck only mutates literals outside assertions, so no ``inputs-class``
  exists for it and the run is skipped rather than crashed.
- Lang 10's ``FastDateFormat_ParserTest`` *inherits* its trigger method, which
  FixCheck cannot see, and its sibling class's generated prefixes fail to
  compile -- which upstream turns into a ``NullPointerException`` and no
  report at all.

Lang 12's ``RandomStringUtilsTest`` declares its inputs in ordinary
statements, so it exercises the path that actually produces a report. It also
covers the per-method filtering: its second trigger method (``testLANG805``)
has no mutable literal and must be dropped from ``test-methods``, since
FixCheck would otherwise abort the whole class over it.

Requires a running Docker daemon with the ``defects4j:3.0.1`` image and the
FixCheck jar built (``bash scripts/buildFixcheck.sh``). Both are checked
independently of Ollama/any LLM backend -- this test never calls one. Skipped
automatically when either prerequisite is missing.

Run with:

    .venv/bin/python -m pytest test/e2e/test_fixcheck_devfix.py -v -s
"""

import difflib
import os

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
from FixCheckWrapper import FixCheckWrapper, write_fixcheck_failure_logs

PROJECT = "Lang"
BUG_ID = "12"


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


@pytest.fixture(scope="module")
def fixcheck_devfix(tmp_path_factory):
    """Apply the bug's developer fix and run FixCheck against it.

    Checks out both the buggy (``<id>b``) and fixed (``<id>f``) revisions, builds
    a diff between them (the real developer patch, not an LLM guess), applies
    it to the buggy checkout, confirms it actually fixes the trigger tests
    (a precondition for the test's premise, not something FixCheck-related),
    and then runs FixCheck on it exactly as ``Experiment.py`` would with
    ``--fixcheck``. The container is always removed afterwards.
    """
    import docker
    from Experiment import apply_diff

    mount_dir = str(tmp_path_factory.mktemp("workspace"))
    workdir_b = os.path.join(mount_dir, f"{PROJECT}_{BUG_ID}")
    workdir_f = os.path.join(mount_dir, f"{PROJECT}_{BUG_ID}_fixed")

    client = docker.from_env()
    extra_mounts = {FIXCHECK_DIR: {"bind": FIXCHECK_DIR, "mode": "ro"}}
    container = start_container(client, mount_dir, extra_mounts=extra_mounts)
    try:
        # 1. Check out both revisions of the bug.
        checkout_b = run_step(
            container, f"defects4j checkout -p {PROJECT} -v {BUG_ID}b -w {workdir_b}",
            workdir=None, description=f"Checking out {PROJECT} {BUG_ID}b",
        )
        assert checkout_b.ok, f"buggy checkout failed:\n{checkout_b.output}"
        checkout_f = run_step(
            container, f"defects4j checkout -p {PROJECT} -v {BUG_ID}f -w {workdir_f}",
            workdir=None, description=f"Checking out {PROJECT} {BUG_ID}f (developer fix)",
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
            num_prefixes=5,
            assertion_generator="previous-assertion",
            similarity_threshold=0.8,
        )
        result = fixcheck.run(container, workdir_b, trigger_tests, trigger_method_sources)
        print(f"\n===== FixCheck result ({PROJECT} {BUG_ID}, developer fix) =====\n")
        print(result)
        print("\n===== End of FixCheck result =====\n")

        yield result
    finally:
        container.stop()
        container.remove()


def test_fixcheck_runs_cleanly(fixcheck_devfix):
    """FixCheck completes without an orchestration-level error."""
    result = fixcheck_devfix
    assert result["ok"], f"FixCheck did not run cleanly: {result.get('error')}"


def test_fixcheck_produced_a_report_for_every_trigger_class(fixcheck_devfix):
    """Every trigger class got a parsed report.csv, not just an attempt."""
    result = fixcheck_devfix
    assert result["per_test_class"], "no per-class FixCheck records"
    for record in result["per_test_class"]:
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
    for record in result["per_test_class"]:
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
        f"FixCheck flagged {PROJECT} {BUG_ID}'s actual developer fix as "
        f"suspicious (failing_prefixes={result['failing_prefixes']}, "
        f"max_failure_similarity={result['max_failure_similarity']}); "
        "this is the fix that defines the bug as fixed, so this indicates a "
        "bug in the integration rather than a real overfitting patch."
    )
