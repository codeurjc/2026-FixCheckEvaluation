"""
Fast, deterministic unit tests for the full-benchmark campaign's pure helpers:
bug enumeration and selection (``defects4j_bugs``), the shared runner helpers
and the per-bug timeout (``experiment_runner``). No Docker, GPU or network.

The bug-selection tests are the ones that earn their keep. Defects4J's ids are
not contiguous, and every deprecated id that slips through costs minutes of
container time before ``Experiment.py`` gives up on the checkout -- multiplied
by 854 bugs, a silent off-by-one here is hours of wasted GPU.

Run with:

    .venv/bin/python -m pytest test/unit/test_campaign_units.py -v
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from defects4j_bugs import (
    PROJECT_BUG_COUNTS,
    TOTAL_ACTIVE_BUGS,
    active_bug_ids,
    chunk,
    resolve_bug_ids,
)
from experiment_runner import (
    RunOutcome,
    clean_checkout,
    experiment_args,
    load_result,
    parse_bug_ids,
    result_dir,
    run_experiment,
)


# ------------------------------------------------------------- parse_bug_ids

@pytest.mark.parametrize("tokens,expected", [
    (["7"], ["7"]),
    (["1-5"], ["1", "2", "3", "4", "5"]),
    (["1-3", "5", "5"], ["1", "2", "3", "5"]),       # duplicates dropped, order kept
    (["1-3", "2"], ["1", "2", "3"]),
    # Commas are the wire format: `sbatch --export` cannot carry a space
    # without quoting gymnastics, so a selection travels as "1-5,8".
    (["1-3,8"], ["1", "2", "3", "8"]),
    (["1,2", "5-6"], ["1", "2", "5", "6"]),
])
def test_parse_bug_ids_expands_ranges_commas_and_spaces(tokens, expected):
    assert parse_bug_ids(tokens) == expected


# ------------------------------------------------------------ active_bug_ids

def _write_active_bugs(tmp_path, project, ids):
    project_dir = tmp_path / project
    project_dir.mkdir(parents=True)
    rows = ["bug.id,revision.id.buggy,revision.id.fixed,report.id,report.url"]
    rows += [f"{i},aaa,bbb,PROJ-{i},http://example/{i}" for i in ids]
    (project_dir / "active-bugs.csv").write_text("\n".join(rows) + "\n")
    return str(tmp_path)


def test_active_bug_ids_skips_the_header_and_keeps_gaps(tmp_path):
    # A real project's ids have holes where bugs were deprecated; the order and
    # the holes both have to survive.
    root = _write_active_bugs(tmp_path, "Lang", [1, 3, 4, 7])
    assert active_bug_ids("Lang", active_bugs_dir=root) == ["1", "3", "4", "7"]


def test_active_bug_ids_rejects_an_unknown_project():
    with pytest.raises(ValueError, match="unknown Defects4J project"):
        active_bug_ids("NotAProject")


def test_active_bug_ids_reads_the_real_vendored_checkout():
    """Every project resolves, and the totals match Defects4J's own README.

    This is the cross-check that a different Defects4J version would trip:
    getting 853 or 855 here means the campaign is not the one the plan sized.
    """
    total = 0
    for project, expected in PROJECT_BUG_COUNTS.items():
        ids = active_bug_ids(project)
        assert len(ids) == expected, f"{project}: {len(ids)} ids, expected {expected}"
        assert len(set(ids)) == len(ids), f"{project} has duplicate ids"
        total += len(ids)
    assert total == TOTAL_ACTIVE_BUGS == 854


def test_lang_deprecated_ids_are_absent():
    # LANG 2, 18, 25 and 48 no longer reproduce and are not in active-bugs.csv.
    ids = set(active_bug_ids("Lang"))
    assert {"2", "18", "25", "48"}.isdisjoint(ids)
    assert "1" in ids and "65" in ids


# ----------------------------------------------------------- resolve_bug_ids

def test_resolve_bug_ids_defaults_to_every_active_bug(tmp_path):
    root = _write_active_bugs(tmp_path, "Csv", [1, 2, 3])
    for tokens in (None, [], ["all"], ["ALL"]):
        assert resolve_bug_ids("Csv", tokens, active_bugs_dir=root) == ["1", "2", "3"]


def test_resolve_bug_ids_returns_defects4j_order_not_request_order(tmp_path):
    # A resumed run must walk the project the same way as the first attempt.
    root = _write_active_bugs(tmp_path, "Csv", [1, 2, 3, 4])
    assert resolve_bug_ids("Csv", ["4,1"], active_bugs_dir=root) == ["1", "4"]


def test_resolve_bug_ids_rejects_a_deprecated_id_by_name(tmp_path):
    root = _write_active_bugs(tmp_path, "Lang", [1, 3, 4])
    with pytest.raises(ValueError) as excinfo:
        resolve_bug_ids("Lang", ["1-4"], active_bugs_dir=root)
    # Naming the id is the point: the alternative is discovering it one failed
    # checkout at a time, minutes apart.
    assert "2" in str(excinfo.value)


# -------------------------------------------------------------------- chunk

def test_chunk_partitions_without_loss():
    ids = [str(i) for i in range(1, 175)]        # Closure's size
    groups = chunk(ids, 2)
    assert [len(g) for g in groups] == [87, 87]
    assert [b for g in groups for b in g] == ids


def test_chunk_never_emits_empty_groups():
    assert chunk(["1", "2", "3"], 5) == [["1"], ["2"], ["3"]]


def test_chunk_rejects_zero():
    with pytest.raises(ValueError):
        chunk(["1"], 0)


# ------------------------------------------------------- experiment_args etc.

class _Args:
    """Minimal stand-in for an argparse Namespace."""

    def __init__(self, **kwargs):
        defaults = dict(
            model=None, temperature=None, include_test_code=False,
            include_test_log=False, include_issue=False, fixcheck=False,
            fixcheck_prefixes=None, fixcheck_assertions=None,
            fixcheck_inputs_class=None, fixcheck_similarity_threshold=None,
        )
        defaults.update(kwargs)
        self.__dict__.update(defaults)


def test_experiment_args_forwards_only_what_was_set():
    # Unset options must not be forwarded, so Experiment.py keeps ownership of
    # every default and there is no second copy to drift.
    assert experiment_args(_Args()) == []
    assert experiment_args(_Args(model="ollama/gpt-oss:120b", fixcheck=True,
                                 fixcheck_prefixes=10)) == [
        "--model", "ollama/gpt-oss:120b", "--fixcheck", "--fixcheck-prefixes", "10",
    ]


def test_experiment_args_forwards_a_zero_temperature():
    # 0.0 is falsy but meaningful, so it must survive the `is not None` check.
    assert experiment_args(_Args(temperature=0.0)) == ["--temperature", "0.0"]


def test_result_dir_distinguishes_campaign_runs_from_iterations():
    assert result_dir("m", "Lang", "12") == os.path.join("results", "m", "Lang", "Bug_12")
    assert result_dir("m", "Lang", "12", 3) == os.path.join(
        "results", "m", "Lang", "Bug_12", "3"
    )


def test_load_result_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_result("m", "Lang", "12") is None
    target = tmp_path / "results" / "m" / "Lang" / "Bug_12"
    target.mkdir(parents=True)
    (target / "result.json").write_text(json.dumps({"fixed": True}))
    assert load_result("m", "Lang", "12") == {"fixed": True}


def test_load_result_survives_a_truncated_file(tmp_path, monkeypatch):
    # A job killed mid-write must not crash the next run's resume check.
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "results" / "m" / "Lang" / "Bug_12"
    target.mkdir(parents=True)
    (target / "result.json").write_text('{"fixed": tru')
    assert load_result("m", "Lang", "12") is None


def test_clean_checkout_removes_only_that_bugs_tree(tmp_path):
    (tmp_path / "Lang_12").mkdir()
    (tmp_path / "Lang_13").mkdir()
    clean_checkout(str(tmp_path), "Lang", "12")
    assert not (tmp_path / "Lang_12").exists()
    assert (tmp_path / "Lang_13").exists()
    clean_checkout(str(tmp_path), "Lang", "999")          # missing: no error


# -------------------------------------------------------------- run_experiment

def _stub_experiment(tmp_path, body):
    """Write a fake Experiment.py and point experiment_runner at it."""
    script = tmp_path / "fake_experiment.py"
    script.write_text(textwrap.dedent(body))
    return str(script)


def test_run_experiment_reports_a_clean_exit(tmp_path, monkeypatch):
    import experiment_runner

    monkeypatch.setattr(
        experiment_runner, "EXPERIMENT",
        _stub_experiment(tmp_path, "import sys; sys.exit(0)"),
    )
    outcome = run_experiment("Lang", "12", str(tmp_path), [], echo=False)
    assert outcome.status == "ok" and outcome.exit_code == 0 and outcome.ok


def test_run_experiment_reports_a_failure_without_raising(tmp_path, monkeypatch):
    # Experiment.py exits 1 when a checkout or compile fails, which is an
    # expected outcome for a fair number of Defects4J bugs; the campaign has to
    # record it and carry on rather than abort with hundreds of bugs left.
    import experiment_runner

    monkeypatch.setattr(
        experiment_runner, "EXPERIMENT",
        _stub_experiment(tmp_path, "import sys; sys.exit(1)"),
    )
    outcome = run_experiment("Lang", "12", str(tmp_path), [], echo=False)
    assert outcome.status == "error" and outcome.exit_code == 1 and not outcome.ok


def test_run_experiment_kills_a_hanging_run(tmp_path, monkeypatch):
    # The only bound on a run: llms/ollama_llm.py sets no client timeout, so a
    # wedged daemon would otherwise hang a SLURM job for its whole allocation.
    import experiment_runner

    monkeypatch.setattr(
        experiment_runner, "EXPERIMENT",
        _stub_experiment(tmp_path, "import time; time.sleep(300)"),
    )
    outcome = run_experiment(
        "Lang", "12", str(tmp_path), [], timeout=2, kill_grace=2, echo=False,
    )
    assert outcome.status == "timeout"
    assert outcome.seconds < 30, "the child was not killed promptly"


def test_run_experiment_writes_a_per_bug_log(tmp_path, monkeypatch):
    import experiment_runner

    monkeypatch.setattr(
        experiment_runner, "EXPERIMENT",
        _stub_experiment(tmp_path, "print('hello from the run')"),
    )
    log_path = str(tmp_path / "logs" / "Lang_12.log")
    run_experiment("Lang", "12", str(tmp_path), [], log_path=log_path, echo=False)
    assert "hello from the run" in open(log_path, encoding="utf-8").read()


def test_run_experiment_passes_the_forwarded_flags(tmp_path, monkeypatch):
    import experiment_runner

    monkeypatch.setattr(
        experiment_runner, "EXPERIMENT",
        _stub_experiment(tmp_path, "import sys; print(' '.join(sys.argv[1:]))"),
    )
    log_path = str(tmp_path / "argv.log")
    run_experiment(
        "Lang", "12", "/wd", ["--fixcheck", "--fixcheck-prefixes", "10"],
        log_path=log_path, echo=False,
    )
    argv = open(log_path, encoding="utf-8").read()
    assert "--project Lang --bug-id 12 --workdir /wd --fixcheck --fixcheck-prefixes 10" in argv
    assert "--iteration" not in argv, "the campaign runs one unnumbered run per bug"


def test_run_outcome_ok_only_for_status_ok():
    assert RunOutcome("ok", 0, 1.0).ok
    assert not RunOutcome("error", 1, 1.0).ok
    assert not RunOutcome("timeout", -1, 1.0).ok


# ----------------------------------------------------------- ollama_serve.sh

def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_ollama_serve_never_pkills():
    """The single-node cluster makes `pkill -f "ollama serve"` fatal.

    Every campaign job runs on the same node, so a pattern kill takes out the
    sibling jobs' daemons too. Guarding it here because the failure is silent:
    the victim job just starts producing empty results.
    """
    root = _repo_root()
    for name in ("scripts/ollama_serve.sh", "scripts/slurm_job.sbatch",
                 "scripts/project_job.sbatch"):
        lines = open(os.path.join(root, name), encoding="utf-8").read().splitlines()
        # Comments are allowed to name it -- they explain why it is gone.
        code = [line for line in lines if not line.lstrip().startswith("#")]
        assert not any("pkill" in line for line in code), \
            f"{name} still pattern-kills Ollama"


def test_ollama_serve_picks_distinct_free_ports():
    """Two jobs with different ids must not land on the same port."""
    root = _repo_root()
    script = f"""
        source {root}/scripts/ollama_serve.sh
        SLURM_JOB_ID=1000 _next_free_port $(( 21000 + (1000 % 900) ))
        SLURM_JOB_ID=1001 _next_free_port $(( 21000 + (1001 % 900) ))
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            timeout=60)
    ports = [int(p) for p in result.stdout.split()]
    assert len(ports) == 2 and ports[0] != ports[1]
    assert all(21000 <= p < 22000 for p in ports)


def test_project_job_derives_the_fixcheck_generator_from_the_model():
    """The two notations for the same model must not be typed twice.

    --model takes '<provider>/<model>' and --fixcheck-assertions takes
    'ollama:<model>@<port>'; deriving one from the other is what stops a job
    from pointing FixCheck at a different model or another job's daemon.
    """
    text = open(os.path.join(_repo_root(), "scripts/project_job.sbatch"),
                encoding="utf-8").read()
    assert 'FIXCHECK_ASSERTIONS="ollama:${MODEL#ollama/}@${OLLAMA_PORT}"' in text
