---
name: wormlens
description: 'Lossless session continuity. USE WHEN: context monitor shows >80% fill, or when you want to check context status. Provides episodic memory across session handoffs with zero context loss. Check .wormlens/status for current context fill %. When approaching 85-90%, initiate a handoff: extract session with wl, create .wormlens/handoff_ready marker, and the outer loop will restart you with full recall.'
argument-hint: 'e.g. "check context status", "initiate handoff", "how much context is left"'
---

# wormlens — Lossless Session Continuity

You are running inside a wormlens session loop. This means:

1. **You have infinite effective context.** When your context fills up, you hand off to a fresh instance of yourself with complete episodic memory of everything that happened.

2. **Compaction is OFF.** There is no lossy compaction. Your context will not be silently summarized and compressed by the system. You use your context until you decide to hand off.

3. **A context monitor is running.** Check your context fill level anytime:
   ```bash
   cat .wormlens/status
   ```
   This returns JSON with `context_pct`, `context_remaining`, and `level` (OK/CAUTION/WARNING/HANDOFF_NOW/CRITICAL).

4. **You decide when to hand off.** Not the system, not a timer — you. The right time is when:
   - Context is at 85-90%
   - You're at a clean stopping point (commit pushed, test passing, etc.)
   - NOT in the middle of a multi-step operation

## Handoff Protocol

When you decide to hand off:

```bash
# 1. Extract your session memory
python -m wormlens --full --format chat -o .wormlens/extracts/session_N.md

# 2. Signal the outer loop
touch .wormlens/handoff_ready

# 3. Tell the user what's pending
# (your message will be in the next session's extract)
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
| 90-95% | CRITICAL | Extract immediately, hand off |
| 95%+ | FORCED | Outer loop will force-kill and extract |

## Checking Context Status

Quick check:
```bash
cat .wormlens/ctx_pct
```
Returns just the percentage number.

Full status:
```bash
cat .wormlens/status
```
Returns JSON with all details.

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

- **DON'T** wait until 95% to hand off. You'll be mid-operation and the forced kill will be messy.
- **DON'T** try to compact manually. That's what wormlens replaces.
- **DON'T** ignore the context monitor. Check it after completing each major task.
- **DON'T** repeat work from the extract. You already did it. Trust your memory.
