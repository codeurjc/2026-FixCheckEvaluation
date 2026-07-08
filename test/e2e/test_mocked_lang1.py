"""
Deterministic e2e test for the Defects4J Lang 1 pipeline.

The LLM response is replayed from a pre-recorded file so the test is fully
deterministic and requires no LLM backend. Docker and the defects4j:3.0.1 image
are still needed; the test skips automatically when they are unavailable.

Pre-recorded response: results/Lang_6Jul/1/1/raw_response.txt
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from Experiment import (
    DEFECTS4J_IMAGE,
    apply_diff,
    evaluate_fix,
    extract_bug_report_url,
    extract_trigger_test_code,
    fetch_issue_text,
    get_trigger_tests,
    locate_source_files,
    locate_test_files,
    parse_failing_test_names,
    read_sources,
    run_step,
    run_trigger_tests,
    start_container,
)
from FixGenerator import FixGenerator

PROJECT = "Lang"
BUG_ID = "1"

_HERE = os.path.dirname(__file__)
RAW_RESPONSE_PATH = os.path.join(_HERE, "fixtures", "lang1_raw_response.txt")


def _docker_image_available():
    try:
        import docker

        client = docker.from_env()
        client.ping()
        tags = [tag for image in client.images.list() for tag in image.tags]
        return DEFECTS4J_IMAGE in tags
    except Exception:
        return False


@pytest.fixture(scope="session")
def lang1_mocked_pipeline(tmp_path_factory):
    """Full Experiment pipeline for Lang 1 with the LLM response mocked.

    Skips when Docker or the defects4j:3.0.1 image is unavailable, or when the
    pre-recorded response file is missing. Never skips due to LLM availability.
    """
    if not os.path.isfile(RAW_RESPONSE_PATH):
        pytest.skip(f"Pre-recorded response not found: {RAW_RESPONSE_PATH}")
    if not _docker_image_available():
        pytest.skip(f"Docker daemon or image {DEFECTS4J_IMAGE} not available")

    with open(RAW_RESPONSE_PATH, "r", encoding="utf-8") as f:
        raw_response_content = f.read()

    import docker

    mount_dir = str(tmp_path_factory.mktemp("workspace_mocked"))
    workdir = os.path.join(mount_dir, f"{PROJECT}_{BUG_ID}")

    client = docker.from_env()
    container = start_container(client, mount_dir)
    try:
        checkout = run_step(
            container,
            f"defects4j checkout -p {PROJECT} -v {BUG_ID}b -w {workdir}",
            workdir=None,
            description=f"Checking out {PROJECT} {BUG_ID}b",
        )
        assert checkout.ok, f"checkout failed:\n{checkout.output}"

        compile_before = run_step(
            container, "defects4j compile", workdir,
            description="Compiling buggy sources (pre-fix)",
        )
        assert compile_before.ok, f"pre-fix compilation failed:\n{compile_before.output}"

        test_before = run_step(
            container, "defects4j test", workdir,
            description="Running test suite (pre-fix)",
        )
        failing_before_names = parse_failing_test_names(test_before.output)
        print(f"\n[mocked] Failing tests before fix: {failing_before_names}")

        info = run_step(
            container,
            f"defects4j info -p {PROJECT} -b {BUG_ID}",
            workdir=None,
            description="Extracting bug metadata",
        )
        assert info.ok, f"info failed:\n{info.output}"

        bug_report_url = extract_bug_report_url(info.output)
        issue_text = fetch_issue_text(bug_report_url) if bug_report_url else ""

        files = locate_source_files(container, workdir)
        sources = read_sources(files)
        assert sources, "no buggy source files could be read"

        trigger_tests = get_trigger_tests(container, workdir)
        assert trigger_tests, "no trigger tests found"
        test_classes = sorted({t.split("::")[0] for t in trigger_tests})
        test_files = locate_test_files(container, workdir, test_classes)
        test_sources = extract_trigger_test_code(trigger_tests, read_sources(test_files))
        assert test_sources, "no regression test source files could be read"

        test_log = run_trigger_tests(container, workdir, trigger_tests)

        # Replace the LLM with a mock that replays the pre-recorded response.
        mock_response = MagicMock()
        mock_response.content = raw_response_content
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = mock_response

        with patch.object(FixGenerator, "_initialize_llm", return_value=mock_llm):
            generator = FixGenerator(model="mock/model")
            gen = generator.generate(
                sources,
                test_sources=test_sources,
                test_log=test_log,
                issue_text=issue_text,
            )

        diff = gen["diff"]
        print(f"\n[mocked] Generated diff ({len(diff)} chars)")

        applied, apply_log = apply_diff(container, workdir, diff)
        print(f"[mocked] Diff applied: {applied}")

        compiled = False
        compile_after_output = ""
        failing_after_names = []
        if applied:
            compile_after = run_step(
                container, "defects4j compile", workdir,
                description="Compiling fixed sources (post-fix)",
            )
            compiled = compile_after.ok
            compile_after_output = compile_after.output
            if compiled:
                test_after = run_step(
                    container, "defects4j test", workdir,
                    description="Running test suite (post-fix)",
                )
                failing_after_names = parse_failing_test_names(test_after.output)
                print(f"[mocked] Failing tests after fix: {failing_after_names}")

        triggers_fixed, new_failures, fixed = evaluate_fix(
            trigger_tests, failing_before_names, failing_after_names, applied
        )

        yield {
            "trigger_tests": trigger_tests,
            "failing_before_names": failing_before_names,
            "failing_after_names": failing_after_names,
            "diff": diff,
            "applied": applied,
            "apply_log": apply_log,
            "compiled": compiled,
            "compile_after_output": compile_after_output,
            "triggers_fixed": triggers_fixed,
            "new_failures": new_failures,
            "fixed": fixed,
        }
    finally:
        container.stop()
        container.remove()


def test_mocked_bug_present_before_fix(lang1_mocked_pipeline):
    """Lang 1b's trigger tests are failing before the pre-recorded fix is applied."""
    failing = set(lang1_mocked_pipeline["failing_before_names"])
    triggers = set(lang1_mocked_pipeline["trigger_tests"])
    assert triggers & failing, (
        f"Expected trigger tests {triggers} to be failing before the fix; "
        f"failing tests were {failing}"
    )


def test_mocked_diff_applied(lang1_mocked_pipeline):
    """The diff built from the pre-recorded response can be applied to the source tree."""
    assert lang1_mocked_pipeline["applied"], (
        f"apply_diff failed.\nApply log:\n{lang1_mocked_pipeline['apply_log']}"
    )


def test_mocked_fixed_sources_compile(lang1_mocked_pipeline):
    """The project compiles after applying the pre-recorded fix."""
    assert lang1_mocked_pipeline["compiled"], (
        f"defects4j compile failed after applying the fix.\n"
        f"{lang1_mocked_pipeline['compile_after_output']}"
    )


def test_mocked_trigger_tests_pass(lang1_mocked_pipeline):
    """The pre-recorded fix makes the bug's trigger tests pass."""
    assert lang1_mocked_pipeline["triggers_fixed"], (
        f"Trigger tests still failing after pre-recorded fix: "
        f"{sorted(set(lang1_mocked_pipeline['trigger_tests']) & set(lang1_mocked_pipeline['failing_after_names']))}"
    )


def test_mocked_no_new_failures(lang1_mocked_pipeline):
    """The pre-recorded fix does not introduce any new test failures."""
    assert not lang1_mocked_pipeline["new_failures"], (
        f"Pre-recorded fix introduced new failures: {lang1_mocked_pipeline['new_failures']}"
    )
