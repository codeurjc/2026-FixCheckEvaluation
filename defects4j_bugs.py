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
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DEFECTS4J_IMAGE = "defects4j:3.0.1"

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
    # The vendored clone. Gitignored, so a fresh checkout of this repo may not
    # have it -- hence the fallbacks below and the Docker path in active_bug_ids.
    candidates.append(
        os.path.join(HERE, "defects4j", "framework", "projects", project, "active-bugs.csv")
    )
    d4j_home = os.getenv("DEFECTS4J_HOME")
    if d4j_home:
        candidates.append(
            os.path.join(d4j_home, "framework", "projects", project, "active-bugs.csv")
        )
    return candidates


def _read_active_bugs_csv(path):
    """Bug ids from an ``active-bugs.csv``, in file order.

    Column 0 is ``bug.id``; the header row is skipped by name rather than by
    position so a reordered export cannot silently yield a bug called
    "bug.id".
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = [row for row in csv.reader(f) if row and row[0].strip()]
    if rows and rows[0][0].strip() == "bug.id":
        rows = rows[1:]
    return [row[0].strip() for row in rows]


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
