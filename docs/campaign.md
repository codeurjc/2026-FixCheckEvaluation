# Full-benchmark campaign protocol

What was run, with which knobs, so the numbers in `results/` stay interpretable
after the fact. Fill in the launch dates and git shas as each wave goes out.

## Protocol

| | |
|---|---|
| Subjects | all **854 active** Defects4J bugs, 17 projects |
| Runs per bug | **1** (no `run_iterations.py`) |
| Model (fix) | `ollama/gpt-oss:120b`, `--temperature 0.0` |
| Model (FixCheck assertions) | the **same** model, as `ollama:gpt-oss:120b@<port>` |
| Prompt context | `--include-test-code --include-test-log --include-issue` |
| FixCheck | on, `--fixcheck-prefixes 10`, similarity threshold `0.8` (default) |
| Per-bug timeout | 7200 s |
| Hardware | one H100 per project job; jobs queue behind the node's 4 H100s |

Bug ids come from `defects4j/framework/projects/<P>/active-bugs.csv` via
`defects4j_bugs.py`, so deprecated ids (Lang 2/18/25/48, Cli 6, Closure 63/93,
JacksonDatabind 65/89, Time 21) are never attempted.

## Waves

| Wave | Projects | Bugs | Submitted | Git sha |
|---|---|---|---|---|
| Pilot | JacksonXml, Csv, Codec | 40 | | |
| Full | the remaining 14 | 814 | | |

```bash
# pilot
./scripts/runCampaign.sh --projects JacksonXml,Csv,Codec --minutes-per-bug 20
# measure, then
.venv/bin/python summarize_campaign.py --list-failures
# full launch, with the measured p90
./scripts/runCampaign.sh --projects all --minutes-per-bug <measured>
```

Each job writes `scripts/logs/<job_id>/manifest.json` with the git sha, argv,
model, Ollama port and resolved bug list. **Copy those somewhere durable**:
both `results/` and `scripts/logs/` are gitignored, so they are the only record
tying a number to the code that produced it.

## How to read the results

Three fields answer different questions and are routinely conflated:

- `applied` — the diff was applicable at all.
- `triggers_fixed` — the bug's trigger tests pass again.
- `fixed` — `triggers_fixed` **and** no test that passed before now fails.
  `fixed` never depends on FixCheck.

`fixcheck_suspicious` is **advisory and a single sample**. The model is
non-deterministic even at temperature 0, and FixCheck's own mutation and
assertion generation are random per run, so one run per bug cannot measure an
overfitting *rate* — it observes one draw. Read
[fixcheck-verdict-limitations.md](fixcheck-verdict-limitations.md) before
reporting any verdict: a `suspicious: false` is weak evidence, not proof the
patch is good, and `analyzed_test_classes > 0` is necessary for it to mean
anything at all.

Also worth knowing when comparing projects: `included_issue` is now
`bool(issue_text)`, so a bug whose issue is genuinely unavailable records
`false` rather than claiming it was in the prompt.

## Issue reports

The issues are **not** fetched during the campaign. `python -m d4j.fetch_issues`
downloads all 854 once into `d4j/issues/<Project>/<bug_id>.txt` (committed), and
`Experiment.py` reads from there. Fetching at run time was not viable:

| Tracker | Bugs | Why it needed handling |
|---|---:|---|
| Jira (`issues.apache.org`) | 337 | fine over the API |
| GitHub | 280 | 2 calls each vs **60/hour** unauthenticated → needs `GITHUB_TOKEN` |
| Google Code JSON (Closure) | 174 | JSON, not HTML; the generic scraper produced raw JSON |
| Google Code archive pages (Mockito) | 23 | JavaScript-only: scraping returns 210 bytes of boilerplate, so the URL is rewritten to the archive's JSON |
| SourceForge (Chart, Time) | 22 | **excluded** — see below |
| `report.url = UNKNOWN` (Chart) | 18 | no issue exists; cached as empty |

Re-running the fetcher only downloads what is missing, so a failed tracker can
be retried without touching the rest.

### The prompt carries the original report only

The issue is cut at the first `Comment:` line before it reaches the prompt
(`strip_issue_comments`). The maintainers' thread is written *after* the bug was
diagnosed and routinely discusses — sometimes states — the fix, so including it
would be answer leakage: at report time no repair tool could have had it.

| | |
|---|---|
| Issues with a comment thread | **436 of 813 (54%)** |
| Issue text dropped | 1471 KB → 859 KB (**−42%**) |
| Issues left empty by the cut | **0** |
| Jira-tracked bugs affected | 0 — they never had comments |

Per project: Closure 692 comments, JacksonDatabind 618, Jsoup 224, Mockito 201,
JacksonCore 72, Gson 56, JacksonXml 14, Time 10. The eight Jira projects (Cli,
Codec, Collections, Compress, Csv, JxPath, Lang, Math) are untouched, which also
removes an asymmetry: until now only the GitHub and Google Code bugs carried any
discussion at all.

**Residual leakage this does not fix**, and which should be stated when
reporting results:

- **15 bugs name the fix in the report body itself** (a patch, a commit hash or
  "fixed in X") — Closure 5, JacksonDatabind 3, Math 3, Compress 2, Cli 1,
  Gson 1. That text is part of the original report; removing it would mean
  hand-curating each one.
- **A few "issues" are pull requests** whose title already states the fix, e.g.
  Gson 6 (*"Fixed a regression in Gson 2.6 where…"*) and JacksonCore 13
  (*"Fix UTF8JsonGenerator to allow QUOTE_FIELD_NAMES to be toggled"*).

### 40 bugs carry no issue, for three different reasons

`--include-issue` is not uniform across the benchmark, and `result.json`'s
`issue_status` says which case each bug is:

| `issue_status` | Bugs | Meaning |
|---|---:|---|
| `available` | 814 | real text went into the prompt |
| `unusable` | 22 | SourceForge (Chart 8, Time 14). Its tickets render inside a navigation shell and the HTML-to-text extraction keeps all of it, so the cached files open with ~40 lines of *"Join/Login / Business Software / Open Source Software / …"* before any ticket text. Feeding that to a model is worse than feeding nothing, so it is excluded by `UNUSABLE_ISSUE_HOSTS`. The files stay on disk — the exclusion is policy, not deletion. |
| `empty` | 19 | 18 Chart bugs Defects4J has no URL for, plus Jsoup 45, whose GitHub issue was deleted |
| `not-requested` | — | the run did not pass `--include-issue` |

**Chart therefore contributes no issue at all** (18 blank + 8 unusable = its 26
bugs), and 14 of Time's 26 are in the same position. Worth remembering before
comparing per-project fix rates: those projects' prompts are strictly smaller
than the rest.
