---
name: wormlens
description: 'Extract, search, and display chat history from Claude Code or VS Code Copilot sessions. USE FOR: reviewing previous conversations, pulling episodic memory from past sessions, listing available sessions, extracting specific sessions by ID, searching conversation history with regex. DO NOT USE FOR: modifying chat history, real-time chat monitoring.'
argument-hint: 'Describe what chat history you need, e.g. "last session", "list sessions", "session abc-123"'
---

# wormlens -- Lossless Episodic Memory

You have access to `wl` for extracting chat history from Claude Code and VS Code Copilot sessions.

## Invocation

```bash
wl [INPUT...] [options]           # if installed via pip
python -m wormlens [INPUT...] [options]   # module
python .copilot/wormlens.pyz [INPUT...] [options]  # zipapp
```

## Quick Reference

```bash
wl --list-sessions             # list available sessions (start here)
wl --recall --session <UUID>   # recover prior session into context (agent use)
wl --session <UUID>            # extract specific session
wl --full --session <UUID>     # full session (ignore compact boundaries)
wl -t 20 --session <UUID>      # last 20 messages of a session
wl --source vscode --list-sessions   # list VS Code Copilot sessions
wl --grep "pattern"            # search all sessions
wl --grep "pattern" -i -B2 -A2 # case-insensitive with context
wl --index 5-10 --session <UUID>     # extract specific turns
wl --summary-stats             # session statistics
wl --doctor                    # diagnose environment issues
```

Always pass `--session <UUID>` for deterministic extraction. Bare `wl`
(no args) prints help -- it does not extract anything. Use
`wl --list-sessions` to find a UUID first, then `wl --recall --session
<UUID>` for the common "give me my prior session" recovery path.

## Key Flags

- `--all` include everything (thinking, tools, hooks, bash, system msgs)
- `--thinking` / `--tools` / `--bash` include specific types
- `--format chat|md|txt|jsonl` output format (default: chat)
- `-o FILE` write to file instead of stdout
- `--merge` combine multiple input files

## Output

Default chat format uses `<user turn=N>` / `<assistant turn=N>` tags. For CC sessions, turn=N is the JSONL line number -- use `sed -n 'Np' <source.jsonl>` for full-fidelity retrieval of any turn.

Run `wl --help` for complete option reference.

## When Running Under wormlens Harness

If the wormlens harness (outer loop) is active, your system-reminders include:

- **`context_used_pct`**: percentage of context window consumed (authoritative -- do not compute your own)
- **`context_remaining_pct`**: percentage of context window remaining (authoritative)
- **`time`**: current local time with UTC offset and day of week (authoritative -- do not guess the date or DOW)

Trust these injected values. They come from CC's StatusLine hook and are accurate. Your internal date/DOW reasoning is unreliable for future dates.

When `context_remaining_pct` drops below 15%, initiate a clean handoff:
1. Finish current operation
2. Include `<wl-summary>short session description</wl-summary>` in your response
3. Call `wl --handoff --session <session-id>` (wl validates the summary tag exists)
4. Tell the user you are handing off

When `urgent` appears in your system-reminder, comply immediately -- do not wait for a clean stopping point.
