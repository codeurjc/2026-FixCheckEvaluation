# The generation budget: why 32768, and why not raise it further

An operational decision taken during the 2026-09-09 campaign, from the first
runs' data. Companion to [campaign.md](campaign.md) (the protocol) and
[audit-2026-09.md](audit-2026-09.md) (where the problem came from).

## The problem this set out to fix

In the archived campaign (`results/old/9-Sep/`) the generation budget **decided
the outcome of 46 runs**: 30 exhausted the output ceiling
(`num_predict=24576`) and 16 the context window (`num_ctx=49152`). All 46 are
`qwen3.6:35b` and **none** is `gpt-oss:120b`, because Ollama's `eval_count`
counts a reasoning model's chain of thought as output. All 46 were recorded as
the model failing to fix the bug.

A flat token budget is not neutral between a model that reasons and a concise
one.

## What changed

| | before | now |
|---|---:|---:|
| `num_predict` (output) | 24,576 | **32,768** |
| `num_ctx` (window) | 49,152 | **131,072** |

131,072 is **`gpt-oss:120b`'s native context length** (`qwen3.6:35b`'s is
262,144). Both models must get the same window or the asymmetry comes back, and
asking gpt-oss for more would be silently clamped.

## The warning: the new ceiling is reached too

37 runs into the new campaign, the monitor reported two truncated runs:

```
qwen3.6:35b/JacksonDatabind/Bug_2   32768 tok, 139,084 chars of reasoning
qwen3.6:35b/JacksonDatabind/Bug_77  32768 tok, 133,885 chars of reasoning
```

Both with `done_reason='length'` and a **0-character answer**.

## Why the budget is NOT raised again

The instinct is to raise `num_predict`. The data says otherwise (first 37 runs):

| | qwen3.6:35b | gpt-oss:120b |
|---|---:|---:|
| runs that finished cleanly | 14 | 21 |
| median `output_tokens` | 7,142 | 3,094 |
| **maximum** `output_tokens` | **12,043** | 6,303 |
| truncated | 2 | **0** |

**32,768 is already 2.7x qwen's largest *successful* generation.** A run that
has burned the whole ceiling is not thinking hard; it is stuck. Raising it to
65,536 would cost twice the GPU time and almost certainly end the same way.

Three checks behind the decision:

1. **The window is not the constraint.** In the truncated runs, input + output =
   46,807 of 131,072 available. Context to spare; `num_predict` is what binds.
2. **Cost is not the obstacle.** At a measured ~135 tok/s, 32,768 tokens take
   4.1 min and 98,304 would take 12.2 min. The budget *could* be raised — it
   simply would not help.
3. **The rate has not blown up.** In the archived campaign 30 of 852 qwen runs
   (**3.5%**) hit the old ceiling. It is 2 of 16 so far, but both are
   JacksonDatabind — the project with the largest prompts — and the sample is
   far too small to compare.

## What did change, and it is the point

Those two runs are **no longer counted as the model failing to fix the bug**.
They are recorded in `result.json` as:

```json
"generation_status": "truncated",
"done_reason": "length",
"response_truncated": true,
"reasoning_chars": 139084,
"max_tokens": 32768,
"context_length": 131072
```

The analysis can drop them from the denominator or report them separately —
exactly what was impossible for the archived campaign's 46. **The audit fix is
not that the budget never binds; it is that when it binds, you know.**

## Known instrumentation limit

`reasoning_chars` records the *length* of the chain of thought but **not its
text**: `message.thinking` is not persisted. So "it is stuck in a repetition
loop" is an **inference** — it exceeds 2.7x what any successful run needs — and
not something demonstrated. Diagnosing it properly would mean storing the
reasoning, which would mean relaunching the campaign.

## Rule for future campaigns

Before raising the budget because runs came back truncated, compare the ceiling
against the **maximum of the runs that do finish**. If the ceiling already
clears it comfortably, the problem is not the budget and raising it only spends
GPU. `result.json`'s `generation_status` and `reasoning_chars` give that
comparison directly.
