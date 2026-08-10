"""
Shared machinery for the integration tests that run FixCheck against a
Defects4J bug's *developer* fix.

Both ``test_fixcheck_devfix.py`` (does a correct fix survive the check?) and
``test_fixcheck_ollama_generator.py`` (does the configurable Ollama generator
actually drive a run?) need the same expensive preamble: check out ``<id>b``
and ``<id>f``, diff them into the real developer patch, apply it, prove it
fixes the trigger tests, and only then hand the patched checkout to FixCheck.
It lives here so neither test owns it and the two cannot drift apart.

Not a ``conftest.py``: this exports plain callables rather than fixtures, since
each test module parametrizes and scopes its own fixture differently.
"""

import contextlib
import difflib
import json
import os
import shutil

from Experiment import (
    DEFECTS4J_IMAGE,
    FIXCHECK_DIR,
    apply_diff,
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
    FixCheckWrapper,
    fixcheck_failure_log_path,
    group_triggers_by_class,
    needs_host_network,
    write_fixcheck_failure_logs,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGS_DIR = os.path.join(_REPO_ROOT, "logs", "test")


def docker_image_available():
    """True when the Docker daemon is reachable and the image is present."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        tags = [tag for image in client.images.list() for tag in image.tags]
        return DEFECTS4J_IMAGE in tags
    except Exception:
        return False


def developer_diff(buggy_sources, fixed_sources):
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


def collect_logs(log_dir, workdir, result, trigger_tests, dev_diff):
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


@contextlib.contextmanager
def fixcheck_on_developer_fix(project, bug_id, assertion_generator, mount_dir,
                              log_dir, num_prefixes, similarity_threshold):
    """Apply a bug's developer fix and run FixCheck against it.

    Checks out both the buggy (``<id>b``) and fixed (``<id>f``) revisions, builds
    a diff between them (the real developer patch, not an LLM guess), applies
    it to the buggy checkout, confirms it actually fixes the trigger tests
    (a precondition for the caller's premise, not something FixCheck-related),
    and then runs FixCheck on it exactly as ``Experiment.py`` would with
    ``--fixcheck``.

    Yields ``(result, workdir)``: FixCheck's result dict, already written to
    ``log_dir`` along with the rest of the run's artifacts, and the patched
    checkout it was produced from. The container is always removed afterwards.

    Failures of the preamble are raised as ``AssertionError`` -- they mean the
    harness broke, not that FixCheck reached a verdict.
    """
    import docker

    workdir_b = os.path.join(mount_dir, f"{project}_{bug_id}")
    workdir_f = os.path.join(mount_dir, f"{project}_{bug_id}_fixed")

    # Start from a clean slate so a stale directory can never be mistaken for
    # this run's output.
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
        dev_diff = developer_diff(buggy_sources, fixed_sources)
        assert dev_diff.strip(), "developer fix produced an empty diff"

        applied, apply_log = apply_diff(container, workdir_b, dev_diff)
        assert applied, f"developer diff failed to apply:\n{apply_log}"

        # 5. Compile + test post-fix, and confirm the developer fix actually
        #    fixes the trigger tests -- a precondition for the caller's
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
        triggers_fixed, _new_failures, _fixed = evaluate_fix(
            trigger_tests, failing_before_names, failing_after_names, applied
        )
        assert triggers_fixed, (
            "developer fix did not make the trigger tests pass -- "
            f"still failing: {sorted(set(trigger_tests) & set(failing_after_names))}"
        )

        # 6. Run FixCheck exactly as Experiment.py would with --fixcheck.
        fixcheck = FixCheckWrapper(
            num_prefixes=num_prefixes,
            assertion_generator=assertion_generator,
            similarity_threshold=similarity_threshold,
        )
        result = fixcheck.run(container, workdir_b, trigger_tests, trigger_method_sources)
        result["project"], result["bug_id"] = project, bug_id
        print(f"\n===== FixCheck result ({project} {bug_id}, {assertion_generator}, "
              "developer fix) =====\n")
        print(result)
        print("\n===== End of FixCheck result =====\n")

        # 7. Copy the artifacts out before the tmp checkout goes away. Done
        #    before the caller can skip, so a "not a FixCheck subject" verdict
        #    is just as inspectable as a real result.
        collect_logs(log_dir, workdir_b, result, trigger_tests, dev_diff)
        print(f"[test] FixCheck artifacts kept in: {log_dir}")

        yield result, workdir_b
    finally:
        container.stop()
        container.remove()
