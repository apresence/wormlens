# Compact Cost: Measured Numbers

Empirical results from running `wl --analyze-compacts` (currently a
local script; productized for v0.2) over the maintainer's session
JSONLs.

**Methodology:** read every CC session JSONL on disk, filter to
sessions >=100 KB and exclude spawned subagent transcripts
(`agent-*.jsonl`). For every `system / compact_boundary` record,
extract the compact summary text from the adjacent
`isCompactSummary=True` user record (which CC writes immediately
after the compact_boundary). Tokenize using tiktoken cl100k_base
(correct for opus-4-6, sonnet-4-6, haiku-4-5; approximation for
opus-4-7 which uses a newer opaque tokenizer). Dedup by
`(session_id, pre_tokens, content_length)` since the maintainer's
archive holds the same JSONLs across multiple hosts and backup dirs.

**Sample:** n=43 compact summaries in 24 unique sessions, spanning CC
versions 2.1.49 through 2.1.128, models claude-opus-4-6/4-7 and
claude-sonnet-4-6.

**Caveats:**

1. Crew-internal corpus, not a random sample of the CC user
   population. Workflows are dev-heavy.
2. Summary generation is charged at `cache_read_rate` ($0.50/M for
   Opus), assuming the summary call benefits from the same
   KV-cache prefix that interactive turns use. If CC issues an
   un-cached `messages.create` for the summary, the `preTokens`
   read incurs `input_rate` ($5/M for Opus) and gen costs are 10x
   higher. Indirect evidence (`cache_read_input_tokens` is
   preserved across the boundary) supports the cache-read
   assumption, but it's not directly verified.
3. No per-record `cost` field exists in any CC session JSONL we
   examined. All dollar figures are derived from token counts at
   the rates published in `docs/token-economics.md`.
4. Opus 4.7 uses an opaque tokenizer (not cl100k); 3 of 43
   summaries are opus-4-7 and use cl100k as an approximation.

## Summary-text token counts (tiktoken cl100k, n=43)

The compact summary text is the `isCompactSummary=True` record's
`message.content` -- a plain string starting with "This session is
being continued from a previous conversation..." that CC injects as
a synthetic user message after the compact_boundary.

| stat     | tokens |
|----------|-------:|
| min      |  1,779 |
| p25      |  2,864 |
| median   |  4,349 |
| mean     |  4,433 |
| p75      |  5,291 |
| p95      |  8,823 |
| max      |  9,982 |

**Summary-only residue** (tiktoken tokens / 200K window):

| stat     | residue |
|----------|--------:|
| min      |   0.89% |
| median   |   2.17% |
| mean     |   2.22% |
| max      |   4.99% |

## What `postTokens` actually measures

7 of 43 compacts carry both `compactMetadata.postTokens` (from newer
CC versions) and the `isCompactSummary` text. Comparing:

| postTokens | tiktoken (summary text) | overhead |
|-----------:|------------------------:|---------:|
|      2,586 |                   1,779 |      807 |
|      3,561 |                   2,749 |      812 |
|      5,846 |                   2,281 |    3,565 |
|      6,978 |                   3,585 |    3,393 |
|      7,747 |                   2,881 |    4,866 |
|     11,593 |                   2,814 |    8,779 |
|     15,052 |                   2,757 |   12,295 |

Median overhead: **3,565 tokens**. `postTokens` is NOT summary-text
tokens -- it includes the post-compact context size (system prompt,
tool definitions, and the summary). The summary text alone is
typically 18-77% of `postTokens`.

## chars/3.0 heuristic accuracy

Compared chars/3.0 (the offline heuristic documented for Claude
models without a public tokenizer) against tiktoken cl100k on the
same text:

- Median error: **+27.2%** (always overshoots)
- Range: +18.8% to +40.1%

Not suitable for measurement-grade claims. Use cl100k for
opus-4-6/sonnet-4-6/haiku-4-5; flag opus-4-7 results as
approximate.

## Headline distributions

| metric                       | n  | median  | p25     | p75     | p95     |
|------------------------------|----|---------|---------|---------|---------|
| `pre_tokens`                 | 49 | 167,286 | 167,050 | 168,182 | 171,715 |
| `summary_tokens` (tiktoken)  | 43 |   4,349 |   2,864 |   5,291 |   8,823 |
| `ctx_at_trigger_pct`         | 49 |   83.6% |   83.5% |   84.1% |   85.9% |
| `waste_zone_pct`             | 49 |   16.4% |   15.9% |   16.5% |   24.1% |
| `summary_residue_pct`        | 43 |    2.2% |    1.4% |    2.6% |    4.4% |
| `generation_cost_usd`        | 43 |  $0.192 |  $0.155 |  $0.216 |  $0.304 |
| `prefill_cost_per_turn_usd`  | 43 | $0.0022 | $0.0014 | $0.0026 | $0.0044 |

Generation cost uses tiktoken summary tokens at the session model's
output rate, plus `preTokens` at cache_read rate. Prefill cost uses
tiktoken summary tokens at cache_read rate (the per-turn carrying
cost of the summary in the post-compact context).

## Compacts per session (deduped)

n=24 sessions: median 2 compacts/session, mean 2.04, max 5. 11
sessions have 1 compact; 7 have 2; 6 have 3-5. 20 of 24 sessions
contained at least one near-window-full compact (`pre_tokens` >=
80% of the 200K standard window).

## Compact share of total session bill

For each session, summing every assistant turn at the dominant
model's input/cache_read/cache_creation/output rates:

| stat                  | value   |
|-----------------------|---------|
| median session bill   | $50.10  |
| max session bill      | $1,345  |
| median compact share  |  <1.0%  |

## What the README hypotheticals got wrong

| Quantity                          | README hypothetical | Measured (tiktoken, median) |
|-----------------------------------|--------------------:|----------------------------:|
| ctx at trigger                    |             ~80-85% |                       83.6% |
| summary residue (% of window)    |                ~20% |                        2.2% |
| summary gen cost (Opus, per-compact) |            ~$1.0 |                       $0.19 |
| per-turn carry cost (Opus)        |              ~$0.02|                      $0.002 |

The hypothetical overstated summary residue by ~9x and per-compact
cost by ~5x. Direction of the asymmetry vs. wormlens is unchanged
(compact still pays output rate to generate a summary; wormlens
extracts mechanically), but the absolute magnitude is much smaller
than the original numbers suggested.

## Reproduce

The scripts and per-row CSVs are gitignored under `.copilot/` for
the maintainer's local copy. Key artifacts:

- `compact_recon.py` / `compact_recon.json` -- Pass 1 schema recon
- `analyze_compacts.py` / `compact_rows_dedup.csv` -- Pass 2-3
- `compact_validation.py` / `compact_validation.md` -- Pass 4
- `tiktoken_recount.py` / `tiktoken_summary_counts.csv` -- tiktoken pass

v0.2's `wl --analyze-compacts` mode will let any user run the same
analysis on their own corpus.
