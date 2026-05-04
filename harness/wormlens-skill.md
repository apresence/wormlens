---
name: wormlens
description: 'Lossless session continuity. USE WHEN: context is filling up or you need to hand off. Provides episodic memory across session handoffs with zero context loss. Context percentages and time are injected into your system-reminders by the hook -- trust them. When approaching 85-90%, initiate a handoff: write <wl-summary> tag then call wl --handoff --session <id>. When URGENT appears, comply immediately.'
argument-hint: 'e.g. "check context status", "initiate handoff", "how much context is left"'
---

# wormlens -- Lossless Session Continuity

You are running inside a wormlens session loop. This means:

1. **You have infinite effective context.** When your context fills up, you hand off to a fresh instance of yourself with complete episodic memory of everything that happened.

2. **Compaction is OFF.** There is no lossy compaction. Your context will not be silently summarized and compressed by the system. You use your context until you decide to hand off.

3. **Context and time are injected into your system-reminders by the hook.** Every turn and tool call, you receive:
   - `context_used_pct` / `context_remaining_pct` -- authoritative. Do not compute your own.
   - `time` -- authoritative local time with UTC offset and DOW. Never guess the date or DOW.
   - `urgent` -- appears when context is critically low (>=90%). **When URGENT appears, comply immediately.**

4. **You decide when to hand off.** Not the system, not a timer -- you. The right time is when:
   - Context is at 85-90%
   - You're at a clean stopping point (commit pushed, test passing, etc.)
   - NOT in the middle of a multi-step operation
   - Exception: when URGENT appears in system-reminder, comply immediately regardless.

## Handoff Protocol

When you decide to hand off:

1. Write `<wl-summary>short session description</wl-summary>` in your response (wl validates this -- handoff will fail without it)
2. Call `wl --handoff --session <session-id>` to signal the outer loop
3. Tell the user what's pending

```bash
wl --handoff --session <session-id>
```

The outer loop will:
- Kill this session cleanly
- Start a fresh Claude instance
- Feed it your extract as boot context
- The new instance has full recall and ~90% context free

## Context Budget Guidelines

| Fill % | Level | Action |
|--------|-------|--------|
| 0-70% | OK | Work normally |
| 70-80% | CAUTION | Be aware, plan ahead |
| 80-85% | WARNING | Finish current task, prepare to hand off |
| 85-90% | HANDOFF_NOW | Complete current operation, extract, hand off |
| 90%+ | URGENT | Hook injects URGENT directive -- comply immediately |

## What Gets Preserved

The extract captures:
- Every user message and assistant response (the decisions, the intent, the momentum)
- Turn numbers mapped to JSONL line numbers for random-access retrieval
- Session metadata (timestamps, source file paths)

What's NOT in the extract but is retrievable on-demand:
- Thinking blocks
- Tool call details
- Bash output
- Full file diffs

To retrieve full detail for any turn: use `wl --index N` or read the source JSONL directly.

## Anti-Patterns

- **DON'T** wait until URGENT to hand off. Plan ahead at 85%.
- **DON'T** ignore URGENT when it appears. The hook means it -- comply immediately.
- **DON'T** try to compact manually. That's what wormlens replaces.
- **DON'T** guess the date, time, or DOW. Trust the `time` field in your system-reminder.
- **DON'T** compute context percentages yourself. Trust `context_used_pct` from the hook.
- **DON'T** skip the `<wl-summary>` tag before calling `wl --handoff`. It will fail validation.
- **DON'T** repeat work from the extract. You already did it. Trust your memory.
