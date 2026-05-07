# wormlens

**Kill pancake brain.** Episodic memory handoff between agent sessions -- no
compact required.

Pluggable chat history extraction for Claude Code and VS Code Copilot. Reads
raw session logs and produces token-efficient, addressable extracts that
agents can consume as context -- no more lossy compacts, no more 5-minute
waits, no more drilling the wrong wall.

> Has this ever happened to you? You're happily coding with your companion
> agent, lining 'em up and knocking 'em down. Then -- BAM! Blindsided by
> compact. Agent gets pancake brain. You get an aneurysm staring at a
> spinner for 5 minutes. And then, it all goes oh so very pear shaped. 🍐

Wormlens skips the compact entirely. Mechanically extract the prior
session, hand it to the next one, keep going.

- **Extract, not compact.** Compact is for garbage. Extract is for nectar.
- **Instant** -- extracts in milliseconds, not minutes.
- **Lossless** -- user/assistant text preserved verbatim by default;
  thinking, tool calls, and bash output opt-in via flags. Nothing is
  paraphrased or reduced by a model.
- **Addressable** -- random-access by turn index. Pull a single turn
  or a slice (e.g. `--index 5-10`) from any extracted session without
  re-processing the whole thing.
- **Historical** -- chain recalls across sessions. Today's recall can
  include yesterday's, which includes the one before. Walk back as far
  as you need.
- **Agent-driven** -- the agent decides whether to recall, what to
  recall, and when to hand off. Wormlens injects authoritative
  `context_used_pct` and `time` into every turn (~10 tokens) so the
  agent has the telemetry to make those calls.
- **Unified** -- list, grep, search, summarize across providers (Claude
  Code, VS Code Copilot now; pluggable for others).

## Why it's cheap

Native compact triggers a summary-write at the session's model tier --
output-rate tokens to generate the summary, plus prefill-rate tokens
to load it into the fresh post-compact context. The session itself is
already in context (that's how compact triggered in the first place);
what's new and expensive is the generation pass at output rate.
Wormlens extraction is mechanical: **zero model tokens**.

Compact also reserves a chunk of the context window for the summary
itself, leaving the active agent fewer tokens to actually work with.

Hypothetical (200K Opus window): a wormlens recall might land at ~6%
used; a compact's summary residue at ~20%, plus another ~25% reserved
for the next auto-compact, leaves ~45% committed before any work.
**Working room: ~94% (wormlens) vs ~55% (compact).**\*

There are five cost layers (inference, prefill, degradation laundering,
waste tokens in the danger zone, and developer flow state). Wormlens
wins all five. The flow-state layer alone might run ~60x cheaper -- a
senior developer at $100/hour costs roughly $100/session in compact-
induced block + recovery vs ~$1.67/session of clean handoff (also
hypothetical until benchmarked).

See [docs/token-economics.md](docs/token-economics.md) for the
five-layer accounting with current Anthropic pricing, and
[docs/agent-agency.md](docs/agent-agency.md) for the design philosophy.

\* These percentages and dollar figures are illustrative, not
measured. They give a reader a magnitude estimate while we do the
honest work: a `wl --analyze-compacts` mode is on the punch list to
walk our own session JSONLs and produce real numbers from observed
compacts. Real data will replace the hypotheticals.

## Installation

```bash
pip install .
wl --help
```

This installs the `wl` command via the entry point defined in `pyproject.toml`.

## Usage

```bash
# Installed command
wl [INPUT...] [options]

# Module invocation
python -m wormlens [INPUT...] [options]

# Zipapp (single-file distributable)
python wormlens.pyz [INPUT...] [options]
```

## Quick Start

```bash
wl --list-sessions                   # list CC sessions (start here)
wl --list-sessions --source vscode   # list VS Code sessions
wl --recall --session <UUID>         # extract one session for agent recall
wl --session <UUID>                  # extract specific CC session
wl --session abc-123,def-456         # extract multiple sessions
wl session.jsonl                     # extract from explicit file (auto-detect source)
wl --source vscode --session <UUID>  # explicit VS Code session
wl --full --session <UUID>           # full session (ignore compact boundaries)
wl -t 20 --session <UUID>            # last 20 messages of a session
wl --index 5-10 --session <UUID>     # extract turns 5 through 10
wl --index 42 --session <UUID>       # extract a single turn
wl --grep "pattern"                  # search across all sessions
wl --format jsonl --all --session <UUID> -o full.jsonl
wl *.jsonl --merge -o merged.md      # merge explicit JSONL files
wl --summary-stats                   # show session statistics
```

Bare `wl` (no args) prints help. For extraction, always pass `--session
<UUID>` -- use `--list-sessions` to discover IDs.

## Sources

| Source | Flag | S | Auto-detect | Session Location |
|--------|------|---|-------------|------------------|
| Claude Code | `--source cc` | C | `type` + `sessionId` + `timestamp` keys | `$CLAUDE_CONFIG_DIR/projects/**/*.jsonl` |
| VS Code Copilot | `--source vscode` | V | `kind` + `v` keys | `%APPDATA%/Code/User/workspaceStorage/*/chatSessions/*.jsonl` |
| WormLens extract | `--source wl` | W | `<wormlens-extract>` or `<wl-recall-caveat>` wrapper | File input only (no discovery) |

Auto-detection examines the first record in the file. `--list-sessions` scans all providers and shows a one-character source column (S). Timestamps are UTC.

## Filtering

By default, only user and assistant messages are included. Add flags to include more:

| Flag | Content |
|------|---------|
| `--thinking` | Reasoning/thinking blocks |
| `--tools` | Tool calls and results |
| `--code-edits` | Code edit groups (VS Code) |
| `--hooks` | Hook events (CC) |
| `--bash` | Bash output (CC) |
| `--teammates` | Teammate messages (CC) |
| `--refs` | Inline references (VS Code) |
| `--system-msgs` | System-injected messages (CC: isMeta, local-command, etc.) |
| `--all` | Everything |

## Output Formats

| Format | Flag | Notes |
|--------|------|-------|
| Chat | `--format chat` (default) | Token-efficient XML-style turn wrappers, agent-optimized |
| Markdown | `--format md` | Structured with headers, turn numbers, metadata |
| Plain text | `--format txt` | Session/role markers, no formatting |
| JSONL | `--format jsonl` | One JSON record per message |

### Chat format

The default. Designed for LLM context injection -- maximum signal, minimum chrome:

```
<session id="4a97ef42-beb2-41ba-81e1-fdc3b470b58b" source="vscode" date="2026-04-30" title="Parquet to CSV">
<!-- Sequential turn numbers. Source: C:\...\4a97ef42-....jsonl -->
<user turn=1>Write a python script to convert parquet files to CSV
<assistant turn=1>pyarrow is available. Script created at `parquet2csv.py`.
<user turn=2>Is there a way to do sql-like where clause?
<assistant turn=2>Both are doable. For (b) it's trivial with pyarrow column selection.
</session>
```

**Turn numbering:** CC uses JSONL line numbers (turn=80 -> line 80 of source file for full-fidelity retrieval). VS Code uses sequential numbers.

**Escaping:** Only at start-of-line -- `\` -> `\\`, `<` -> `\<`. Mid-line `<` is untouched.

## Record Selection

| Flag | Effect |
|------|--------|
| `-n N` | Limit to N output records |
| `--rev` | Reverse: take last N (requires `-n`) |
| `-t N` / `--tail N` | Last N records (shorthand for `--rev -n N`) |
| `--newest-first` | Reverse chronological order |
| `--index SPEC` | Subaddress retrieval -- extract specific turns or ranges (e.g. `5`, `5-10`, `5,8,12`) |
| `--session ID[,ID]` | Extract specific session(s) by UUID |
| `--session-id ID` | Filter to specific sessionId within a file |
| `--min-turns N` | Minimum user+assistant turns (default: 2 for `--list-sessions`) |
| `--min-size SIZE` | Minimum file size, e.g. `10KB`, `1MB` |

## Session Noise Filtering

`--list-sessions` defaults to `--min-turns 2`, hiding throwaway sessions (someone starts Claude, checks something, exits). Override with `--min-turns 0` to see everything, or increase the threshold:

```bash
wl --list-sessions --min-turns 5         # substantial sessions only
wl --list-sessions --min-size 100KB      # filter by file size
wl --list-sessions --min-turns 0          # show all including noise
```

## System-Injected Messages

Claude Code sends certain messages as `user` role that are actually system-injected: local command output (`<local-command-stdout>`), command caveats, slash commands, etc. These are detected via the `isMeta` record flag and known XML tag patterns, and tagged as `system_inject` internally.

By default they are filtered out. Use `--system-msgs` (or `--all`) to include them.

## Recovery Mode (Claude Code)

`wl --recall --session <UUID>` operates in **recovery mode**:

1. Finds the last `compact_boundary` marker in the session file
2. Extracts only messages after that point
3. Wraps the output in `<wl-recall-caveat>` tags so the consuming agent
   recognizes it as recovered episodic memory, not live conversation

Use `--full` to extract the whole session file regardless of compact
boundaries.

## VS Code State Reconstruction

VS Code Copilot stores chat sessions as an incremental patch stream (kind 0=snapshot, 1=set, 2=splice). The backend replays the full patch sequence to reconstruct final session state before extracting messages.

## Searching Chat History

```bash
wl --grep "pattern"                      # search all sessions, all sources
wl --grep "pattern" -i                   # case-insensitive
wl --grep "pattern" -B 2 -A 2           # with context messages
wl --grep "pattern" --source cc          # search specific source
```

## Building the Zipapp

```bash
python3 build_pyz.py
# Output: .copilot/wormlens.pyz
```

Produces a single-file `wormlens.pyz` that can be distributed and run with `python wormlens.pyz`. No dependencies beyond the standard library.

## Architecture

The repo uses a flat layout: the project root **is** the `wormlens` package
(via `[tool.setuptools.package-dir]` mapping `"wormlens" = "."`). Modules like
`cli.py`, `pipeline.py`, etc. live at the project root, not in a nested
`wormlens/` subdirectory.

```
wormlens/                  (project root = python package)
  __init__.py              # Package version
  __main__.py              # python -m entry point
  cli.py                   # Argument parsing, orchestration
  models.py                # ChatMessage, ChatSession, FilterOpts
  pipeline.py              # discover -> parse -> filter -> sort
  formatters.py            # md/txt/jsonl output
  build_pyz.py             # Zipapp builder
  skill.md                 # Skill manifest (also bundled in package)
  pyproject.toml
  README.md
  LICENSE
  AGENTS.md                # Instructions for AI agents working in this repo
  CHANGELOG.md
  tests/                   # pytest suite (see "Running tests")
  harness/
    __init__.py
    wormlens.py            # Outer loop (wl launch)
    wl-hook.py             # StatusLine + context injection hook
  providers/
    __init__.py            # Auto-discovery registry
    _base.py               # Provider ABC
    claude_code/parser.py
    vscode_copilot/parser.py
    wl_extract/parser.py
```

## Diagnostics

```bash
wl --doctor
```

Checks provider availability, session directory paths, file permissions, and configuration health. Run this first when something is not working.

## Session Continuity (Outer Loop)

`wl launch` runs the wormlens harness -- an outer loop that manages CC's lifecycle
for infinite session continuity. When the agent reaches context limits, the harness
restarts CC with episodic recall from the prior session.

```bash
wl launch                                # interactive, no initial prompt
wl launch --prompt "build a redis server" # start with a task
wl launch --ctx-limit 85 --hard-kill 95  # tighter thresholds
wl launch --grace 30                     # shorter grace period before kill
wl launch --project-dir /path/to/repo    # explicit project dir
```

| Flag | Default | Effect |
|------|---------|--------|
| `--prompt` | none | Initial task prompt for the CC session |
| `--ctx-limit` | 90 | Context %% at which URGENT is injected |
| `--hard-kill` | 99 | Context %% at which to force kill |
| `--grace` | 60 | Seconds after URGENT before forced handoff |
| `--poll-interval` | 2.0 | Poll interval for context/handoff checks |
| `--project-dir` | cwd | Project directory for trust dialog |

The harness requires the wormlens skill to be installed (`wl --install-skill`) so
that context tracking hooks are active.

For debugging, the harness can also be run standalone:

```bash
python3 -m wormlens.harness.wormlens --prompt "echo hi"
```

## Running tests

```bash
pip install -e .[dev]
pytest
```

The suite (`tests/`) covers CLI argparse, JSONL parser edge cases, formatter
output shape, settings.json merge/unmerge, skill install/uninstall, recall and
handoff gating, checkpoint extraction, and the .wl round-trip. All fixtures are
synthetic ASCII files under `tests/fixtures/` and `tmp_path` -- nothing touches
your real `~/.claude` tree.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes.

## See also

- **Design notes**:
  - [docs/agent-agency.md](docs/agent-agency.md) -- why agent-driven
    memory wins; how telemetry + tools beat framework-curated context.
  - [docs/token-economics.md](docs/token-economics.md) -- five-layer
    cost analysis of compact vs. wormlens with current Anthropic
    pricing.
- **[spad-mcp](https://github.com/apresence/spad-mcp)** -- the
  autonomous, agent-controlled SSH harness we use for wormlens
  development. Two roles in the dev cycle:
  - **Dev / test / debug**: specs in, fully-tested ready-to-ship out.
    An agent installs wormlens, verifies the skill loads and hooks
    fire, exercises the outer-loop restart on handoff -- including
    the Claude-extension scaffolding (skill packaging, hook wiring,
    settings.json merge). Bugs kick back to a human; clean runs ship.
    Generalizes to other agent tools beyond CC.
  - **Benchmarks**: agent-as-proctor + agent-as-testee, fully
    autonomous across the comparison matrix (compact-only,
    wl+compact, wl-only, fresh-start). Real workloads, real numbers,
    no wetware.

  Despite urgency to ship wormlens, the debug cadence was too slow
  with humans in the loop and fair, consistent benchmarks were
  impractical without an autonomous runner. So we paused wormlens
  and pivoted to spad-mcp -- we needed it to properly test and
  finish wormlens at a reasonable pace. Dogfooding: spad runs long
  unattended sessions; wormlens keeps them coherent.

## Known Limitations

- VS Code splice reconstruction handles inserts and deletes but the `d` (deleteCount) key format is inferred from VS Code's source -- edge cases may exist
