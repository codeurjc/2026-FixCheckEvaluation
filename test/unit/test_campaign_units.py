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
import re
import subprocess
import sys
import textwrap

import pytest

from d4j.defects4j_bugs import (
    ISSUE_AVAILABLE,
    ISSUE_EMPTY,
    ISSUE_UNCACHED,
    ISSUE_UNUSABLE,
    is_unusable_tracker,
    issue_text,
    PROJECT_BUG_COUNTS,
    TOTAL_ACTIVE_BUGS,
    active_bug_ids,
    chunk,
    issue_path,
    load_cached_issue,
    strip_issue_comments,
    report_urls,
    resolve_bug_ids,
)
from d4j.fetch_issues import classify, format_googlecode, googlecode_json_url
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


# --------------------------------------------------- issue reports and cache

def test_report_urls_marks_unknown_as_no_issue(tmp_path):
    """Defects4J writes the literal UNKNOWN when it has no issue URL.

    18 Chart bugs are like that. Returning ``None`` rather than the string lets
    the fetcher record "no issue exists" instead of trying to GET "UNKNOWN"
    and calling it a failure.
    """
    project_dir = tmp_path / "Chart"
    project_dir.mkdir(parents=True)
    (project_dir / "active-bugs.csv").write_text(
        "bug.id,revision.id.buggy,revision.id.fixed,report.id,report.url\n"
        "1,a,b,R-1,https://example.org/1\n"
        "3,a,b,UNKNOWN,UNKNOWN\n"
    )
    urls = report_urls("Chart", active_bugs_dir=str(tmp_path))
    assert urls == {"1": "https://example.org/1", "3": None}


def test_report_urls_cover_every_real_bug():
    """Every one of the 854 bugs resolves to a URL or an explicit None."""
    total = 0
    for project in PROJECT_BUG_COUNTS:
        urls = report_urls(project)
        ids = active_bug_ids(project)
        assert set(urls) == set(ids), f"{project}: url map and id list disagree"
        total += len(urls)
    assert total == TOTAL_ACTIVE_BUGS


@pytest.mark.parametrize("url,expected", [
    ("https://issues.apache.org/jira/browse/LANG-747", "jira"),
    ("https://github.com/FasterXML/jackson-dataformat-xml/issues/180", "github"),
    ("https://storage.googleapis.com/google-code-archive/v2/code.google.com/"
     "closure-compiler/issues/issue-253.json", "googlecode-json"),
    ("https://code.google.com/archive/p/mockito/issues/188", "googlecode-page"),
    ("https://sourceforge.net/p/jfreechart/bugs/983/", "generic"),
    (None, "none"),
])
def test_classify_recognises_every_tracker_family(url, expected):
    assert classify(url) == expected


def test_every_real_bug_classifies_to_a_known_tracker():
    # The counts are load-bearing: 197 bugs (Closure + part of Mockito) sit on
    # Google Code, which the generic HTML path cannot read at all.
    counts = {}
    for project in PROJECT_BUG_COUNTS:
        for url in report_urls(project).values():
            kind = classify(url)
            counts[kind] = counts.get(kind, 0) + 1
    assert sum(counts.values()) == TOTAL_ACTIVE_BUGS
    assert counts == {
        "jira": 337, "github": 280, "googlecode-json": 174,
        "googlecode-page": 23, "generic": 22, "none": 18,
    }


def test_googlecode_page_urls_are_rewritten_to_their_json():
    """The archive page is JavaScript-only; scraping it yields boilerplate.

    Fetching https://code.google.com/archive/p/mockito/issues/188 returns
    ~210 bytes of "requires JavaScript to be enabled" and no issue text, so
    the 23 Mockito bugs pointing there need the archive's JSON instead.
    """
    assert googlecode_json_url("https://code.google.com/archive/p/mockito/issues/188") == (
        "https://storage.googleapis.com/google-code-archive/v2/code.google.com/"
        "mockito/issues/issue-188.json"
    )
    assert googlecode_json_url("https://example.org/not/an/issue") is None


def test_format_googlecode_matches_the_other_fetchers_shape():
    rendered = format_googlecode({
        "summary": "NPE in the parser",
        "comments": [{"content": "It throws."}, {"content": "Fixed in r42."}],
    })
    assert rendered == (
        "Summary: NPE in the parser\n\n"
        "Description:\nIt throws.\n\n"
        "Comment:\nFixed in r42."
    )


def test_format_googlecode_tolerates_an_issue_with_no_comments():
    assert format_googlecode({"summary": "Bare"}) == "Summary: Bare"


def test_load_cached_issue_distinguishes_absent_from_empty(tmp_path):
    """An empty cached file means "this bug has no issue", not "not cached".

    Conflating them would make the 18 Chart bugs hit the network on every run
    forever.
    """
    assert load_cached_issue("Chart", "3", issues_dir=str(tmp_path)) is None
    target = tmp_path / "Chart"
    target.mkdir()
    (target / "3.txt").write_text("")
    assert load_cached_issue("Chart", "3", issues_dir=str(tmp_path)) == ""
    (target / "1.txt").write_text("Summary: something")
    assert load_cached_issue("Chart", "1", issues_dir=str(tmp_path)) == "Summary: something"


def test_issue_path_layout():
    assert issue_path("Lang", "12", issues_dir="/c") == os.path.join("/c", "Lang", "12.txt")


# ------------------------------------------- usable issues and their statuses

def test_sourceforge_issues_are_reported_unusable(tmp_path):
    """SourceForge tickets scrape to a navigation menu, not the issue.

    The generic HTML-to-text extraction keeps the whole page shell, so the
    cached Chart and Time files open with ~40 lines of "Join/Login / Business
    Software / Open Source Software / ..." before any ticket text. Feeding
    that to a model is worse than feeding nothing, so those 22 bugs report
    ``unusable`` and contribute no issue.
    """
    csv_dir = _write_active_bugs(tmp_path / "csv", "Chart", [])
    (tmp_path / "csv" / "Chart" / "active-bugs.csv").write_text(
        "bug.id,revision.id.buggy,revision.id.fixed,report.id,report.url\n"
        "1,a,b,983,https://sourceforge.net/p/jfreechart/bugs/983/\n"
        "2,a,b,J-2,https://issues.apache.org/jira/browse/J-2\n"
    )
    issues = tmp_path / "issues" / "Chart"
    issues.mkdir(parents=True)
    (issues / "1.txt").write_text("JFreeChart / Bugs / #983\nJoin/Login\nBusiness Software")
    (issues / "2.txt").write_text("Summary: a real issue")

    text, status = issue_text("Chart", "1", issues_dir=str(tmp_path / "issues"),
                              active_bugs_dir=csv_dir)
    assert (text, status) == ("", ISSUE_UNUSABLE)
    # The raw download is still on disk: the exclusion is policy, not deletion.
    assert load_cached_issue("Chart", "1", issues_dir=str(tmp_path / "issues"))

    text, status = issue_text("Chart", "2", issues_dir=str(tmp_path / "issues"),
                              active_bugs_dir=csv_dir)
    assert (text, status) == ("Summary: a real issue", ISSUE_AVAILABLE)


@pytest.mark.parametrize("url,unusable", [
    ("https://sourceforge.net/p/jfreechart/bugs/983/", True),
    ("https://a.sourceforge.net/p/x/1/", True),
    ("https://issues.apache.org/jira/browse/LANG-1", False),
    ("https://github.com/jhy/jsoup/issues/1", False),
    (None, False),
])
def test_is_unusable_tracker(url, unusable):
    assert is_unusable_tracker(url) == unusable


def test_issue_text_distinguishes_every_no_issue_case(tmp_path):
    """An absent issue must say *why*, not just be an empty string.

    40 of the 854 bugs have no usable issue for three different reasons, and a
    bare "" would make them indistinguishable in result.json.
    """
    csv_dir = str(tmp_path / "csv")
    (tmp_path / "csv" / "Chart").mkdir(parents=True)
    (tmp_path / "csv" / "Chart" / "active-bugs.csv").write_text(
        "bug.id,revision.id.buggy,revision.id.fixed,report.id,report.url\n"
        "3,a,b,UNKNOWN,UNKNOWN\n"
        "9,a,b,R-9,https://issues.apache.org/jira/browse/R-9\n"
    )
    issues_dir = str(tmp_path / "issues")
    (tmp_path / "issues" / "Chart").mkdir(parents=True)
    # Cached but empty: the tracker had nothing (or there was no URL at all).
    (tmp_path / "issues" / "Chart" / "3.txt").write_text("")

    assert issue_text("Chart", "3", issues_dir, csv_dir) == ("", ISSUE_EMPTY)
    # Never downloaded at all -- distinct from "downloaded and empty", because
    # this one is worth fetching and that one is not.
    assert issue_text("Chart", "9", issues_dir, csv_dir) == ("", ISSUE_UNCACHED)


def test_the_real_cache_has_no_uncached_bugs():
    """Every one of the 854 bugs resolves to a definite status."""
    statuses = {}
    for project in PROJECT_BUG_COUNTS:
        for bug_id in active_bug_ids(project):
            _text, status = issue_text(project, bug_id)
            statuses[status] = statuses.get(status, 0) + 1
    assert statuses.get(ISSUE_UNCACHED, 0) == 0, "run: python -m d4j.fetch_issues"
    # 22 SourceForge (Chart 8 + Time 14), 18 Chart bugs with no URL and the one
    # Jsoup issue GitHub no longer has.
    assert statuses[ISSUE_UNUSABLE] == 22
    assert statuses[ISSUE_EMPTY] == 19
    assert statuses[ISSUE_AVAILABLE] == TOTAL_ACTIVE_BUGS - 22 - 19


def test_chart_contributes_no_issue_at_all():
    """The user's finding: Chart's issues are blank or unusable, all 26."""
    for bug_id in active_bug_ids("Chart"):
        text, status = issue_text("Chart", bug_id)
        assert text == ""
        assert status in (ISSUE_EMPTY, ISSUE_UNUSABLE)


# ------------------------------------------------- trimming the comment thread

def test_strip_issue_comments_keeps_the_report_and_drops_the_thread():
    """The maintainers' thread is written after the diagnosis and leaks the fix.

    Only the reporter's own text is context a repair tool would have had.
    """
    github = (
        "Title: NPE in the parser\n\n"
        "Body:\nIt throws on empty input.\n\n"
        "Comment:\nFixed in 2.6, see commit abc1234.\n\n"
        "Comment:\nThanks!"
    )
    assert strip_issue_comments(github) == (
        "Title: NPE in the parser\n\nBody:\nIt throws on empty input."
    )

    googlecode = (
        "Summary: args optimized away\n\n"
        "Description:\nThe length property breaks.\n\n"
        "Comment:\nThe compiler could replace it with a constant."
    )
    assert strip_issue_comments(googlecode) == (
        "Summary: args optimized away\n\nDescription:\nThe length property breaks."
    )


def test_strip_issue_comments_leaves_jira_issues_untouched():
    # fetch_issues asks Jira for summary+description only, so those 337 bugs
    # never had a thread to begin with.
    jira = "Summary: Something broke\n\nDescription:\nHere is how."
    assert strip_issue_comments(jira) == jira
    assert strip_issue_comments("") == ""


def test_strip_issue_comments_requires_the_marker_on_its_own_line():
    """A mention of "Comment:" inside prose or code must not cut the report."""
    inline = (
        "Title: Parser bug\n\n"
        "Body:\nThe javadoc says Comment: foo, which is wrong.\n"
        "See also /* Comment: bar */ in the source."
    )
    assert strip_issue_comments(inline) == inline.rstrip()


def test_issue_text_returns_the_trimmed_report():
    """The policy is applied where the text is read, not where it is cached."""
    raw = load_cached_issue("Closure", "1")
    trimmed, status = issue_text("Closure", "1")
    assert status == ISSUE_AVAILABLE
    assert "Comment:" in raw, "the cache still holds the thread as evidence"
    assert "Comment:" not in trimmed
    assert len(trimmed) < len(raw)


def test_trimming_never_empties_a_real_issue():
    """Guard on the real corpus: cutting must not leave a bug with no context.

    Measured when this was introduced: 436 of the 813 issues with content carry
    a thread, and none of them is left empty by the cut.
    """
    trimmed_count = 0
    for project in PROJECT_BUG_COUNTS:
        for bug_id in active_bug_ids(project):
            raw = load_cached_issue(project, bug_id)
            text, status = issue_text(project, bug_id)
            if status != ISSUE_AVAILABLE:
                continue
            assert text.strip(), f"{project} {bug_id} lost all context to trimming"
            if raw and "Comment:" in raw:
                trimmed_count += 1
    assert trimmed_count > 400, f"only {trimmed_count} issues trimmed; expected ~436"


def test_jira_projects_are_unaffected_by_trimming():
    """The eight Jira-tracked projects must come through byte for byte.

    They have no thread to cut, so ``strip_issue_comments`` returns the text
    untouched -- it only rstrips what it actually trimmed.
    """
    for project in ("Cli", "Codec", "Collections", "Compress", "Csv",
                    "JxPath", "Lang", "Math"):
        for bug_id in active_bug_ids(project):
            raw = load_cached_issue(project, bug_id)
            text, status = issue_text(project, bug_id)
            if status == ISSUE_AVAILABLE:
                assert text == raw, f"{project} {bug_id} was altered"


def test_runcampaign_does_not_put_the_bug_list_inside_export():
    """`sbatch --export=NAME=VALUE,...` is comma-separated, so a value holding
    a comma is truncated at the first one.

    Passing `BUG_IDS=1,2,3` that way delivered `BUG_IDS=1`, and every project
    ran only its first bug -- silently, because the job looks perfectly healthy.
    The list has to travel through the exported environment with a bare
    `--export=ALL` instead. The smoke test missed it because `--bug-id 1` makes
    every project's list a single id with no comma.
    """
    script = open(os.path.join(_repo_root(), "scripts/runCampaign.sh"),
                  encoding="utf-8").read()
    code = [l for l in script.splitlines() if not l.lstrip().startswith("#")]
    assert any("--export=ALL \\" in l or l.strip() == "--export=ALL" for l in code), \
        "the submission must use a bare --export=ALL"
    assert not any("--export=ALL," in l for l in code), \
        "BUG_IDS in --export=NAME=VALUE would be cut at its first comma"
    assert any(l.strip().startswith("export ") and "BUG_IDS=" in l for l in code), \
        "BUG_IDS must be exported into the environment instead"


# ---------------------------------------------------------- collect_project

def test_collect_project_reads_the_fields_the_analysis_needs(tmp_path):
    """The notebook in analysis/ builds its DataFrame from these records.

    The subtle ones: ``fixcheck_analyzed`` (0 means the verdict is vacuous,
    which the analysis must not count as evidence), ``llm_seconds`` vs
    ``seconds`` (generation time vs whole-run wall clock), and the token
    counts for the cost section.
    """
    from summarize_campaign import collect_project

    bug_dir = tmp_path / "Lang" / "Bug_12"
    bug_dir.mkdir(parents=True)
    (bug_dir / "result.json").write_text(json.dumps({
        "applied": True, "triggers_fixed": True, "fixed": True,
        "new_failures": [], "failing_tests_after": 1, "compiled_after": True,
        "included_issue": True, "issue_status": "available",
        "elapsed_seconds": 53.8,
        "usage_metadata": {"input_tokens": 5296, "output_tokens": 3194},
        "fixcheck": {"analyzed_test_classes": 1, "suspicious": False},
        "fixcheck_suspicious": False,
    }))
    (bug_dir / "run_status.json").write_text(json.dumps({
        "status": "ok", "exit_code": 0, "seconds": 98.1,
    }))
    # A bug that errored: run_status only, no result.json.
    err_dir = tmp_path / "Lang" / "Bug_7"
    err_dir.mkdir(parents=True)
    (err_dir / "run_status.json").write_text(json.dumps({
        "status": "error", "exit_code": 1, "seconds": 12.0,
    }))

    records = {r["bug_id"]: r for r in collect_project(str(tmp_path), "Lang")}
    ok = records["12"]
    assert ok["fixed"] and ok["has_result"]
    assert ok["input_tokens"] == 5296 and ok["output_tokens"] == 3194
    assert ok["llm_seconds"] == 53.8 and ok["seconds"] == 98.1
    assert ok["issue_status"] == "available"
    assert ok["fixcheck_analyzed"] == 1 and ok["new_failures"] == 0

    err = records["7"]
    assert not err["has_result"] and err["status"] == "error"
    # Absent fields must come back as None -- not 0, which would be
    # indistinguishable from a run that measured zero analysed classes.
    assert err["input_tokens"] is None and err["fixcheck_analyzed"] is None
    assert err["max_failure_similarity"] is None
    assert err["fixcheck_invoked"] is False and err["fixcheck_ran"] is False


def test_collect_project_vacuous_fixcheck_is_distinguishable(tmp_path):
    """suspicious=False with nothing analyzed must not look like evidence."""
    from summarize_campaign import collect_project

    bug_dir = tmp_path / "Lang" / "Bug_1"
    bug_dir.mkdir(parents=True)
    (bug_dir / "result.json").write_text(json.dumps({
        "applied": True, "triggers_fixed": True, "fixed": True,
        "compiled_after": True,
        "fixcheck": {"ok": True, "analyzed_test_classes": 0, "suspicious": False},
        "fixcheck_suspicious": False,
    }))
    record = collect_project(str(tmp_path), "Lang")[0]
    assert record["fixcheck_ran"] is True
    assert record["fixcheck_analyzed"] == 0    # <- the vacuous marker


def test_collect_project_separates_fixcheck_invoked_from_actually_ran(tmp_path):
    """FixCheck was called on all 203 non-compiling patches and aborted on
    every one of them; `bool(fixcheck)` counted those as FixCheck runs, which
    is why the CLI table and the notebook disagreed by exactly 203."""
    from summarize_campaign import collect_project

    bug_dir = tmp_path / "Lang" / "Bug_2"
    bug_dir.mkdir(parents=True)
    (bug_dir / "result.json").write_text(json.dumps({
        "applied": True, "triggers_fixed": True, "fixed": True,
        "compiled_after": False,
        "fixcheck": {"ok": False, "error": "defects4j compile failed:\n..."},
        "fixcheck_suspicious": False,
    }))
    record = collect_project(str(tmp_path), "Lang")[0]
    assert record["fixcheck_invoked"] is True   # Experiment.py did call it
    assert record["fixcheck_ran"] is False      # ... and it aborted at once
    assert record["fixcheck_analyzed"] is None
    # And the compile guard still applies, independently.
    assert record["compiled_after"] is False and record["fixed"] is False


def test_patch_defects4j_image_script_is_safe_to_rerun():
    """The image patch must retag in place, be idempotent, and target the CSVs.

    Chart 26 is the one bug in the benchmark that makes Defects4J *write* to
    framework/projects/<P>/dir-layout.csv (its buggy revision 102 is missing
    from Chart's layout map), and the stock image ships that file root-owned
    644 while our containers run as the host uid. Guarding the script here
    because the failure is a single bug out of 854 -- easy to reintroduce and
    easy to miss.
    """
    script = open(os.path.join(_repo_root(), "scripts/patchDefects4jImage.sh"),
                  encoding="utf-8").read()
    code = [l for l in script.splitlines() if not l.lstrip().startswith("#")]
    joined = "\n".join(code)

    assert "chmod a+w /defects4j/framework/projects/*/dir-layout.csv" in joined
    # Retagged in place, so nothing that names defects4j:3.0.1 has to change.
    assert 'IMAGE="defects4j:3.0.1"' in joined
    assert 'docker build -t "$IMAGE" -' in joined
    # The label is both the marker and the idempotency check.
    assert 'LABEL="org.fixcheckeval.layout-writable"' in joined
    assert "already patched" in joined


def test_run_project_warns_about_an_unpatched_image():
    """A campaign on a stock image should say so, not fail one bug hours in."""
    import run_project

    assert run_project.LAYOUT_WRITABLE_LABEL == "org.fixcheckeval.layout-writable"
    source = open(os.path.join(_repo_root(), "run_project.py"), encoding="utf-8").read()
    assert "warn_if_image_unpatched()" in source, "the warning must be wired into main()"
    # A warning, not an abort: 853 of 854 bugs work fine without the patch.
    assert "problems.append" not in source.split("def warn_if_image_unpatched")[1].split("\ndef ")[0]


def test_collect_project_corrects_a_patch_that_never_compiled(tmp_path):
    """Historical results are re-scored, not trusted blindly.

    A run recorded before the Experiment.py guard says fixed=True even though
    the sources never compiled (failing_tests_after == -1). The loader must
    correct that so the notebook and the CLI both report the honest number,
    while keeping the original value visible as `fixed_as_recorded`.
    """
    from summarize_campaign import collect_project

    bug_dir = tmp_path / "JxPath" / "Bug_6"
    bug_dir.mkdir(parents=True)
    (bug_dir / "result.json").write_text(json.dumps({
        "applied": True, "triggers_fixed": True, "fixed": True,
        "failing_tests_after": -1,          # no "Failing tests:" line -> no compile
    }))
    record = collect_project(str(tmp_path), "JxPath")[0]
    assert record["applied"] is True
    assert record["compiled_after"] is False
    assert record["triggers_fixed"] is False and record["fixed"] is False
    assert record["fixed_as_recorded"] is True


def test_collect_project_prefers_the_recorded_compiled_after_flag(tmp_path):
    """Newer runs carry the flag explicitly; it wins over the -1 heuristic."""
    from summarize_campaign import collect_project

    bug_dir = tmp_path / "Lang" / "Bug_1"
    bug_dir.mkdir(parents=True)
    (bug_dir / "result.json").write_text(json.dumps({
        "applied": True, "triggers_fixed": True, "fixed": True,
        "compiled_after": True, "failing_tests_after": 3,
    }))
    record = collect_project(str(tmp_path), "Lang")[0]
    assert record["compiled_after"] is True and record["fixed"] is True


def test_collect_project_keeps_the_audit_value_after_a_backfill(tmp_path):
    """Once a file is repaired, its own `fixed` is the corrected one.

    scripts/backfill_compiled_after.py preserves the pre-guard value as
    `fixed_as_recorded`; the loader must prefer it, otherwise the notebook
    reports the size of the correction as zero and the audit trail is lost.
    """
    from summarize_campaign import collect_project

    bug_dir = tmp_path / "JxPath" / "Bug_6"
    bug_dir.mkdir(parents=True)
    (bug_dir / "result.json").write_text(json.dumps({
        "applied": True, "compiled_after": False,
        "triggers_fixed": False, "fixed": False,
        "fixed_as_recorded": True, "triggers_fixed_as_recorded": True,
        "failing_tests_after": -1,
    }))
    record = collect_project(str(tmp_path), "JxPath")[0]
    assert record["fixed"] is False
    assert record["fixed_as_recorded"] is True


def test_ollama_daemon_context_is_derived_not_duplicated():
    """The daemon must load the model with at least the window the client asks
    for. Ollama silently clamps a per-request num_ctx above what the runner was
    loaded with -- and worse, FixCheck's OllamaGenerator sends no options, so a
    mismatch makes the server start a *second* runner and thrash. Two hardcoded
    copies of the number would drift; this asserts there is only one.
    """
    script = open(os.path.join(_repo_root(), "scripts/ollama_serve.sh"),
                  encoding="utf-8").read()
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "OLLAMA_CONTEXT_LENGTH" in code
    assert "FixGenerator.DEFAULT_CONTEXT_LENGTH" in code, (
        "ollama_serve.sh must read the context length from FixGenerator rather "
        "than repeating it"
    )


def test_per_bug_timeout_exceeds_the_runs_that_previously_timed_out():
    """6 runs died at exactly 7200.3 s; re-running at 7200 reproduces them."""
    script = open(os.path.join(_repo_root(), "scripts/runCampaign.sh"),
                  encoding="utf-8").read()
    match = re.search(r'^TIMEOUT="(\d+)"', script, re.M)
    assert match, "runCampaign.sh no longer sets a default TIMEOUT"
    assert int(match.group(1)) > 7200


def test_ollama_forces_the_cuda_backend():
    """Ollama 0.32 enabled Vulkan by default, and Vulkan ignores
    CUDA_VISIBLE_DEVICES -- the only GPU isolation this cluster applies. Every
    concurrent job then enumerates all GPUs, picks one by free memory, and jobs
    started together collide on the same card. Guarded here because the symptom
    (a buffer allocation failure deep in ggml) points nowhere near the cause.
    """
    script = open(os.path.join(_repo_root(), "scripts/ollama_serve.sh"),
                  encoding="utf-8").read()
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert re.search(r"export OLLAMA_VULKAN=0\b", code), (
        "ollama_serve.sh must pin OLLAMA_VULKAN=0 so the runner honours the "
        "GPU SLURM allocated"
    )
