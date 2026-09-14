"""
replay_fixcheck.py — re-measure FixCheck on patches whose verdict is already recorded.

The campaigns' FixCheck results were dominated by artifacts of the tool and of
our integration (``scripts/fixcheck-patches/README.md``). Re-measuring them
must not regenerate the patches: the model is not deterministic, and a re-run
draws a different patch (docs/campaign.md, "The verdict of record"). So this
re-applies each patch as recorded and runs only FixCheck, writing
``fixcheck_v2.json`` next to -- never over -- ``result.json``.

Three kinds of subject (``--target``):

- ``plausible``: a model's plausible patches of record,
  ``results/<model>/<Project>/Bug_<id>/fix.diff``;
- ``devfix``: each bug's developer fix (``<id>b`` -> ``<id>f``), the control
  for false positives, under ``results/controls/devfix/<oracle>/``;
- ``defectrepairing``: the labelled patches FixCheck's author evaluated
  (``external/DefectRepairing``), under
  ``results/controls/defectrepairing/<config>/<oracle>/``, with his own
  hand-picked test and inputs-class (``--config author``) or with ours.

A subject is analysed only if its patch still applies, compiles and makes the
trigger tests pass; otherwise it is recorded as ``not_reproduced`` -- never
regenerated.

    python replay_fixcheck.py --target plausible --model ollama/qwen3.6:35b \\
        --project Lang --fixcheck-assertions ollama:qwen3.6:35b@11434
    python replay_fixcheck.py --target devfix --project Lang --bug-id 12 \\
        --fixcheck-assertions ollama:gpt-oss:120b@11434 --dry-run

As in run_project.py, each subject runs in a process of its own under a
timeout, so one hung subject cannot stall a whole job; ``--single`` is that
process. ``scripts/runFixcheckReplay.sh`` submits the SLURM jobs.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

from d4j.defects4j_bugs import PROJECTS, resolve_bug_ids
from experiment_runner import parse_bug_ids, reap_containers, run_command
from FixCheckWrapper import (
    DEFAULT_FIXCHECK_LLM_TIMEOUT,
    DEFAULT_FIXCHECK_PREFIX_TIMEOUT,
    DEFAULT_FIXCHECK_PREFIXES,
    DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD,
    DEFAULT_FIXCHECK_TIMEOUT,
    FIXCHECK_DIR,
    FIXCHECK_JAR,
    resolve_ollama_backend,
    validate_assertion_generator,
)
from summarize_campaign import FIXCHECK_REPLAY_FILE, collect_project

HERE = os.path.dirname(os.path.abspath(__file__))

TARGETS = ("plausible", "devfix", "defectrepairing")
CONFIGS = ("ours", "author")
DEFAULT_DATASET = os.path.join(HERE, "external", "DefectRepairing")
AUTHOR_SUBJECTS_CSV = os.path.join(FIXCHECK_DIR, "experiments", "defect-repairing-subjects.csv")
# Test sources FixCheck's author rewrote by hand for a few patches (e.g. Lang 51's
# test_toBoolean_String, whose calls he moved out of the assertEquals so that
# FixCheck had literals to mutate). His scripts do not copy them, but his CSV
# configuration for those patches only works with them.
AUTHOR_ADAPTED_TESTS_DIR = os.path.join(FIXCHECK_DIR, "experiments", "defects-repairing")
# Several FixCheck runs per subject, each bounded on its own (FixCheckWrapper):
# this only has to stop a subject that is stuck outside FixCheck.
DEFAULT_SUBJECT_TIMEOUT = 86400
DEFAULT_KILL_GRACE = 120

REPLAY_SCHEMA = "fixcheck-replay-v1"
STATUS_OK = "ok"                          # FixCheck ran on the reproduced patch
STATUS_NOT_REPRODUCED = "not_reproduced"  # the patch no longer applies/compiles/passes
STATUS_ERROR = "error"                    # the harness broke before a measurement
STATUS_TIMEOUT = "timeout"                # the subject process outlived --timeout
DONE_STATUSES = (STATUS_OK, STATUS_NOT_REPRODUCED)

_STOP_REQUESTED = False


# ------------------------------------------------------------------ subjects

def _natural_key(text):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def oracle_label(assertion_generator):
    """How a FixCheck oracle is named in paths: the Ollama model, or the generator."""
    backend = resolve_ollama_backend(assertion_generator)
    return backend.model if backend else assertion_generator


def subject_output_dir(results_root, target, project, subject, model_dir=None,
                       oracle=None, config=None):
    """Where a subject's ``fixcheck_v2.json`` and artifacts go.

    A model's patch keeps its replay next to its own ``result.json``; the
    controls get a tree of their own, one per oracle (and configuration), so
    that the two oracles' measurements of the same subject never collide.
    """
    if target == "plausible":
        return os.path.join(results_root, model_dir, project, f"Bug_{subject}")
    if target == "devfix":
        return os.path.join(results_root, "controls", "devfix", oracle, project, f"Bug_{subject}")
    if target == "defectrepairing":
        return os.path.join(results_root, "controls", "defectrepairing", config, oracle,
                            project, subject)
    raise ValueError(f"unknown target {target!r}")


def plausible_subjects(results_root, model_dir, project, bug_ids=None):
    """A model's plausible patches of record that can be re-applied.

    Plausible as ``summarize_campaign.collect_project`` reads it (compile guard
    included). A verdict reconstructed from a job log has no patch on disk, so
    it is reported instead of replayed.

    Returns ``(subjects, excluded)``.
    """
    wanted = set(bug_ids) if bug_ids else None
    subjects, excluded = [], []
    records = collect_project(os.path.join(results_root, model_dir), project)
    for record in sorted(records, key=lambda r: _natural_key(r["bug_id"])):
        bug = record["bug_id"]
        if wanted is not None and bug not in wanted:
            continue
        if not (record["has_result"] and record["triggers_fixed"]):
            continue
        if record.get("verdict_source") not in (None, "run"):
            excluded.append({"subject": bug, "reason": f"verdict reconstructed from "
                                                       f"{record['verdict_source']}: no patch on disk"})
            continue
        diff_path = os.path.join(results_root, model_dir, project, f"Bug_{bug}", "fix.diff")
        if not os.path.isfile(diff_path) or os.path.getsize(diff_path) == 0:
            excluded.append({"subject": bug, "reason": "no fix.diff on disk"})
            continue
        subjects.append({"subject": bug, "bug_id": bug, "patch_fixed": record["fixed"]})
    return subjects, excluded


def devfix_subjects(project, bug_ids=None, active_bugs_dir=None):
    """Every active bug of the project, each with its developer fix."""
    ids = resolve_bug_ids(project, bug_ids or None, active_bugs_dir)
    return [{"subject": bug, "bug_id": bug, "patch_fixed": True} for bug in ids], []


def load_author_configs(csv_path=AUTHOR_SUBJECTS_CSV):
    """FixCheck author's per-patch configuration for DefectRepairing.

    ``fixcheck/experiments/defect-repairing-subjects.csv`` names, for each
    patch, the test class, the ``:``-separated test methods and the
    inputs-class he chose by hand. Rows he left empty are subjects he excluded;
    they are absent from the result.
    """
    configs = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not (row.get("target_test") and row.get("target_test_methods") and row.get("input_class")):
                continue
            configs[row["id"]] = {
                "target_test": row["target_test"],
                "target_test_methods": row["target_test_methods"].split(":"),
                "input_class": row["input_class"],
            }
    return configs


def defectrepairing_subjects(dataset, project, patch_ids=None, author_configs=None):
    """The DefectRepairing patches of a project, with their correctness label.

    With ``author_configs`` (the ``--config author`` pass), a patch without a
    configuration is one the author excluded, and is reported as such.

    Returns ``(subjects, excluded)``.
    """
    info_dir = os.path.join(dataset, "tool", "patches", "INFO")
    wanted = set(patch_ids) if patch_ids else None
    subjects, excluded = [], []
    for name in sorted(os.listdir(info_dir), key=_natural_key):
        if not name.endswith(".json"):
            continue
        info = _read_json(os.path.join(info_dir, name)) or {}
        if info.get("project") != project:
            continue
        patch_id = info.get("ID") or name[:-len(".json")]
        if wanted is not None and patch_id not in wanted:
            continue
        subject = {
            "subject": patch_id,
            "bug_id": str(info.get("bug_id")),
            "correctness": info.get("correctness"),
            "tool": info.get("tool"),
        }
        if author_configs is not None:
            config = author_configs.get(patch_id)
            if config is None:
                excluded.append({"subject": patch_id, "reason": "excluded by FixCheck's author "
                                                                "(no configuration in his subjects CSV)"})
                continue
            subject["author_config"] = config
        subjects.append(subject)
    return subjects, excluded


def java_package(source):
    """The package a Java source file declares, or ``""``."""
    match = re.search(r"^\s*package\s+([\w.]+)\s*;", source, re.M)
    return match.group(1) if match else ""


def author_adapted_tests(patch_id, adapted_dir=AUTHOR_ADAPTED_TESTS_DIR):
    """The test sources FixCheck's author adapted for a patch, as ``(package, file, path)``."""
    directory = os.path.join(adapted_dir, patch_id)
    if not os.path.isdir(directory):
        return []
    adapted = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".java"):
            path = os.path.join(directory, name)
            with open(path, encoding="utf-8", errors="replace") as f:
                adapted.append((java_package(f.read()), name, path))
    return adapted


def enumerate_subjects(args, selection=None):
    """The subjects of ``args.project`` for ``args.target``, as ``(subjects, excluded)``."""
    tokens = selection if selection is not None else args.bug_id
    if args.target == "plausible":
        from Experiment import model_dir_name

        ids = parse_bug_ids(tokens) if tokens and tokens != ["all"] else None
        return plausible_subjects(args.results_root, model_dir_name(args.model), args.project, ids)
    if args.target == "devfix":
        return devfix_subjects(args.project, None if tokens in (None, ["all"]) else tokens,
                               args.active_bugs_dir)
    patch_ids = None
    if tokens and tokens != ["all"]:
        patch_ids = [t for token in tokens for t in token.replace(",", " ").split()]
    configs = load_author_configs() if args.config == "author" else None
    return defectrepairing_subjects(args.dataset, args.project, patch_ids, configs)


def output_dir_for(args, subject):
    model_dir = None
    if args.target == "plausible":
        from Experiment import model_dir_name

        model_dir = model_dir_name(args.model)
    return subject_output_dir(
        args.results_root, args.target, args.project, subject, model_dir=model_dir,
        oracle=oracle_label(args.fixcheck_assertions), config=args.config,
    )


def should_replay(output_dir, resume=True, retry_errored=False):
    """Whether a subject still needs a replay, and why not if it doesn't.

    A subject measured or found not reproducible is done. One the harness
    broke (``error``/``timeout``) is retried only with ``retry_errored``, as in
    run_project.py.
    """
    if not resume:
        return True, None
    record = _read_json(os.path.join(output_dir, FIXCHECK_REPLAY_FILE))
    if record is None:
        return True, None
    status = record.get("replay_status")
    if status in DONE_STATUSES:
        return False, f"already replayed ({status})"
    if retry_errored:
        return True, None
    return False, f"previously {status} (use --retry-errored to redo)"


def write_json_atomic(path, payload):
    """Write JSON so a killed process never leaves a truncated file behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _git_sha(path=HERE):
    try:
        return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _file_sha256(path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _now():
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------ one subject

def triggers_still_failing(container, workdir, trigger_tests):
    """The trigger tests that do not pass on the checkout as it stands."""
    from docker_utils import exec_in_container
    from Experiment import parse_failing_test_names, parse_failing_tests

    failing = []
    for trigger in trigger_tests:
        output = exec_in_container(container, f"defects4j test -t {trigger}", workdir=workdir).output
        # No "Failing tests:" line at all means the run did not get that far.
        if parse_failing_tests(output) != 0 or trigger in parse_failing_test_names(output):
            failing.append(trigger)
    return failing


def author_runs(container, workdir, config, prefixes):
    """The one FixCheck run FixCheck's author configured for a DefectRepairing patch.

    Test class, methods and inputs-class come from his subjects CSV. His
    failure trace is the buggy version's whole ``failing_tests`` file, from a
    full ``defects4j test`` (``setup-defect-repairing.py``), which FixCheck
    cuts at the first reflective frame; without the Defects4J header lines it
    is the same trace. Must run on the buggy checkout, before the patch.
    """
    from docker_utils import run_step
    from FixCheckWrapper import strip_defects4j_headers

    run_step(container, "defects4j test", workdir,
             description="Running the buggy test suite (the author's failure trace)")
    try:
        with open(os.path.join(workdir, "failing_tests"), encoding="utf-8", errors="replace") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    path = os.path.join(workdir, ".fixcheck", "traces", "author.failing_tests")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(strip_defects4j_headers(content))
    methods = config["target_test_methods"]
    runs = [{
        "test_class": config["target_test"],
        "method": "+".join(methods),
        "test_methods": methods,
        "inputs_class": config["input_class"],
        "num_prefixes": prefixes,
        "literal_counts": None,
        "failure_log_path": path,
    }]
    return runs, []


def replay_subject(args, subject):
    """Re-apply one subject's patch and run FixCheck on it; always writes its record."""
    import docker

    from d4j.developer_fix import developer_diff
    from docker_utils import exec_in_container, export_property, run_step
    from Experiment import (
        apply_diff,
        extract_trigger_method_sources_by_class,
        get_trigger_tests,
        locate_source_files,
        locate_test_files,
        read_sources,
        run_trigger_tests_raw,
        start_container,
    )
    from FixCheckWrapper import (
        FixCheckWrapper,
        copy_fixcheck_artifacts,
        needs_host_network,
        write_fixcheck_failure_logs,
    )

    project, bug, name = args.project, subject["bug_id"], subject["subject"]
    out_dir = output_dir_for(args, name)
    record = {
        "schema": REPLAY_SCHEMA,
        "target": args.target,
        "config": args.config if args.target == "defectrepairing" else None,
        "project": project,
        "subject": name,
        "bug_id": bug,
        "model": args.model if args.target == "plausible" else None,
        "oracle": oracle_label(args.fixcheck_assertions),
        "assertion_generator": args.fixcheck_assertions,
        "correctness": subject.get("correctness"),
        "tool": subject.get("tool"),
        "patch_fixed": subject.get("patch_fixed"),
        "git_sha": _git_sha(),
        "fixcheck_jar_sha256": _file_sha256(FIXCHECK_JAR),
        "hostname": socket.gethostname(),
        "slurm_job_id": os.getenv("SLURM_JOB_ID"),
        "started_at": _now(),
        "replay_status": None,
        "reason": None,
        "fixcheck": None,
    }

    def conclude(status, reason=None):
        record["replay_status"], record["reason"] = status, reason
        print(f"[replay] {project} {name}: {status}" + (f" -- {reason}" if reason else ""), flush=True)
        return record

    mount_dir = os.path.abspath(args.workdir)
    # DefectRepairing's patches name their files as <Project><id>b/..., relative
    # to the directory that holds the checkout: the author's layout.
    subject_root = os.path.join(mount_dir, f"{project}_{name}")
    workdir = os.path.join(subject_root, f"{project}{bug}b")
    fixed_workdir = os.path.join(subject_root, f"{project}{bug}f")
    shutil.rmtree(subject_root, ignore_errors=True)
    os.makedirs(subject_root)
    # Artifacts of an earlier attempt must not mix with this one's.
    shutil.rmtree(os.path.join(out_dir, "fixcheck_v2"), ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    started = time.time()
    container = None
    try:
        container = start_container(
            docker.from_env(), mount_dir,
            extra_mounts={FIXCHECK_DIR: {"bind": FIXCHECK_DIR, "mode": "ro"}},
            network_mode="host" if needs_host_network(args.fixcheck_assertions) else None,
        )
        if not run_step(container, f"defects4j checkout -p {project} -v {bug}b -w {workdir}",
                        workdir=None, description=f"Checking out {project} {bug}b").ok:
            return conclude(STATUS_ERROR, "buggy checkout failed")
        if args.target == "defectrepairing" and args.config == "author":
            adapted = author_adapted_tests(name)
            if adapted:
                tests_dir = os.path.join(workdir, export_property(container, workdir, "dir.src.tests"))
                for package, file_name, path in adapted:
                    shutil.copy(path, os.path.join(tests_dir, *package.split("."), file_name)
                                if package else os.path.join(tests_dir, file_name))
                record["author_adapted_tests"] = [file_name for _, file_name, _ in adapted]
        if not run_step(container, "defects4j compile", workdir,
                        description="Compiling buggy sources").ok:
            return conclude(STATUS_ERROR, "buggy sources do not compile")

        trigger_tests = get_trigger_tests(container, workdir)
        if not trigger_tests:
            return conclude(STATUS_ERROR, "no trigger tests")
        test_files = locate_test_files(container, workdir, sorted({t.split("::")[0] for t in trigger_tests}))
        method_sources = extract_trigger_method_sources_by_class(trigger_tests, read_sources(test_files))
        # The pre-fix traces, before the patch overwrites Defects4J's failing_tests.
        write_fixcheck_failure_logs(workdir, trigger_tests,
                                    run_trigger_tests_raw(container, workdir, trigger_tests))
        runs = skipped = None
        if args.target == "defectrepairing" and args.config == "author":
            runs, skipped = author_runs(container, workdir, subject["author_config"],
                                        args.fixcheck_prefixes)

        if args.target == "defectrepairing":
            patch_source = os.path.join(args.dataset, "tool", "patches", name)
            patch_copy = os.path.join(subject_root, "patch.diff")
            shutil.copy(patch_source, patch_copy)
            with open(patch_source, encoding="utf-8", errors="replace") as f:
                patch_text = f.read()
            applied_run = exec_in_container(
                container, f"patch -d {subject_root} -u -p0 -i {patch_copy}", workdir=None
            )
            applied, apply_log = applied_run.ok, applied_run.output
        else:
            if args.target == "plausible":
                from Experiment import model_dir_name

                diff_path = os.path.join(args.results_root, model_dir_name(args.model), project,
                                         f"Bug_{bug}", "fix.diff")
                with open(diff_path, encoding="utf-8") as f:
                    patch_text = f.read()
            else:
                if not run_step(container, f"defects4j checkout -p {project} -v {bug}f -w {fixed_workdir}",
                                workdir=None, description=f"Checking out {project} {bug}f").ok:
                    return conclude(STATUS_ERROR, "fixed checkout failed")
                files = locate_source_files(container, workdir)
                patch_text = developer_diff(
                    read_sources(files),
                    read_sources([(rel, os.path.join(fixed_workdir, rel)) for rel, _ in files]),
                )
                if not patch_text.strip():
                    return conclude(STATUS_ERROR, "the developer fix yields an empty diff")
            applied, apply_log = apply_diff(container, workdir, patch_text)

        with open(os.path.join(out_dir, "replay_patch.diff"), "w", encoding="utf-8") as f:
            f.write(patch_text)
        with open(os.path.join(out_dir, "replay_apply.log"), "w", encoding="utf-8") as f:
            f.write(apply_log)
        if not applied:
            return conclude(STATUS_NOT_REPRODUCED, "the patch no longer applies")
        if not run_step(container, "defects4j compile", workdir,
                        description="Compiling patched sources").ok:
            return conclude(STATUS_NOT_REPRODUCED, "the patched sources do not compile")
        still_failing = triggers_still_failing(container, workdir, trigger_tests)
        if still_failing:
            return conclude(STATUS_NOT_REPRODUCED, f"trigger tests still failing: {still_failing}")

        wrapper = FixCheckWrapper(
            num_prefixes=args.fixcheck_prefixes,
            assertion_generator=args.fixcheck_assertions,
            similarity_threshold=args.fixcheck_similarity_threshold,
            timeout_seconds=args.fixcheck_timeout,
            prefix_timeout_seconds=args.fixcheck_prefix_timeout,
            llm_timeout_seconds=args.fixcheck_llm_timeout,
        )
        # Seeded by the bug, not by the patch: every patch of one bug -- both
        # models', the developer's, DefectRepairing's -- meets the same mutations.
        result = wrapper.run(container, workdir, trigger_tests, method_sources,
                             subject_id=f"{project}-{bug}", runs=runs, skipped=skipped)
        copy_fixcheck_artifacts(result, os.path.join(out_dir, "fixcheck_v2"))
        record["fixcheck"] = result
        if not result.get("ok"):
            return conclude(STATUS_ERROR, result.get("error"))
        return conclude(STATUS_OK)
    except Exception as exc:
        return conclude(STATUS_ERROR, f"{type(exc).__name__}: {exc}")
    finally:
        if record["replay_status"] is None:
            record["replay_status"], record["reason"] = STATUS_ERROR, "interrupted"
        record["seconds"] = round(time.time() - started, 1)
        record["finished_at"] = _now()
        if container is not None:
            try:
                container.stop()
                container.remove()
            except Exception as exc:
                print(f"[replay] WARNING: could not remove the container: {exc}", flush=True)
        shutil.rmtree(subject_root, ignore_errors=True)
        write_json_atomic(os.path.join(out_dir, FIXCHECK_REPLAY_FILE), record)


# ------------------------------------------------------------------- driver

def forward_args(args):
    """The flags a ``--single`` subject process needs, from the driver's args."""
    forwarded = [
        "--target", args.target,
        "--project", args.project,
        "--config", args.config,
        "--dataset", args.dataset,
        "--results-root", args.results_root,
        "--workdir", args.workdir,
        "--fixcheck-assertions", args.fixcheck_assertions,
        "--fixcheck-prefixes", str(args.fixcheck_prefixes),
        "--fixcheck-similarity-threshold", str(args.fixcheck_similarity_threshold),
        "--fixcheck-timeout", str(args.fixcheck_timeout),
        "--fixcheck-prefix-timeout", str(args.fixcheck_prefix_timeout),
        "--fixcheck-llm-timeout", str(args.fixcheck_llm_timeout),
    ]
    if args.model:
        forwarded += ["--model", args.model]
    if args.active_bugs_dir:
        forwarded += ["--active-bugs-dir", args.active_bugs_dir]
    return forwarded


def validate(args):
    """Problems with the arguments themselves, before anything runs."""
    problems = []
    if args.project not in PROJECTS:
        problems.append(f"unknown project {args.project!r}; expected one of {', '.join(PROJECTS)}")
    if args.target == "plausible" and not args.model:
        problems.append("--target plausible needs --model: whose patches of record to replay")
    if args.target != "plausible" and args.model:
        problems.append("--model only applies to --target plausible")
    if args.target == "defectrepairing" and not os.path.isdir(
            os.path.join(args.dataset, "tool", "patches", "INFO")):
        problems.append(f"DefectRepairing not found at {args.dataset}; clone "
                        "https://github.com/Ultimanecat/DefectRepairing there")
    if args.target == "defectrepairing" and args.config == "author" and not os.path.isfile(AUTHOR_SUBJECTS_CSV):
        problems.append(f"the author's subjects CSV is missing at {AUTHOR_SUBJECTS_CSV}; "
                        "run bash scripts/buildFixcheck.sh to clone fixcheck/")
    return problems


def preflight(args):
    """Fail fast, before any GPU time is spent."""
    problems = []
    if not os.path.isfile(FIXCHECK_JAR):
        problems.append(f"the FixCheck jar is missing at {FIXCHECK_JAR}; "
                        "build it with: bash scripts/buildFixcheck.sh")
    backend = resolve_ollama_backend(args.fixcheck_assertions)
    if backend is not None:
        try:
            with urllib.request.urlopen(f"{backend.base_url}/api/tags", timeout=10) as resp:
                tags = [m.get("name", "") for m in json.load(resp).get("models", [])]
            if backend.wanted_tag not in tags:
                problems.append(f"the Ollama daemon at {backend.base_url} does not serve "
                                f"{backend.wanted_tag!r}; it has {tags}")
        except Exception as exc:
            problems.append(f"no Ollama daemon answering at {backend.base_url}: {exc}")
    try:
        import docker

        docker.from_env().ping()
    except Exception as exc:
        problems.append(f"the Docker daemon is not reachable: {exc}")
    return problems


def gpu_problem(assertion_generator):
    """Why the oracle model is not fully in VRAM, or ``None`` (see run_project.py)."""
    backend = resolve_ollama_backend(assertion_generator)
    if backend is None:
        return None
    from run_project import gpu_placement_problem

    try:
        with urllib.request.urlopen(f"{backend.base_url}/api/ps", timeout=10) as resp:
            return gpu_placement_problem(json.load(resp), f"ollama/{backend.model}")
    except Exception:
        return None


def write_manifest(args, subjects, excluded, log_dir):
    """What produced these measurements; results/ and scripts/logs/ are gitignored."""
    manifest = {
        "target": args.target,
        "project": args.project,
        "config": args.config,
        "model": args.model,
        "oracle": oracle_label(args.fixcheck_assertions),
        "assertion_generator": args.fixcheck_assertions,
        "fixcheck_prefixes": args.fixcheck_prefixes,
        "fixcheck_similarity_threshold": args.fixcheck_similarity_threshold,
        "fixcheck_timeout": args.fixcheck_timeout,
        "fixcheck_prefix_timeout": args.fixcheck_prefix_timeout,
        "fixcheck_llm_timeout": args.fixcheck_llm_timeout,
        "subjects": [s["subject"] for s in subjects],
        "excluded": excluded,
        "argv": sys.argv,
        "git_sha": _git_sha(),
        "fixcheck_jar_sha256": _file_sha256(FIXCHECK_JAR),
        "dataset_git_sha": _git_sha(args.dataset) if args.target == "defectrepairing" else None,
        "hostname": socket.gethostname(),
        "slurm_job_id": os.getenv("SLURM_JOB_ID"),
        "started_at": _now(),
    }
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def _install_signal_handlers():
    """Stop after the current subject on SIGTERM/SIGINT; die on the second."""
    def handler(signum, _frame):
        global _STOP_REQUESTED
        if _STOP_REQUESTED:
            sys.exit(143)
        _STOP_REQUESTED = True
        print(f"\n[replay] Signal {signum}: finishing the current subject, then stopping.", flush=True)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Re-measure FixCheck on patches of record, without regenerating them.",
    )
    parser.add_argument("--target", choices=TARGETS, required=True,
                        help="plausible: a model's plausible patches; devfix: developer fixes; "
                             "defectrepairing: FixCheck author's labelled patches.")
    parser.add_argument("--project", required=True, help="Defects4J project (e.g. Lang).")
    parser.add_argument("--bug-id", nargs="+", default=None, metavar="ID",
                        help="Subject selection (default: all). Bug ids and ranges for "
                             "plausible/devfix (e.g. 1,3-5); patch ids for defectrepairing "
                             "(e.g. Patch151).")
    parser.add_argument("--model", default=None,
                        help="--target plausible: the model whose patches to replay "
                             "(e.g. ollama/qwen3.6:35b).")
    parser.add_argument("--config", choices=CONFIGS, default="ours",
                        help="--target defectrepairing: 'author' uses his test and "
                             "inputs-class per patch; 'ours' our automatic choice (default).")
    parser.add_argument("--dataset", default=DEFAULT_DATASET,
                        help="DefectRepairing checkout (default: external/DefectRepairing).")
    parser.add_argument("--results-root", default="results", help="Results tree (default: results).")
    parser.add_argument("--workdir", default=os.path.join("workspace", "replay"),
                        help="Host directory for checkouts (default: workspace/replay).")
    parser.add_argument("--log-dir", default=None,
                        help="Per-subject logs, status and manifest "
                             "(default: logs/replay/<target>/<Project>).")
    parser.add_argument("--timeout", type=int, default=DEFAULT_SUBJECT_TIMEOUT,
                        help=f"Wall-clock limit per subject in seconds (default: {DEFAULT_SUBJECT_TIMEOUT}).")
    parser.add_argument("--kill-grace", type=int, default=DEFAULT_KILL_GRACE,
                        help=f"Seconds between SIGTERM and SIGKILL on timeout (default: {DEFAULT_KILL_GRACE}).")
    parser.add_argument("--resume", action="store_true", default=True,
                        help="Skip subjects already replayed (default).")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        help="Replay every selected subject again (the previous record is kept "
                             "as fixcheck_v2.json.previous).")
    parser.add_argument("--retry-errored", action="store_true",
                        help="With --resume, also retry subjects whose replay errored or timed out.")
    parser.add_argument("--active-bugs-dir", default=None,
                        help="Directory holding <Project>/active-bugs.csv (see run_project.py).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the subjects and exit.")
    parser.add_argument("--single", default=None, metavar="SUBJECT", help=argparse.SUPPRESS)
    parser.add_argument("--fixcheck-assertions", required=True, type=validate_assertion_generator,
                        metavar="GENERATOR",
                        help="FixCheck's assertion generator, e.g. ollama:gpt-oss:120b@11434. "
                             "For --target plausible, the model that wrote the patches.")
    parser.add_argument("--fixcheck-prefixes", type=int, default=DEFAULT_FIXCHECK_PREFIXES,
                        help=f"Prefixes per trigger method (default: {DEFAULT_FIXCHECK_PREFIXES}).")
    parser.add_argument("--fixcheck-similarity-threshold", type=float,
                        default=DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD,
                        help=f"Suspicious-verdict threshold (default: {DEFAULT_FIXCHECK_SIMILARITY_THRESHOLD}).")
    parser.add_argument("--fixcheck-timeout", type=int, default=DEFAULT_FIXCHECK_TIMEOUT,
                        help=f"Budget per FixCheck run in seconds (default: {DEFAULT_FIXCHECK_TIMEOUT}).")
    parser.add_argument("--fixcheck-prefix-timeout", type=int, default=DEFAULT_FIXCHECK_PREFIX_TIMEOUT,
                        help=f"Budget per prefix in seconds (default: {DEFAULT_FIXCHECK_PREFIX_TIMEOUT}).")
    parser.add_argument("--fixcheck-llm-timeout", type=int, default=DEFAULT_FIXCHECK_LLM_TIMEOUT,
                        help=f"Timeout per model call in seconds (default: {DEFAULT_FIXCHECK_LLM_TIMEOUT}).")
    return parser


def run_single(args):
    """``--single``: replay one subject in this process."""
    # A timeout reaches this process as SIGTERM; exiting through SystemExit runs
    # replay_subject's finally, which removes the container and writes the record.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(143))
    subjects, excluded = enumerate_subjects(args, selection=[args.single])
    match = next((s for s in subjects if s["subject"] == args.single), None)
    if match is None:
        reasons = [e["reason"] for e in excluded if e["subject"] == args.single]
        print(f"[replay] {args.project} {args.single} is not a {args.target} subject"
              + (f": {reasons[0]}" if reasons else ""), file=sys.stderr)
        return 2
    record = replay_subject(args, match)
    return 0 if record["replay_status"] in DONE_STATUSES else 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    problems = validate(args)
    if problems:
        sys.exit("[replay] " + "\n[replay] ".join(problems))
    if args.single:
        return run_single(args)

    subjects, excluded = enumerate_subjects(args)
    log_dir = args.log_dir or os.path.join("logs", "replay", args.target, args.project)
    print(f"[replay] {args.target} {args.project}: {len(subjects)} subject(s), "
          f"{len(excluded)} excluded; oracle {oracle_label(args.fixcheck_assertions)}", flush=True)
    for item in excluded:
        print(f"[replay]   excluded {item['subject']}: {item['reason']}")
    if args.dry_run:
        for subject in subjects:
            out_dir = output_dir_for(args, subject["subject"])
            run, why = should_replay(out_dir, args.resume, args.retry_errored)
            print(f"  {subject['subject']:>12}  -> {out_dir}" + ("" if run else f"  (skip: {why})"))
        return 0

    problems = preflight(args)
    if problems:
        sys.exit("[replay] preflight failed:\n  - " + "\n  - ".join(problems))
    write_manifest(args, subjects, excluded, log_dir)
    _install_signal_handlers()

    statuses, skipped = [], 0
    for index, subject in enumerate(subjects, start=1):
        if _STOP_REQUESTED:
            break
        name = subject["subject"]
        out_dir = output_dir_for(args, name)
        run, why = should_replay(out_dir, args.resume, args.retry_errored)
        if not run:
            skipped += 1
            print(f"[replay] {args.project} {name}: skipped ({why})", flush=True)
            continue
        problem = gpu_problem(args.fixcheck_assertions)
        if problem:
            print(f"[replay] ABORT: {problem}. Resubmit the job; the remaining subjects "
                  "resume where this one stopped.", file=sys.stderr, flush=True)
            return 3

        record_path = os.path.join(out_dir, FIXCHECK_REPLAY_FILE)
        if os.path.exists(record_path):
            os.replace(record_path, record_path + ".previous")
        print(f"\n[replay] {args.project} {name} ({index}/{len(subjects)})", flush=True)
        cmd = [sys.executable, os.path.abspath(__file__), *forward_args(args), "--single", name]
        log_path = os.path.join(log_dir, "subjects", f"{args.project}_{name}.log")
        outcome = run_command(cmd, timeout=args.timeout, log_path=log_path,
                              kill_grace=args.kill_grace)
        record = _read_json(record_path)
        if outcome.status == "timeout" or record is None:
            status = STATUS_TIMEOUT if outcome.status == "timeout" else STATUS_ERROR
            record = {**(record or {}), "schema": REPLAY_SCHEMA, "target": args.target,
                      "project": args.project, "subject": name, "bug_id": subject["bug_id"],
                      "replay_status": status,
                      "reason": f"subject process {outcome.status} (exit {outcome.exit_code})"}
            write_json_atomic(record_path, record)
        if outcome.status != "ok":
            reap_containers(args.workdir)

        fixcheck = record.get("fixcheck") or {}
        status_line = {
            "project": args.project, "subject": name, "status": record.get("replay_status"),
            "reason": record.get("reason"), "seconds": outcome.seconds,
            "analyzed_runs": fixcheck.get("analyzed_runs"), "suspicious": fixcheck.get("suspicious"),
            "log": log_path, "finished_at": _now(),
        }
        statuses.append(status_line)
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "status.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(status_line) + "\n")
        print(f"[replay] {args.project} {name}: {status_line['status']} in {outcome.seconds}s "
              f"(analyzed_runs={status_line['analyzed_runs']}, "
              f"suspicious={status_line['suspicious']})", flush=True)

    counts = {}
    for status in statuses:
        counts[status["status"]] = counts.get(status["status"], 0) + 1
    summary = {"target": args.target, "project": args.project, "replayed": len(statuses),
               "skipped": skipped, "by_status": counts, "finished_at": _now(), "subjects": statuses}
    with open(os.path.join(log_dir, "replay_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\n[replay] {args.target} {args.project}: {len(statuses)} replayed, "
          f"{skipped} skipped, {counts}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
