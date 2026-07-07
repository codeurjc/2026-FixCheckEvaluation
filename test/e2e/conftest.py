"""
Shared fixtures for the Defects4J Lang 1 integration tests.

The ``lang1_pipeline`` fixture runs the full Experiment pipeline exactly once
per session and is reused by both the deterministic mechanics tests
(``test_experiment_lang1.py``) and the model-quality benchmark
(``test_benchmark_lang1.py``), so the ~90s container run is never duplicated.

It skips automatically when the Docker daemon, the ``defects4j:3.0.1`` image, or
the target Ollama model is unavailable. Set ``OLLAMA_BASE_URL`` if Ollama is not
on ``http://localhost:1995``.
"""

import json
import os
import urllib.request

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
MODEL = os.getenv("FIXGEN_TEST_MODEL", "ollama/gpt-oss:120b")
MODEL_NAME = MODEL.replace("ollama/", "")
OLLAMA_HOST = os.getenv("OLLAMA_BASE_URL", "http://localhost:1995")


def _docker_image_available():
    try:
        import docker

        client = docker.from_env()
        client.ping()
        tags = [tag for image in client.images.list() for tag in image.tags]
        return DEFECTS4J_IMAGE in tags
    except Exception:
        return False


def _model_available():
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2) as resp:
            tags = [m["name"] for m in json.load(resp).get("models", [])]
        return MODEL_NAME in tags
    except Exception:
        return False


NUM_BENCHMARK_ATTEMPTS = int(os.getenv("FIXGEN_BENCHMARK_ATTEMPTS", "3"))


@pytest.fixture(scope="session")
def lang1_pipeline(tmp_path_factory, request):
    """Run the full Experiment pipeline for Lang 1b once and yield the outcome.

    Mirrors what Experiment.py does, including the trigger-based fix evaluation:
      checkout → compile (pre) → test (pre) → info → issue → sources →
      trigger test code → trigger test log → generate fix → apply →
      compile (post) → test (post)

    The LLM's fix attempt (step 6 onward) is non-deterministic even at
    temperature 0.0 (observed: identical config, different generated patches
    across runs). When ``--run-benchmark`` is passed, it is retried up to
    ``FIXGEN_BENCHMARK_ATTEMPTS`` (default 3) times, and the run is considered
    a success as soon as one attempt fixes the trigger tests — mirroring how
    the pipeline would actually be used (retry until it works). Mechanics-only
    runs (no ``--run-benchmark``) use a single attempt so they stay fast.

    Skips when the required infrastructure is missing. The container is always
    removed afterwards.
    """
    if not _model_available():
        pytest.skip(f"Ollama daemon unreachable or model {MODEL_NAME!r} not pulled")
    if not _docker_image_available():
        pytest.skip(f"Docker daemon or image {DEFECTS4J_IMAGE} not available")

    import docker

    mount_dir = str(tmp_path_factory.mktemp("workspace"))
    workdir = os.path.join(mount_dir, f"{PROJECT}_{BUG_ID}")
    os.environ["OLLAMA_BASE_URL"] = OLLAMA_HOST

    client = docker.from_env()
    container = start_container(client, mount_dir)
    try:
        # 1. Checkout the buggy version.
        checkout = run_step(
            container,
            f"defects4j checkout -p {PROJECT} -v {BUG_ID}b -w {workdir}",
            workdir=None,
            description=f"Checking out {PROJECT} {BUG_ID}b",
        )
        assert checkout.ok, f"checkout failed:\n{checkout.output}"

        # 2. Compile pre-fix — confirm the project builds in its buggy state.
        compile_before = run_step(
            container, "defects4j compile", workdir,
            description="Compiling buggy sources (pre-fix)",
        )
        assert compile_before.ok, f"pre-fix compilation failed:\n{compile_before.output}"

        # 3. Run tests pre-fix — record which tests were failing.
        test_before = run_step(
            container, "defects4j test", workdir,
            description="Running test suite (pre-fix)",
        )
        failing_before_names = parse_failing_test_names(test_before.output)
        print(f"\n[test] Failing tests before fix: {failing_before_names}")

        # 4. Extract bug metadata.
        info = run_step(
            container,
            f"defects4j info -p {PROJECT} -b {BUG_ID}",
            workdir=None,
            description="Extracting bug metadata",
        )
        assert info.ok, f"info failed:\n{info.output}"

        # 4b. Fetch the original bug-tracker issue report.
        bug_report_url = extract_bug_report_url(info.output)
        issue_text = fetch_issue_text(bug_report_url) if bug_report_url else ""
        print(f"\n[test] Issue report ({bug_report_url}): {len(issue_text)} chars")

        # 5. Locate and read the buggy source files.
        files = locate_source_files(container, workdir)
        sources = read_sources(files)
        assert sources, "no buggy source files could be read"

        # 5b. Locate the trigger tests and reduce them to the failing method(s).
        trigger_tests = get_trigger_tests(container, workdir)
        assert trigger_tests, "no trigger tests found"
        test_classes = sorted({t.split("::")[0] for t in trigger_tests})
        test_files = locate_test_files(container, workdir, test_classes)
        test_sources = extract_trigger_test_code(
            trigger_tests, read_sources(test_files)
        )
        assert test_sources, "no regression test source files could be read"

        # 5c. Run the trigger tests in isolation to capture their failure log.
        test_log = run_trigger_tests(container, workdir, trigger_tests)

        # 6-9. Generate a fix, apply it, and evaluate it. Retried (when running
        # the benchmark) since a single generation is not representative of
        # model quality given LLM output variance at fixed temperature.
        num_attempts = NUM_BENCHMARK_ATTEMPTS if request.config.getoption("--run-benchmark") else 1
        # Debug artifacts (prompt.txt, fix.diff, raw_response.txt) go here per
        # attempt so the exact prompt can be diffed against a manual run's
        # results/<project>/<bug>/<iteration>/ artifacts.
        debug_root = os.path.join("results", "_debug", "lang1_pipeline")
        os.makedirs(debug_root, exist_ok=True)
        with open(os.path.join(debug_root, "issue.txt"), "w", encoding="utf-8") as f:
            f.write(issue_text or "")
        with open(os.path.join(debug_root, "regression_test.log"), "w", encoding="utf-8") as f:
            f.write(test_log or "")
        outcome = None
        for attempt in range(1, num_attempts + 1):
            generator = FixGenerator(model=MODEL, temperature=0.0)
            gen = generator.generate(
                info.output, sources,
                test_sources=test_sources, test_log=test_log, issue_text=issue_text,
                results_dir=os.path.join(debug_root, f"attempt_{attempt}"),
            )

            diff = gen["diff"]
            print(f"\n===== Generated fix (Lang 1) - attempt {attempt}/{num_attempts} =====\n")
            print(diff)
            print("\n===== End of fix =====\n")

            applied, apply_log = apply_diff(container, workdir, diff)
            print(f"[test] Diff applied: {applied}")
            if not applied:
                print(f"[test] Apply log:\n{apply_log}")

            compiled = False
            compile_after_output = ""
            failing_after_names = []
            if applied:
                compile_after = run_step(
                    container, "defects4j compile", workdir,
                    description=f"Compiling fixed sources (post-fix, attempt {attempt})",
                )
                compiled = compile_after.ok
                compile_after_output = compile_after.output

                if compiled:
                    test_after = run_step(
                        container, "defects4j test", workdir,
                        description=f"Running test suite (post-fix, attempt {attempt})",
                    )
                    failing_after_names = parse_failing_test_names(test_after.output)
                    print(f"[test] Failing tests after fix: {failing_after_names}")

            triggers_fixed, new_failures, fixed = evaluate_fix(
                trigger_tests, failing_before_names, failing_after_names, applied
            )

            outcome = {
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
                "attempt": attempt,
                "num_attempts": num_attempts,
            }
            if triggers_fixed:
                break

        yield outcome
    finally:
        container.stop()
        container.remove()
