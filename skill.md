---
name: wormlens
description: 'Extract, search, and display chat history from Claude Code or VS Code Copilot sessions. USE FOR: reviewing previous conversations, pulling episodic memory from past sessions, listing available sessions, extracting specific sessions by ID, searching conversation history with regex. Supports filtering by message type (thinking, tools, hooks, bash, system-injected). The wl format provides addressable turn numbers (JSONL line numbers for CC) for random access to full-fidelity source. DO NOT USE FOR: modifying chat history, real-time chat monitoring.'
argument-hint: 'Describe what chat history you need, e.g. "last session", "list sessions", "session abc-123"'
---

# wormlens -- Lossless Episodic Memory Skill

You have access to the `wl` CLI tool for extracting chat history from Claude Code and VS Code Copilot sessions.

## Invocation

From the project root:
```bash
python -m wormlens [INPUT...] [options]
```

If a `wormlens.pyz` exists in `.copilot/`:
```bash
python .copilot/wormlens.pyz [INPUT...] [options]
```

## Common Patterns

### List available sessions
```bash
# Claude Code sessions (default, filters noise with --min-turns 2)
wl --list-sessions

# All sessions including noise
wl --list-sessions --min-turns 0

# VS Code Copilot sessions
wl --list-sessions --source vscode

# Only substantial sessions
wl --list-sessions --min-turns 5
wl --list-sessions --min-size 100KB
```

### Extract latest session
```bash
# Recovery mode (from last compact boundary) -- default
wl

# Full session
wl --full

# Latest VS Code session
wl --source vscode
```

### Extract specific session(s)
```bash
# Full UUID or prefix -- prefix matching is supported
wl --session <UUID>
wl --session abc123,def456
```

### Tail recent messages
```bash
# Last 20 messages
wl -t 20

# Last 10 messages with tool calls
wl -t 10 --tools

# Last 50 messages with everything
wl -t 50 --all
```

### Subaddress specific turns
```bash
# Extract specific turn(s) by index
wl --index 42
wl --index 5-10
wl --index 5,8,12

# Session summary statistics
wl --summary-stats
```

### Output to file
```bash
wl -o history.md
wl --format jsonl -o export.jsonl
wl --format txt -o plain.txt
```

## Source Auto-Detection

| Source | Flag | Detection |
|--------|------|-----------|
| Claude Code | `--source cc` (default) | `type` + `sessionId` + `timestamp` keys |
| VS Code Copilot | `--source vscode` | `kind` + `v` keys |

When given a file path, auto-detects the source from file contents.

## Filtering Flags

By default only user and assistant messages are shown. Add flags for more:

| Flag | Content |
|------|---------|
| `--thinking` | Reasoning/thinking blocks |
| `--tools` | Tool calls and results |
| `--hooks` | Hook events (CC) |
| `--bash` | Bash output (CC) |
| `--code-edits` | Code edit groups (VS Code) |
| `--teammates` | Teammate messages (CC) |
| `--refs` | Inline references (VS Code) |
| `--system-msgs` | System-injected messages (CC: local-command, slash commands) |
| `--compact-markers` | Compact boundary markers (CC) |
| `--all` | Everything |

## Output Formats

- `--format chat` (default): Compact XML-style tags, minimal token overhead. Turn numbers are JSONL line numbers (CC) or sequential (VS Code). Use for LLM context injection and episodic memory.
- `--format md`: Structured markdown with turn numbers, session metadata blocks. Use for human reading.
- `--format txt`: Plain text with role markers
- `--format jsonl`: One JSON record per message

All output is wrapped in `<wormlens-extract>` / `</wormlens-extract>` bookend tags. Content is scrubbed before output. These tags let downstream consumers identify and delimit wormlens extracts cleanly.

### Chat format and line-number indexing

The `chat` format emits addressable turn numbers:
- **CC sessions**: `turn=N` is the 1-based JSONL line number. To retrieve full context (tool calls, thinking, code) for a specific turn, read line N from the source file.
- **VS Code sessions**: `turn=N` is sequential (JSONL format differs).

Each session tag includes a comment with the source file path and index scheme:
```
<session id="abc-123" source="claude_code" date="2026-05-01">
<!-- turn = JSONL line number. /path/to/abc-123.jsonl -->
<user turn=42>What about the edge case?
<assistant turn=45>The fix is...
</session>
```

To get full detail for turn 42: `sed -n '42p' /path/to/abc-123.jsonl | python -m json.tool`

## Searching Chat History

```bash
# Search all sessions across all sources (CC + VS Code)
wl --grep "pattern"

# Case-insensitive
wl --grep "pattern" -i

# With context (N messages before/after each match)
wl --grep "pattern" -B 2 -A 2

# Search specific source only
wl --grep "pattern" --source cc
```

Grep searches all message types (user, assistant, tool output, thinking, bash) across all sessions. Results show session ID prefix, source, date, and matching lines with highlights.

## Important Notes

- Output goes to **stdout** by default. Use `-o file` for file output.
- Status/progress messages go to **stderr**, so they don't pollute output.
- Recovery mode (default for CC with no input) extracts only messages since the last compact boundary -- this gives the "current conversation" window.
- Use `--full` to get the entire session file.
- `--list-sessions` defaults to `--min-turns 2` to hide throwaway/noise sessions. Use `--min-turns 0` to see all.
- Run `wl --doctor` to diagnose provider availability, session paths, and configuration issues.
