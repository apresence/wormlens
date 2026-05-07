# Token economics: the true cost of compact

The headline economic claim for wormlens is that mechanical extraction
is strictly cheaper than native compact across every cost layer that
matters. This doc is the long-form backing.

## Pricing reference

Per million tokens, USD. Source:
https://docs.claude.com/en/docs/about-claude/pricing.

| Model | Standard input (new prefill) | Cache read (kv-cached prefill) | Output (generation) |
|---|---|---|---|
| Opus 4.7 | $5.00 | $0.50 | $25.00 |
| Sonnet 4.6 | $3.00 | $0.30 | $15.00 |
| Haiku 4.5 | $1.00 | $0.10 | $5.00 |

Cache write multipliers, relative to standard input:

- Cache read: 0.1x
- Cache write, 5-minute TTL: 1.25x
- Cache write, 1-hour TTL: 2x

The first time a prefix is processed it costs slightly more than a
fresh prefill (the write premium); every subsequent hit drops to 10%
of base. A 5-minute cache pays off after one hit; a 1-hour cache after
two.

The two ratios that drive the rest of the analysis:

1. **Output is 5x input.** Generating 1K tokens of summary on Opus
   costs the same as prefilling 5K tokens of context.
2. **Cache read is 0.1x input.** A KV-cached prefix is essentially
   free on reuse.

## 1. Inference cost (the obvious one)

Compact requires the model to generate a summary -- output tokens at
full inference price. The summary itself is typically 500-2000 tokens
of output, but it is not unusual for an Opus session to land at 40K
tokens of summary residue when the underlying conversation was rich
(see swag math below).

Wormlens extraction: zero inference. Mechanical text processing. The
cost is CPU-milliseconds.

## 2. Prefill cost (the hidden one)

After compact clears context, the summary must be replayed as input to
the fresh context window. Input/prefill tokens are cheap relative to
output -- but the compact summary is typically *larger* than a
wormlens extract of the same conversation because:

- A degraded model writes verbose summaries (hedging, repetition,
  filler).
- A mechanical extract strips everything that isn't user/assistant
  signal.
- Measured: wormlens extracts run ~10:1 compression vs raw JSONL;
  compact summaries are typically 3-5:1.

So compact pays full output-token price to generate a larger artifact,
then pays prefill price to replay it. Wormlens pays zero to generate a
smaller artifact, then pays less prefill to replay it.

## Per-boundary cost in real money (Opus 4.7, swag numbers)

Observed empirical figures from a 200K Opus session:

- **Compact residue: ~40K tokens (~20% of the 200K window)** lands as
  the fresh context's seed.
- **Wormlens recall: ~12K tokens (~6% of the 200K window)** lands as
  the fresh context's seed -- and that's an upper bound; agent-driven
  recall often slices smaller (see `agent-agency.md`).

Plus: CC reserves another ~25% of the window as buffer for the *next*
auto-compact. So a compacted session is sitting on ~20% summary +
~25% reserve = **~45% committed** before the agent does any work.
Wormlens has no such reserve requirement (handoff is opt-in, not
pressure-triggered), so post-recall sessions are at ~6% committed.
**Working room: ~55% (compact) vs ~94% (wormlens).**

| Layer | Compact (40K summary) | Wormlens (12K extract) |
|---|---|---|
| Generation (output rate, $25/M) | 40K x $25/M = **$1.00** | $0 (mechanical) |
| First prefill + cache write 5-min (1.25x x $5/M) | 40K x $6.25/M = $0.25 | 12K x $6.25/M = $0.075 |
| Each cache-hit replay (0.1x x $5/M) | 40K x $0.50/M = $0.020 | 12K x $0.50/M = $0.006 |
| **First-boundary total** | **$1.25** | **$0.075** |

**~17x cost ratio per boundary** before any of the layers below.

In a continued workflow, every cache expiry pays the write-rate again,
and the compact summary's larger size compounds the gap on every
cycle. "Bookended" cost: compact sits between summary residue
(forward) and reserve buffer (forward), eating the budget from both
sides.

These numbers are swag-grade pending formal measurement, but the
direction and order-of-magnitude are correct.

## Model-tier coupling: compact pays at session-model rate

Native compact runs on whatever model the session is running on. An
Opus session pays Opus output rate ($25/M) to generate the summary. A
Sonnet session pays Sonnet rate ($15/M). The user does not get to
choose a cheaper model for the summary step.

Wormlens has no model in the extract path. Mechanical text processing.
If a downstream consumer wants a *summary* of the wl extract -- for
example, a Haiku-backed L1 cache subagent that holds 100K of episodic
memory and answers "did we decide X" queries on demand -- the user
chooses the model. Haiku output is $5/M, 5x cheaper than Opus.

The asymmetry is the win. Compact forces you to use the most expensive
tier you have for the most expendable artifact (a summary of stuff you
already paid for). Wormlens decouples the artifact from the model that
ever touches it. You can run an Opus session, extract mechanically,
hand the extract to Haiku for cache-and-query, and never re-pay Opus
rates for the parts of memory that don't need Opus quality.

## Cache eviction reality

Anthropic publishes 5-minute and 1-hour TTLs for prompt caching. In
practice, sessions returning to a project after extended absence
(observed: 12-hour gap) are reported by CC as "you will pay full
prefill rate for this turn" -- meaning the cache has been evicted and
the next prefill incurs the write penalty (1.25x at 5-minute, 2x at
1-hour) again.

This compounds the boundary cost asymmetry. A compact summary at 40K
tokens that gets evicted between sessions re-pays cache-write
(40K x $6.25/M = $0.25 on Opus) every time. A wl extract at 12K
re-pays $0.075. The differential is the same ~3.3x ratio per
re-prefill, multiplied by however many sessions span a cache eviction
boundary.

(Cache eviction beyond the published TTLs is not officially documented
to our knowledge. Worth empirical measurement; flagged as v0.2
research item.)

## 3. Degradation cost (the insidious one)

Compact triggers when context is nearly full -- the model is operating
in the degraded final 10-20% of its context window. The summary is
written by the worst version of the model in the session. This
degraded summary then occupies position zero of the fresh context --
the highest-attention slot.

The result: degradation is laundered forward. Each compact cycle
injects a low-quality seed into a fresh context. The model trusts its
own summary because it's in the system prompt position. It cannot
distinguish "this is a good summary I wrote while sharp" from "this
is a confused summary I wrote while dying."

Wormlens extraction is model-state-independent. A healthy model and a
fully degraded model produce identical extracts from the same JSONL
because the model is not involved.

## 4. Waste token cost (the invisible one)

The final turns before compact triggers are produced by a degraded
model. These turns contain:

- Lower quality code edits (more reverts, more regressions)
- Confused reasoning (circular logic, contradictions)
- Wasted tool calls (re-reading files, re-running tests without
  changes)

All of these consume tokens at full price. The developer pays for
output that is actively harmful.

Wormlens mitigates this by enabling earlier handoff. An agent-driven
handoff at 85% context avoids the worst degradation zone entirely. The
developer never pays for degraded output because the session ends
before the model enters the danger zone.

## 5. Developer flow state cost (the most expensive one)

When compact triggers:

- The developer is blocked for 3-5 minutes.
- Development flow state is broken.
- Context-switching cost to re-orient after compact completes: 5-30
  minutes for neurotypical developers, potentially 60+ minutes for
  developers with ADHD or similar attention-regulation differences.
- The developer cannot predict when compact will trigger, creating
  ambient anxiety about whether the current task will complete before
  interruption.

Wormlens handoff:

- Takes ~15 seconds (extract + restart).
- Agent-driven: the agent chooses a clean stopping point (commit
  pushed, tests passing).
- Predictable: the agent and developer both see context budget via
  status monitoring.
- No surprise interruption: handoff is a planned transition, not an
  emergency.

**Cost estimate:** Senior developer at $100/hour.

- Compact: 3 compacts/session x 5 min blocked + 15 min recovery = 60
  min/session = $100.
- Wormlens: 3 handoffs/session x 15 sec + 0 recovery = ~1 min/session
  = $1.67.

At organizational scale (100 developers, daily usage): compact costs
~$10,000/day in developer time. Wormlens costs ~$167/day. The
difference is $3.5M/year.

These numbers are rough but directionally correct. Even at half this
estimate, the developer flow state cost dwarfs all token costs
combined.

## Summary

| Cost Layer | Compact | Wormlens |
|---|---|---|
| 1. Extraction inference | High (output tokens) | Zero |
| 2. Prefill replay | Higher (verbose summary) | Lower (compressed extract) |
| 3. Degradation laundering | Corrupted seed in fresh ctx | Clean, model-independent |
| 4. Waste tokens in danger zone | Pays for degraded output | Avoids danger zone entirely |
| 5. Developer flow state | $100/session (blocked + recovery) | $1.67/session |
| **Total** | **Expensive + harmful** | **Cheap + clean** |

The fundamental inversion: compact converts a cheap operation
(replaying context as input) into an expensive one (generating a
summary as output), then compounds it with degradation, waste, and
developer time loss. Wormlens keeps everything in the cheap lane --
mechanical extraction, minimal prefill, no inference, no degradation,
no blocked developer.

## Related

- [agent-agency.md](agent-agency.md) -- why agent-driven memory wins;
  how wormlens lets the agent decide *whether* to recall, *what* to
  recall, and *when* to hand off.
