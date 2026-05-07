# Agent agency: tools, not curation

## The principle

Give agents tools and information. Then get out of the way.

Wormlens does not manage agent memory. It gives agents the ability to
manage their own. The distinction matters architecturally, economically,
and philosophically.

## The spectrum

Memory management frameworks for LLM agents fall on a spectrum of agency:

```
Framework-driven <----------------------------> Agent-driven

MemGPT          RAG pipelines          Wormlens
"I decide what   "We retrieve what      "Here are your tools.
you remember"    we think you need"     You decide."
```

**Framework-driven (MemGPT model):** the framework maintains a memory
hierarchy, decides what to page in and out, manages the context window.
The agent is a passenger -- it receives curated context and works with
what it's given. The framework is the bottleneck: it can only be as
smart as its retrieval logic, which is frozen at deployment time.

**Agent-driven (wormlens model):** the agent has telemetry, tools, and
discretion. It decides.

## What the agent sees

Wormlens injects two pieces of telemetry into every agent turn (via the
hook layer):

- **`context_used_pct`** -- the authoritative percentage of context
  window consumed, written by the StatusLine hook. Not a guess, not a
  token-math estimate the model attempts on its own.
- **`time`** -- current local time with UTC offset and day-of-week.

The injection costs roughly 10 tokens per turn. That is not zero, and
it is also not free of trade-offs -- those tokens come out of the
agent's working budget. The exchange is deliberate: 10 tokens of
telemetry buy the awareness the agent needs to make every other
decision in this doc. Without telemetry, the agent has no idea how
close to the wall it is, and "agent-driven" collapses to "agent
guessing."

Burn rate is observable from the per-turn delta. An agent at 50% usage
that watched itself jump from 30% in the last 10 turns can recognize a
rapid burn (heavy tool use, large reads, verbose output) and course
correct: tighten its tool calls, summarize before reading more,
checkpoint and hand off, or just spend less.

Plus the wormlens CLI surface:

- `wl --recall --stats --session <UUID>` -- how much memory exists, how
  large it is, what's in it.
- `wl --recall --index N-M --session <UUID>` -- pull exactly the slice
  the agent wants.
- `wl --list-sessions` and `wl --grep` -- find episodes by content.
- `<wl-checkpoint>` and `<wl-summary>` patterns -- breadcrumbs the agent
  leaves for itself in future sessions.
- `wl --handoff` -- emit a sentinel the harness picks up to start a
  clean continuation session.

The combination is the point. Telemetry tells the agent *what state it
is in*. Tools let it *act on that state*.

## Agency is a state-aware decision

A self-aware agent at any moment can:

- See its own context usage (e.g. 50%).
- See its own burn rate across recent turns (computed from the per-turn
  ctx delta).
- Look at the work in progress and judge whether the cruft outweighs the
  remaining runway.
- Decide what to do about it.

The decision space is rich:

- **Burn it down.** Push to 99%, finish this thread, then hand off
  cleanly with a wl extract for the next session.
- **Take a compact this pass.** If the immediate task is small and a
  native compact will preserve enough, accept the cost.
- **Pre-emptive fresh start.** At 50% with too much cruft (failed
  experiments, stale exploration), recognize that the noise hurts more
  than the saved budget helps. Hand off now, recall only the decisions
  that actually mattered, leave the cruft behind.
- **Mix.** Compact for the short term, wl for the long term. Or wl for
  the current session, compact for a side-quest. The agent picks.
- **Or do nothing.** A pure coding task with a clear plan and the source
  of truth in the file system: no recall, no handoff, just keep working.

A framework cannot make these calls because it does not know what the
agent is working on, what the agent has tried, or what the agent has
already decided. The agent does. Wormlens makes that decision tractable
by surfacing the telemetry the agent needs.

## Recall is optional

The most underappreciated property: an agent that doesn't need recall
should skip it. Pure coding tasks with a clear plan, an active branch,
and tests that say "good" don't need yesterday's conversation. A smart
agent says "I have everything I need from the code; recalling would
just burn budget" and gets to work.

Frameworks that page memory in by default cannot do this. A wormlens
agent can:

```
1. Check `wl --recall --stats --session <prior_uuid>`: "10K turns,
   34K tokens estimated."
2. Decide based on the current task: "I'm refactoring the parser. The
   parser source is the source of truth, not the chat log. Skip
   recall."
3. Or: "I'm continuing a debugging thread that lived only in
   conversation -- recall the last 50 turns from that session."
```

The cheapest call is the one not made.

## Recall is granular

When the agent does choose to recall, it does not have to firehose. The
flow is:

1. **Stats first.** `wl --recall --stats --session <UUID>` reports the
   extract's size in tokens and a summary of what's in it.
2. **Slice second.** Based on the task, the agent picks an `--index`
   range, a `--grep` pattern, or `-t N` for the tail. Pulls exactly
   those turns.
3. **Iterate if needed.** Recall again with a different slice. Each
   call is a fresh, targeted query against the same backing extract.

The KV-cached pricing makes this cheap by design: the first slice pays
write rate, subsequent slices against the same session pay 0.1x cache
read. The agent can poke around without bleeding budget.

## Why agent-driven wins

**1. Scales with model intelligence.** As models get smarter, they make
better memory decisions without any framework changes. A
framework-driven system improves only when its retrieval logic is
updated. An agent-driven system improves every time the underlying
model improves.

**2. Task-adaptive.** The agent knows what it's working on right now. A
framework doesn't. When implementing pub/sub, the agent knows to recall
the threading model decision. A framework guesses based on embeddings
or keywords.

**3. Composable.** Wormlens doesn't know about any context-budget tool
or any DECISIONS.md the agent maintains. None of those need to know
about wormlens. The agent composes them: "I have 15 minutes before
standup, 60% context remaining, and I need the auth module decisions
from session 3. Pull 20 turns from session 3, skip the rest, finish
this PR."

**4. Debuggable.** When something goes wrong, you can see exactly what
the agent requested and why. Framework-driven systems fail silently --
the wrong context gets paged in and you can't tell why the agent went
off the rails.

**5. Minimal infrastructure.** Wormlens is grep, format, and a status
file. No vector databases, no embedding models, no retrieval pipelines,
no paging logic. The complexity lives in the model, which is already
there.

## The tradeoff

Agent-driven memory requires a capable model. A weak model will make
poor recall decisions -- pulling too much, too little, or the wrong
turns. For current frontier models (Claude 4.7, GPT-4o-class,
Qwen-72B+), this is not a constraint. For smaller models (3-7B), a
framework-driven approach may still be necessary.

This tradeoff resolves itself over time. A model that isn't great at
memory decisions today can be trained to be better, fine-tuned on
examples of good recall judgment, or simply replaced by a stronger
successor. The wormlens API surface stays the same; the agent's policy
improves. Framework-driven systems have to retrofit the agent layer
with each generation; agent-driven systems just upgrade the model.

Wormlens is designed for the models we have now and the better models
coming next -- not for the models we had two years ago.

## Related

- [token-economics.md](token-economics.md) -- the dollars-and-cents
  backing for "agent-driven is cheaper": fewer recalls, smaller slices,
  no framework overhead, no degraded-model summaries.
