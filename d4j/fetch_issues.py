"""
fetch_issues.py — download every Defects4J bug's issue report, once.

A campaign run with ``--include-issue`` fetches each bug's issue from its
tracker while the GPU sits idle waiting. That is slow, non-reproducible, and on
GitHub simply does not work at scale: unauthenticated ``api.github.com`` allows
**60 requests per hour** and the 280 GitHub-tracked bugs need two calls each.
``fetch_issue_text`` swallows every failure and returns ``""``, so the damage is
invisible — a third of the benchmark would silently run without the issue its
``result.json`` claims it had.

So the issues are downloaded once into ``d4j/issues/<Project>/<bug_id>.txt`` and
committed. After that a run reads a local file and the network never enters the
picture.

The five tracker families in Defects4J, and what each needs:

===========================  =====  =========================================
Tracker                      Bugs   Handling
===========================  =====  =========================================
issues.apache.org (Jira)      337   REST API, summary + description
github.com                    280   REST API, title + body + comments; needs
                                    GITHUB_TOKEN for the 5000/hour allowance
storage.googleapis.com        174   Google Code archive JSON (Closure)
code.google.com                23   Archive *page*, JS-only: rewritten to the
                                    equivalent JSON URL, since scraping it
                                    yields 210 bytes of "enable JavaScript"
sourceforge.net                22   Generic HTML-to-text
(report.url = UNKNOWN)         18   No issue exists; cached as empty
===========================  =====  =========================================

    python -m d4j.fetch_issues                 # everything not already cached
    python -m d4j.fetch_issues --project Lang  # one project
    python -m d4j.fetch_issues --force         # re-download
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d4j.defects4j_bugs import (  # noqa: E402
    ISSUES_DIR,
    PROJECTS,
    active_bug_ids,
    is_unusable_tracker,
    issue_path,
    report_urls,
)

# Reuse the pipeline's own fetchers so a cached issue is byte-for-byte what a
# live run would have produced -- otherwise the cache would quietly change the
# prompt.
from Experiment import (  # noqa: E402
    _fetch_generic_issue,
    _fetch_github_issue,
    _fetch_jira_issue,
    _http_get_json,
)

GOOGLE_CODE_JSON = (
    "https://storage.googleapis.com/google-code-archive/v2/code.google.com/"
    "{project}/issues/issue-{number}.json"
)


def classify(url):
    """Which fetcher a report URL needs: one of the tracker family names."""
    if not url:
        return "none"
    host = urllib.parse.urlparse(url).netloc
    if host == "issues.apache.org":
        return "jira"
    if host == "github.com":
        return "github"
    if host == "storage.googleapis.com":
        return "googlecode-json"
    if host == "code.google.com":
        return "googlecode-page"
    return "generic"


def googlecode_json_url(page_url):
    """Turn a Google Code *archive page* URL into its JSON data URL.

    ``https://code.google.com/archive/p/mockito/issues/188`` ->
    ``.../google-code-archive/v2/code.google.com/mockito/issues/issue-188.json``

    The page itself renders through JavaScript, so fetching it returns a
    boilerplate shell with no issue text at all; the archive's JSON has the
    real content and is what Defects4J points at for Closure anyway.
    """
    parts = [p for p in urllib.parse.urlparse(page_url).path.split("/") if p]
    if "p" in parts and "issues" in parts:
        project = parts[parts.index("p") + 1]
        number = parts[parts.index("issues") + 1]
        return GOOGLE_CODE_JSON.format(project=project, number=number)
    return None


def format_googlecode(data):
    """Render Google Code archive JSON the way the Jira/GitHub fetchers do."""
    parts = [f"Summary: {data.get('summary', '')}"]
    comments = [
        c.get("content", "").strip()
        for c in data.get("comments", [])
        if c.get("content", "").strip()
    ]
    if comments:
        parts.append(f"Description:\n{comments[0]}")
        parts.extend(f"Comment:\n{c}" for c in comments[1:])
    return "\n\n".join(parts)


def fetch_one(url, kind):
    """Fetch and render one issue. Raises on failure; the caller records it."""
    if kind == "none":
        return ""
    if kind == "jira":
        return _fetch_jira_issue(url)
    if kind == "github":
        return _fetch_github_issue(url)
    if kind == "googlecode-json":
        return format_googlecode(_http_get_json(url))
    if kind == "googlecode-page":
        json_url = googlecode_json_url(url)
        if not json_url:
            raise ValueError(f"cannot derive the archive JSON URL from {url}")
        return format_googlecode(_http_get_json(json_url))
    return _fetch_generic_issue(url)


def fetch_with_retry(url, kind, attempts=4, pause=5.0):
    """Fetch, backing off on rate limits and transient server errors.

    GitHub answers 403 (or 429) when the allowance runs out and 5xx when it is
    unhappy; both are worth waiting out rather than losing the bug from the
    cache. A 404 is permanent -- some referenced issues really are gone -- so
    it fails immediately instead of sleeping four times for nothing.
    """
    delay = pause
    for attempt in range(1, attempts + 1):
        try:
            return fetch_one(url, kind)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or attempt == attempts:
                raise
            if exc.code in (403, 429) or exc.code >= 500:
                print(f"    HTTP {exc.code}; retrying in {delay:.0f}s "
                      f"({attempt}/{attempts - 1})", flush=True)
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise
            print(f"    {exc}; retrying in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def github_rate_limit():
    """Remaining core-API allowance, or ``None`` if it cannot be read."""
    try:
        core = _http_get_json("https://api.github.com/rate_limit")["resources"]["core"]
        return core["remaining"], core["limit"]
    except Exception:
        return None


def fetch_project(project, issues_dir, force=False, pause=0.0, dry_run=False):
    """Download every issue of one project. Returns the per-bug records."""
    urls = report_urls(project)
    bug_ids = active_bug_ids(project)
    os.makedirs(os.path.join(issues_dir, project), exist_ok=True)

    records = []
    for bug_id in bug_ids:
        url = urls.get(bug_id)
        kind = classify(url)
        path = issue_path(project, bug_id, issues_dir)
        record = {"bug_id": bug_id, "url": url, "tracker": kind}

        if is_unusable_tracker(url):
            # Still downloaded and kept -- the file is evidence, and the
            # exclusion is a policy that can be revisited -- but flagged here
            # so the index says which bugs will never reach a prompt.
            record["usable"] = False
        if not force and os.path.exists(path):
            record.update(status="cached", chars=os.path.getsize(path))
            records.append(record)
            continue
        if dry_run:
            record.update(status="would-fetch", chars=None)
            records.append(record)
            continue

        try:
            text = fetch_with_retry(url, kind)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            # An empty file is a real answer for the 18 Chart bugs with no
            # known URL; anything else empty means the tracker gave us nothing.
            record.update(
                status="empty" if not text.strip() else "ok",
                chars=len(text),
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"  {project} {bug_id}: {record['status']} "
                  f"({record['chars']} chars, {kind})", flush=True)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            # The issue is genuinely gone -- Jsoup 45 points at
            # github.com/jhy/jsoup/issues/575, which 404s on both the web and
            # the API. Cache it as empty so no run ever pays for that request
            # again, but keep the status distinct from the never-had-a-URL
            # bugs so the index still says *why* it is empty.
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            record.update(
                status="not-found", chars=0,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"  {project} {bug_id}: not-found ({kind}) -- the tracker no "
                  "longer has this issue; cached as empty", flush=True)
        except Exception as exc:
            record.update(status="failed", chars=None, error=f"{type(exc).__name__}: {exc}")
            print(f"  {project} {bug_id}: FAILED ({kind}) -- {exc}", flush=True)
        records.append(record)
        if pause:
            time.sleep(pause)

    if not dry_run:
        index = {
            "project": project,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "bugs": records,
        }
        with open(os.path.join(issues_dir, project, "index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2)
    return records


def main():
    parser = argparse.ArgumentParser(
        description="Download every Defects4J bug's issue report into d4j/issues/.",
    )
    parser.add_argument(
        "--project", nargs="+", default=None,
        help="Projects to fetch (default: all 17).",
    )
    parser.add_argument(
        "--issues-dir", default=ISSUES_DIR,
        help=f"Where to write the cache (default: {ISSUES_DIR}).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download issues that are already cached.",
    )
    parser.add_argument(
        "--pause", type=float, default=0.0,
        help="Seconds to wait between requests (default: 0; the token's "
             "5000/hour allowance is ample for 854 bugs).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be fetched, per tracker, without downloading.",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()
    projects = args.project or list(PROJECTS)

    if not os.getenv("GITHUB_TOKEN"):
        limit = github_rate_limit()
        print("[fetch_issues] WARNING: no GITHUB_TOKEN set. The 280 GitHub-tracked "
              "bugs need two calls each against a 60/hour unauthenticated limit"
              + (f" (currently {limit[0]}/{limit[1]} left)." if limit else ".")
              + " Put a token in .env first, or expect most of them to fail.",
              flush=True)

    all_records = []
    for project in projects:
        print(f"\n[fetch_issues] {project}", flush=True)
        all_records.extend(
            (project, r) for r in fetch_project(
                project, args.issues_dir, args.force, args.pause, args.dry_run
            )
        )

    by_status, by_tracker = {}, {}
    for _project, record in all_records:
        by_status[record["status"]] = by_status.get(record["status"], 0) + 1
        by_tracker[record["tracker"]] = by_tracker.get(record["tracker"], 0) + 1
    unusable = [(p, r) for p, r in all_records if r.get("usable") is False]

    print(f"\n[fetch_issues] {len(all_records)} bug(s)")
    print("  by status:  " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    print("  by tracker: " + ", ".join(f"{k}={v}" for k, v in sorted(by_tracker.items())))
    if unusable:
        projects_hit = sorted({p for p, _ in unusable})
        print(f"  excluded:   {len(unusable)} downloaded but not usable as prompt "
              f"context ({', '.join(projects_hit)}) -- their tracker scrapes to a "
              "navigation menu, see UNUSABLE_ISSUE_HOSTS")

    failures = [(p, r) for p, r in all_records if r["status"] == "failed"]
    if failures:
        print(f"\n  {len(failures)} failure(s):")
        for project, record in failures:
            print(f"    {project} {record['bug_id']} ({record['tracker']}): {record['error']}")
        print("  Re-run to retry them; cached issues are skipped.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
