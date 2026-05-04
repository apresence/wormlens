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
wl                          # latest CC session, recovery mode (since last compact)
wl --full                   # full session (ignore compacts)
wl -t 20                    # last 20 messages
wl --source vscode          # latest VS Code Copilot session
wl --list-sessions          # list available sessions
wl --session <UUID>         # extract specific session
wl --grep "pattern"         # search all sessions
wl --grep "pattern" -i -B2 -A2  # case-insensitive with context
wl --index 5-10             # extract specific turns
wl --summary-stats          # session statistics
wl --doctor                 # diagnose environment issues
```

## Key Flags

- `--all` include everything (thinking, tools, hooks, bash, system msgs)
- `--thinking` / `--tools` / `--bash` include specific types
- `--format chat|md|txt|jsonl` output format (default: chat)
- `-o FILE` write to file instead of stdout
- `--merge` combine multiple input files

## Output

Default chat format uses `<user turn=N>` / `<assistant turn=N>` tags. For CC sessions, turn=N is the JSONL line number -- use `sed -n 'Np' <source.jsonl>` for full-fidelity retrieval of any turn.

Run `wl --help` for complete option reference.
