# Compact Cost: Measured Numbers

Empirical results from running `wl --analyze-compacts` (currently a
local script; productized for v0.2) over the maintainer's session
JSONLs.

**Methodology:** read every CC session JSONL on disk, filter to
sessions >=100 KB and exclude spawned subagent transcripts
(`agent-*.jsonl`). For every `system / compact_boundary` record,
compute the per-compact metrics from `compactMetadata` plus the
neighboring assistant turns. Dedup by `(session_id,
compact_idx_in_session)` since the maintainer's archive holds the
same JSONLs across multiple hosts and backup dirs.

**Sample:** n=49 compacts in 24 unique sessions, spanning CC versions
2.1.49 through 2.1.128, models claude-opus-4-6/4-7, claude-sonnet-4-6.
n=7 of those 49 carry direct `compactMetadata.postTokens`; the other
42 use a derived estimator (`cache_creation_input_tokens` of the
first post-compact assistant turn, minus the session's
fresh-baseline `cache_creation`). The derived estimator overshoots
direct postTokens by a median of +22.6% because the post-compact
turn's cache_creation includes the post-compact user message and any
new tool definitions discovered since session start, not just the
summary.

**Caveats:**

1. Crew-internal corpus, not a random sample of the CC user
   population. Workflows are dev-heavy.
2. Summary generation is charged at `cache_read_rate` ($0.50/M for
   Opus), assuming the summary call benefits from the same
   KV-cache prefix that interactive turns use. If CC issues an
   un-cached `messages.create` for the summary, gen costs are 10x
   higher (median Opus gen $0.38 -> $3.81). Indirect evidence
   (`cache_read_input_tokens` is preserved across the boundary)
   supports the cache-read assumption, but it's not directly
   verified.
3. No per-record `cost` field exists in any CC session JSONL we
   examined. All dollar figures are derived from token counts at
   the rates published in `docs/token-economics.md`.

## Headline distributions (deduped, n=49 compacts)

| metric                       | n  | median  | p25     | p75     | p95     |
|------------------------------|----|---------|---------|---------|---------|
| `pre_tokens`                 | 49 | 167,286 | 167,050 | 168,182 | 171,715 |
| `summary_tokens`             | 49 |  11,694 |   7,215 |  15,960 |  20,392 |
| `ctx_at_trigger_pct`         | 49 |   83.6% |   83.5% |   84.1% |   85.9% |
| `waste_zone_pct`             | 49 |   16.4% |   15.9% |   16.5% |   24.1% |
| `post_compact_residue_pct`   | 49 |    5.9% |    3.6% |    8.0% |   10.2% |
| `generation_cost_usd`        | 49 |  $0.337 |  $0.226 |  $0.465 |  $0.593 |
| `cache_write_cost_usd`       | 49 |  $0.064 |  $0.044 |  $0.095 |  $0.127 |
| `prefill_cost_per_turn_usd`  | 49 |  $0.005 |  $0.004 |  $0.008 |  $0.010 |
| `duration_ms`                |  7 |  89,189 |  84,110 | 105,942 | 172,458 |

## Direct-postTokens subset (n=7, the cleanest sample)

| metric                       |  median  | mean    |
|------------------------------|---------:|--------:|
| `summary_tokens`             |    7,747 |   7,623 |
| `post_compact_residue_pct`   |     3.5% |    3.8% |
| `generation_cost_usd` (Opus) |   $0.260 |  $0.191 |

Auto-trigger only, direct-postTokens (n=5): median residue
**3.87%**, median summary 7,747 tokens, median Opus gen
**$0.260**.

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
| median compact share  |   1.0%  |

Compact-summary generation is a small slice of total session spend
(typically 0.5-2%). The much bigger ongoing cost is the per-turn
**carrying cost** of the summary in the post-compact context: at
median 11.7K tokens at the Opus cache_read rate ($0.50/M), every
post-compact turn pays roughly **$0.0058 just to keep the summary
in scope**, multiplied by however many turns the session continues.

## What the README hypotheticals got wrong

| Quantity                                | README hypothetical | Measured (median, deduped) |
|-----------------------------------------|--------------------:|---------------------------:|
| ctx at trigger                          |             ~80-85% |                      83.6% |
| post-compact residue (% of window)      |                ~20% |                       5.9% |
| summary gen cost (Opus)                 |               ~$1.0 |                     $0.34  |
| per-turn carry cost (Opus)              |               ~$0.02|                     $0.005 |

Order of magnitude on `ctx_at_trigger` is right. The summary
residue and per-compact dollar cost were both overstated by
roughly 3x. Direction of the asymmetry vs. wormlens is unchanged
(compact still pays output rate to generate a summary; wormlens
extracts mechanically), but the absolute magnitude is smaller
than the original numbers suggested.

## Reproduce

The script and per-row CSV are gitignored under `.copilot/` for
the maintainer's local copy. v0.2's `wl --analyze-compacts`
mode will let any user run the same analysis on their own
corpus.
