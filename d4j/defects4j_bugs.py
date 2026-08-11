"""
defects4j_bugs.py — which bugs exist in Defects4J, and how to select them.

Defects4J has 854 *active* bugs across 17 projects, and their ids are **not
contiguous**: a bug that stopped reproducing under a newer JVM is deprecated
and removed from ``active-bugs.csv`` while keeping its id reserved (Lang, for
instance, is 1, 3-17, 19-24, 26-47, 49-65 — 2, 18, 25 and 48 are gone). Any
runner that assumes ``range(1, n + 1)`` therefore spends minutes of container
time per dead id before ``Experiment.py`` gives up on the checkout.

This module is the single place that knows the real list, so the campaign can
say "all bugs of project P" and get exactly the reproducible ones. It is pure
in the common path (it reads a CSV), which keeps it unit-testable without
Docker.
"""

import csv
import functools
import os
import re
import subprocess
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFECTS4J_IMAGE = "defects4j:3.0.1"

# Where fetch_issues.py parks the downloaded issue reports, next to the code
# that knows about them. Versioned, unlike results/ and workspace/: an issue
# report is an *input* to the experiment, and keeping it in the repo makes a
# campaign reproducible without depending on five different trackers still
# answering the same way years later.
ISSUES_DIR = os.path.join(HERE, "issues")

# Defects4J's own marker for "we do not know this bug's issue URL" (18 Chart
# bugs). Not a fetch failure: there is nothing to fetch.
UNKNOWN_URL = "UNKNOWN"

# Trackers whose pages cannot be turned into usable prompt context. SourceForge
# renders a ticket inside a large navigation shell, and the generic HTML-to-text
# extraction keeps all of it: the cached files for Chart and Time open with
# ~40 lines of "Join/Login / Business Software / Open Source Software / ..."
# before any issue text, which is noise a model would have to see past. The 22
# affected bugs (Chart 8, Time 14) are reported as `unusable` and get no issue
# rather than a menu.
UNUSABLE_ISSUE_HOSTS = frozenset({"sourceforge.net"})

# Start of the maintainers' comment thread. The marker is reliable because our
# own fetchers write it -- ``_fetch_github_issue`` and ``format_googlecode``
# emit exactly this line before each comment -- rather than it being scraped.
# Jira issues never carry one: fetch_issues only requests summary+description.
ISSUE_COMMENT_MARKER = re.compile(r"^Comment:\s*$", re.M)

# Values of the issue status reported by :func:`issue_text`.
ISSUE_AVAILABLE = "available"    # real text, safe to put in a prompt
ISSUE_EMPTY = "empty"            # cached, but the tracker had nothing (or the
                                 # bug has no URL at all)
ISSUE_UNUSABLE = "unusable"      # excluded tracker: see UNUSABLE_ISSUE_HOSTS
ISSUE_UNCACHED = "uncached"      # never downloaded; run d4j.fetch_issues

# The 17 projects, with the active-bug count Defects4J's own README states.
# Kept as a cross-check: if a resolved list disagrees, the checkout is a
# different Defects4J version than the one this campaign was designed against,
# which is worth knowing *before* burning GPU hours on it.
PROJECT_BUG_COUNTS = {
    "Chart": 26, "Cli": 39, "Closure": 174, "Codec": 18, "Collections": 28,
    "Compress": 47, "Csv": 16, "Gson": 18, "JacksonCore": 26,
    "JacksonDatabind": 110, "JacksonXml": 6, "Jsoup": 93, "JxPath": 22,
    "Lang": 61, "Math": 106, "Mockito": 38, "Time": 26,
}
PROJECTS = tuple(PROJECT_BUG_COUNTS)
TOTAL_ACTIVE_BUGS = sum(PROJECT_BUG_COUNTS.values())  # 854


def _csv_candidates(project, active_bugs_dir=None):
    """Places ``active-bugs.csv`` may live, in order of preference."""
    candidates = []
    if active_bugs_dir:
        candidates.append(os.path.join(active_bugs_dir, project, "active-bugs.csv"))
    env_dir = os.getenv("D4J_ACTIVE_BUGS_DIR")
    if env_dir:
        candidates.append(os.path.join(env_dir, project, "active-bugs.csv"))
    # The vendored clone at the repository root (this module lives in d4j/).
    # Gitignored, so a fresh checkout may not have it -- hence the fallbacks
    # below and the Docker path in active_bug_ids.
    candidates.append(
        os.path.join(REPO_ROOT, "defects4j", "framework", "projects", project,
                     "active-bugs.csv")
    )
    d4j_home = os.getenv("DEFECTS4J_HOME")
    if d4j_home:
        candidates.append(
            os.path.join(d4j_home, "framework", "projects", project, "active-bugs.csv")
        )
    return candidates


def _read_active_bugs_rows(path):
    """Data rows of an ``active-bugs.csv``, header dropped, in file order.

    The header is recognised by name rather than by position, so a reordered
    export cannot silently yield a bug called "bug.id".
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = [row for row in csv.reader(f) if row and row[0].strip()]
    if rows and rows[0][0].strip() == "bug.id":
        rows = rows[1:]
    return rows


def _read_active_bugs_csv(path):
    """Bug ids from an ``active-bugs.csv``, in file order."""
    return [row[0].strip() for row in _read_active_bugs_rows(path)]


def _bids_from_docker(project):
    """Ask Defects4J itself, for checkouts without the vendored clone."""
    result = subprocess.run(
        ["docker", "run", "--rm", DEFECTS4J_IMAGE, "defects4j", "bids", "-p", project],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def active_bug_ids(project, active_bugs_dir=None):
    """The active (reproducible) bug ids of ``project``, in Defects4J's order.

    Tries every known location of ``active-bugs.csv`` and falls back to
    ``defects4j bids`` inside the Docker image. Raises naming everything it
    tried, rather than silently returning an empty list that would make a
    project look finished.
    """
    if project not in PROJECT_BUG_COUNTS:
        raise ValueError(
            f"unknown Defects4J project {project!r}; expected one of {', '.join(PROJECTS)}"
        )
    tried = _csv_candidates(project, active_bugs_dir)
    for path in tried:
        if os.path.isfile(path):
            ids = _read_active_bugs_csv(path)
            if ids:
                return ids
    try:
        ids = _bids_from_docker(project)
    except (OSError, subprocess.SubprocessError):
        ids = None
    if ids:
        return ids
    raise RuntimeError(
        f"could not determine the active bug ids of {project}. Tried: "
        + ", ".join(tried)
        + f", and `docker run --rm {DEFECTS4J_IMAGE} defects4j bids -p {project}`. "
        "Clone Defects4J into ./defects4j, set DEFECTS4J_HOME or "
        "D4J_ACTIVE_BUGS_DIR, or make the Docker image available."
    )


def resolve_bug_ids(project, tokens=None, active_bugs_dir=None):
    """Resolve a ``--bug-id`` selection against the project's active bugs.

    ``tokens`` of ``None``, ``[]`` or ``["all"]`` means every active bug.
    Anything else is expanded by :func:`experiment_runner.parse_bug_ids` and
    then **validated**: requesting a deprecated or out-of-range id is an error
    naming the offending ids, because the alternative is discovering it one
    failed checkout at a time.

    The returned order follows Defects4J's own listing, not the order the ids
    were requested in, so a resumed run walks the project the same way.
    """
    from experiment_runner import parse_bug_ids  # local: keeps this module import-light

    active = active_bug_ids(project, active_bugs_dir)
    if not tokens or [str(t).strip().lower() for t in tokens] == ["all"]:
        return list(active)

    requested = parse_bug_ids(tokens)
    active_set = set(active)
    unknown = [b for b in requested if b not in active_set]
    if unknown:
        raise ValueError(
            f"{project} has no active bug(s) {', '.join(unknown)}. They are either "
            "deprecated (no longer reproducible) or out of range; "
            f"{project} has {len(active)} active bugs: {active[0]}..{active[-1]}."
        )
    requested_set = set(requested)
    return [b for b in active if b in requested_set]


def report_urls(project, active_bugs_dir=None):
    """Map every active bug id of ``project`` to its issue-report URL.

    Read straight from ``active-bugs.csv`` column 4 (``report.url``), which is
    the same URL ``defects4j info`` prints -- but available without starting a
    container. A bug Defects4J has no URL for carries the literal ``UNKNOWN``
    (18 Chart bugs); those are returned as ``None`` so callers can tell "no
    issue exists" from "the fetch failed".

    The CSV is parsed once per project (``issue_text`` asks per bug, and a
    campaign asks 854 times); the copy keeps a caller from mutating the cache.
    """
    return dict(_report_urls(project, active_bugs_dir))


@functools.lru_cache(maxsize=None)
def _report_urls(project, active_bugs_dir=None):
    for path in _csv_candidates(project, active_bugs_dir):
        if os.path.isfile(path):
            urls = {}
            for row in _read_active_bugs_rows(path):
                url = row[4].strip() if len(row) > 4 else ""
                urls[row[0].strip()] = None if (not url or url == UNKNOWN_URL) else url
            if urls:
                return urls
    raise RuntimeError(
        f"could not read report URLs for {project}: no active-bugs.csv found. "
        "The Docker fallback only yields bug ids, not URLs."
    )


def issue_path(project, bug_id, issues_dir=None):
    """Where the cached issue report of one bug lives."""
    return os.path.join(issues_dir or ISSUES_DIR, project, f"{bug_id}.txt")


def load_cached_issue(project, bug_id, issues_dir=None):
    """The pre-downloaded issue text, or ``None`` when it is not cached.

    A raw read: it applies no policy, so a caller that wants to inspect what
    was downloaded still can. Use :func:`issue_text` to get the text that is
    actually fit for a prompt.

    An empty cached file means "this bug genuinely has no issue" and is
    returned as ``""``, not ``None``, so it is not re-fetched.
    """
    path = issue_path(project, bug_id, issues_dir)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return None


def is_unusable_tracker(url):
    """True when a tracker's pages cannot be scraped into usable context."""
    if not url:
        return False
    host = urllib.parse.urlparse(url).netloc
    return host in UNUSABLE_ISSUE_HOSTS or host.endswith(
        tuple(f".{h}" for h in UNUSABLE_ISSUE_HOSTS)
    )


def strip_issue_comments(text):
    """Keep a bug's original report, drop the maintainers' comment thread.

    The thread is written *after* the bug was diagnosed and routinely discusses
    -- sometimes states -- the fix, which a repair tool would not have had at
    report time. Feeding it to the model leaks the answer, so only the
    ``Summary``/``Title`` and ``Description``/``Body`` reach the prompt.

    Measured over the 854 cached issues: 436 of the 813 with content (54%) have
    a thread, cutting it removes 42% of all issue text, and **no** issue is left
    empty. The 337 Jira bugs are untouched, which incidentally removes an
    asymmetry -- until now only the GitHub and Google Code bugs carried
    discussion at all.
    """
    match = ISSUE_COMMENT_MARKER.search(text)
    return text[:match.start()].rstrip() if match else text


def issue_text(project, bug_id, issues_dir=None, active_bugs_dir=None):
    """The bug's issue report as prompt context, with an explicit status.

    Returns ``(text, status)`` where ``status`` is one of
    :data:`ISSUE_AVAILABLE`, :data:`ISSUE_EMPTY`, :data:`ISSUE_UNUSABLE` or
    :data:`ISSUE_UNCACHED`, and ``text`` is ``""`` for everything but
    ``available``. Callers get a positive statement of *why* a prompt has no
    issue instead of having to infer it from an empty string -- 40 of the 854
    bugs are in that position (18 Chart bugs with no URL, one Jsoup issue the
    tracker deleted, and the 22 SourceForge ones).
    """
    try:
        url = report_urls(project, active_bugs_dir).get(bug_id)
    except (RuntimeError, ValueError):
        url = None
    if is_unusable_tracker(url):
        return "", ISSUE_UNUSABLE

    cached = load_cached_issue(project, bug_id, issues_dir)
    if cached is None:
        return "", ISSUE_UNCACHED
    if not cached.strip():
        return "", ISSUE_EMPTY
    return strip_issue_comments(cached), ISSUE_AVAILABLE


def chunk(ids, n):
    """Split ``ids`` into ``n`` contiguous, near-equal groups.

    Used to spread a large project (Closure's 174 bugs) over several SLURM
    jobs when one job would not finish inside the partition's wall-clock
    limit. Empty groups are dropped, so ``chunk(ids, n)`` with ``n > len(ids)``
    yields one group per id rather than empty jobs.
    """
    if n < 1:
        raise ValueError("chunks must be >= 1")
    total = len(ids)
    groups = []
    start = 0
    for i in range(n):
        size = total // n + (1 if i < total % n else 0)
        if size:
            groups.append(ids[start:start + size])
            start += size
    return groups
