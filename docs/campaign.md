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
`bool(issue_text)`, so a bug whose tracker could not be reached records `false`
rather than claiming the issue was in the prompt. 280 of the 854 bugs use
GitHub trackers and need `GITHUB_TOKEN` set — without it the unauthenticated
limit is 60 requests/hour against ~560 needed, and most of those prompts would
silently lack their issue.
