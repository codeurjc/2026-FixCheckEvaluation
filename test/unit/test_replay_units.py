"""
Fast unit tests for replay_fixcheck.py: which subjects each target replays,
where their records go, when a subject counts as done, and what a subject
process is told. No Docker, Ollama or network.

Run with:

    .venv/bin/python -m pytest test/unit/test_replay_units.py -v
"""

import json
import os

import pytest

import replay_fixcheck as replay

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse(*argv):
    return replay.build_parser().parse_args(list(argv))


# ------------------------------------------------------------ naming and layout

def test_oracle_label_is_the_ollama_model_or_the_generator():
    assert replay.oracle_label("ollama:gpt-oss:120b@1995") == "gpt-oss:120b"
    assert replay.oracle_label("previous-assertion") == "previous-assertion"


def test_each_target_writes_to_its_own_tree():
    assert replay.subject_output_dir("results", "plausible", "Lang", "12", model_dir="qwen3.6:35b") \
        == os.path.join("results", "qwen3.6:35b", "Lang", "Bug_12")
    assert replay.subject_output_dir("results", "devfix", "Lang", "12", oracle="gpt-oss:120b") \
        == os.path.join("results", "controls", "devfix", "gpt-oss:120b", "Lang", "Bug_12")
    assert replay.subject_output_dir("results", "defectrepairing", "Math", "Patch51",
                                     oracle="gpt-oss:120b", config="author") \
        == os.path.join("results", "controls", "defectrepairing", "author", "gpt-oss:120b",
                        "Math", "Patch51")
    with pytest.raises(ValueError):
        replay.subject_output_dir("results", "nonsense", "Lang", "12")


# -------------------------------------------------------------------- subjects

def _write_run(root, bug, result, diff="--- a/x\n+++ b/x\n"):
    bug_dir = root / "Lang" / f"Bug_{bug}"
    bug_dir.mkdir(parents=True)
    (bug_dir / "result.json").write_text(json.dumps(result))
    if diff is not None:
        (bug_dir / "fix.diff").write_text(diff)


PLAUSIBLE = {"applied": True, "compiled_after": True, "triggers_fixed": True, "fixed": True}


def test_plausible_subjects_are_the_replayable_patches_of_record(tmp_path):
    runs = tmp_path / "qwen3.6:35b"
    _write_run(runs, 1, PLAUSIBLE)
    _write_run(runs, 2, {**PLAUSIBLE, "triggers_fixed": False, "fixed": False})
    _write_run(runs, 3, {**PLAUSIBLE, "verdict_source": "job_log"})
    _write_run(runs, 4, {**PLAUSIBLE, "fixed": False})
    _write_run(runs, 10, PLAUSIBLE, diff=None)
    # A patch that applied but never compiled is not plausible, whatever it recorded.
    _write_run(runs, 11, {**PLAUSIBLE, "compiled_after": False})

    subjects, excluded = replay.plausible_subjects(str(tmp_path), "qwen3.6:35b", "Lang")

    assert [(s["subject"], s["patch_fixed"]) for s in subjects] == [("1", True), ("4", False)]
    reasons = {e["subject"]: e["reason"] for e in excluded}
    assert set(reasons) == {"3", "10"}
    assert "job_log" in reasons["3"] and "fix.diff" in reasons["10"]


def test_plausible_subjects_honour_a_bug_selection(tmp_path):
    runs = tmp_path / "m"
    _write_run(runs, 1, PLAUSIBLE)
    _write_run(runs, 4, PLAUSIBLE)
    subjects, _ = replay.plausible_subjects(str(tmp_path), "m", "Lang", bug_ids=["4"])
    assert [s["subject"] for s in subjects] == ["4"]


def _write_info(dataset, patch_id, project, bug_id, correctness="Incorrect"):
    info_dir = dataset / "tool" / "patches" / "INFO"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / f"{patch_id}.json").write_text(json.dumps({
        "ID": patch_id, "tool": "jGenProg", "correctness": correctness,
        "project": project, "bug_id": str(bug_id),
    }))


def test_defectrepairing_subjects_carry_their_label_in_natural_order(tmp_path):
    _write_info(tmp_path, "Patch10", "Math", 70, "Correct")
    _write_info(tmp_path, "Patch2", "Math", 5)
    _write_info(tmp_path, "Patch1", "Lang", 51)

    subjects, excluded = replay.defectrepairing_subjects(str(tmp_path), "Math")

    assert [(s["subject"], s["bug_id"], s["correctness"]) for s in subjects] == [
        ("Patch2", "5", "Incorrect"), ("Patch10", "70", "Correct"),
    ]
    assert excluded == []


def test_the_author_pass_reports_the_patches_he_excluded(tmp_path):
    _write_info(tmp_path, "Patch2", "Math", 5)
    _write_info(tmp_path, "Patch10", "Math", 70)
    configs = {"Patch2": {"target_test": "T", "target_test_methods": ["m"], "input_class": "int"}}

    subjects, excluded = replay.defectrepairing_subjects(str(tmp_path), "Math", author_configs=configs)

    assert [s["subject"] for s in subjects] == ["Patch2"]
    assert subjects[0]["author_config"]["input_class"] == "int"
    assert [e["subject"] for e in excluded] == ["Patch10"]


def test_load_author_configs_skips_rows_he_left_empty(tmp_path):
    csv_path = tmp_path / "subjects.csv"
    csv_path.write_text(
        "id,tool,correctness,project,bug,base_dir,main_dep,tests_build,tests_src_dir,"
        "target_test,target_test_methods,target_class,input_class\n"
        "Patch1,jGenprog,Incorrect,Chart,1,Chart1b,build,build-tests,tests,"
        "org.jfree.FooTests,test1:test2,org.jfree.Foo,java.lang.String\n"
        "Patch63,TBar,Correct,Closure,63,,,,,,,,\n"
    )
    configs = replay.load_author_configs(str(csv_path))
    assert set(configs) == {"Patch1"}
    assert configs["Patch1"]["target_test_methods"] == ["test1", "test2"]


@pytest.mark.skipif(not os.path.isfile(replay.AUTHOR_SUBJECTS_CSV),
                    reason="fixcheck/ not cloned (bash scripts/buildFixcheck.sh)")
def test_the_authors_own_subjects_csv_loads():
    configs = replay.load_author_configs()
    assert len(configs) > 150
    assert configs["Patch151"]["target_test"] == "org.apache.commons.lang.BooleanUtilsTest"
    assert configs["Patch151"]["target_test_methods"] == ["test_toBoolean_String"]


def test_java_package_reads_the_declaration():
    assert replay.java_package("/* x */\npackage org.apache.commons.lang;\nimport a.b;") \
        == "org.apache.commons.lang"
    assert replay.java_package("class Default {}") == ""


def test_author_adapted_tests_are_found_per_patch(tmp_path):
    patch_dir = tmp_path / "Patch151"
    patch_dir.mkdir()
    (patch_dir / "BooleanUtilsTest.java").write_text("package org.apache.commons.lang;\n")
    (patch_dir / "notes.txt").write_text("ignored")
    assert replay.author_adapted_tests("Patch151", str(tmp_path)) == [
        ("org.apache.commons.lang", "BooleanUtilsTest.java", str(patch_dir / "BooleanUtilsTest.java")),
    ]
    assert replay.author_adapted_tests("Patch1", str(tmp_path)) == []


# ------------------------------------------------------------ resume and records

def test_should_replay_follows_the_recorded_status(tmp_path):
    def record(status):
        (tmp_path / "fixcheck_v2.json").write_text(json.dumps({"replay_status": status}))

    assert replay.should_replay(str(tmp_path)) == (True, None)
    record("ok")
    assert replay.should_replay(str(tmp_path))[0] is False
    record("not_reproduced")
    assert replay.should_replay(str(tmp_path))[0] is False
    record("error")
    assert replay.should_replay(str(tmp_path))[0] is False
    assert replay.should_replay(str(tmp_path), retry_errored=True)[0] is True
    record("ok")
    assert replay.should_replay(str(tmp_path), resume=False)[0] is True


def test_a_replay_measured_off_the_gpu_becomes_an_error_to_redo(tmp_path):
    record = {"replay_status": "ok", "fixcheck": {"suspicious": True}}
    degraded = replay.mark_gpu_degraded(record, "gpt-oss:120b is resident with 62496 MiB outside the GPU")
    assert degraded["replay_status"] == replay.STATUS_ERROR and degraded["gpu_degraded"]
    assert degraded["status_before_gpu_check"] == "ok"
    assert degraded["fixcheck"] == record["fixcheck"]          # the measurement is kept, not trusted
    (tmp_path / "fixcheck_v2.json").write_text(json.dumps(degraded))
    assert replay.should_replay(str(tmp_path), retry_errored=True)[0] is True


def test_write_json_atomic_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "deep" / "fixcheck_v2.json"
    replay.write_json_atomic(str(path), {"replay_status": "ok"})
    assert json.loads(path.read_text()) == {"replay_status": "ok"}
    assert os.listdir(path.parent) == ["fixcheck_v2.json"]


# ----------------------------------------------------------------- arguments

def test_a_subject_process_gets_the_drivers_configuration():
    args = _parse("--target", "plausible", "--project", "Lang", "--model", "ollama/qwen3.6:35b",
                  "--fixcheck-assertions", "ollama:qwen3.6:35b@21000",
                  "--fixcheck-prefixes", "7", "--fixcheck-prefix-timeout", "30",
                  "--workdir", "/scratch/w", "--bug-id", "1-5")
    again = _parse(*replay.forward_args(args))
    for field in ("target", "project", "model", "config", "dataset", "results_root", "workdir",
                  "fixcheck_assertions", "fixcheck_prefixes", "fixcheck_similarity_threshold",
                  "fixcheck_timeout", "fixcheck_prefix_timeout", "fixcheck_llm_timeout"):
        assert getattr(again, field) == getattr(args, field), field


def test_validate_rejects_inconsistent_targets():
    assert replay.validate(_parse("--target", "plausible", "--project", "Lang",
                                  "--fixcheck-assertions", "previous-assertion"))
    assert replay.validate(_parse("--target", "devfix", "--project", "Lang", "--model", "ollama/x",
                                  "--fixcheck-assertions", "previous-assertion"))
    assert replay.validate(_parse("--target", "devfix", "--project", "Nope",
                                  "--fixcheck-assertions", "previous-assertion"))
    assert replay.validate(_parse("--target", "devfix", "--project", "Lang",
                                  "--fixcheck-assertions", "previous-assertion")) == []


def test_replay_job_derives_the_generator_from_its_oracle():
    """As project_job.sbatch does: never a hand-typed model or another job's port."""
    text = open(os.path.join(ROOT, "scripts", "replay_fixcheck_job.sbatch"), encoding="utf-8").read()
    assert 'FIXCHECK_ASSERTIONS="ollama:${ORACLE#ollama/}@${OLLAMA_PORT}"' in text
    assert "pkill" not in text


def test_plausible_replays_use_the_patch_model_as_oracle():
    text = open(os.path.join(ROOT, "scripts", "runFixcheckReplay.sh"), encoding="utf-8").read()
    assert 'ORACLE="$MODEL"' in text
