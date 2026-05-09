# Changelog

All notable changes to wormlens are documented in this file. The format
is loosely based on Keep a Changelog, and the project adheres to
Semantic Versioning.

## [Unreleased]

### Security

- **Removed `--dangerously-skip-permissions` from the harness's hardcoded
  `claude` invocation.** This was a smoke-test artifact that leaked into
  shipped code; users running `wl launch` were silently getting a CC
  instance with all tool prompts disabled. If a user genuinely wants this
  behavior, they can pass it themselves via the new `--` passthrough
  (and own the risk).
- **Removed automatic `hasTrustDialogAccepted = true` write to
  `~/.claude.json`.** This was another smoke-test artifact: the harness
  was silently editing the user's CC config to pre-accept trust prompts
  for the project directory. The corresponding `--project-dir` flag and
  the `ensure_trust_dialog()` function are removed entirely. Users are
  now prompted by CC for project trust the normal way; this is the
  expected default and matches CC behavior outside the harness.

### Added

- **`--` passthrough to `claude`.** Anything after a literal `--` on the
  `wl launch` command line is forwarded to the `claude` binary verbatim
  (e.g. `wl launch --prompt 'build X' -- --model claude-opus-4-7
  --append-system-prompt 'be terse'`). If `--session-id` is passed via
  passthru, the harness uses that UUID for its own session tracking
  instead of generating one (and does not double-add the flag to the
  launch command).
- **OpenAI Codex CLI provider** (`--source codex`, source label `X`).
  Reads rollout JSONL files from `$CODEX_HOME/sessions/YYYY/MM/DD/`
  (default `~/.codex/`). Auto-detects via the first record's
  `type=session_meta` plus `id` and `cli_version` fields. Handles all
  canonical `response_item` content (message, reasoning,
  function_call, function_call_output, web_search_call, custom tool
  calls), the `compacted` summary record (with `since_last_compact`
  recall slicing analogous to CC's compact_boundary), and the
  in-place append behavior of `codex exec resume --last`. MCP tool
  calls surface with `namespace` metadata so consumers can identify
  the originating server. Synthetic role=developer permissions and
  role=user `<environment_context>` injections are filtered by
  default; surface them with `--system-msgs`.
- Codex CLI added to README providers table, quick-start examples,
  and source-character registry (`X`).

### Documentation

- Compact behavior measured (n=43 summaries in 24 sessions, deduped,
  CC 2.1.49--2.1.128, opus-4-6/4-7 + sonnet-4-6, tokenized with
  tiktoken cl100k_base). Median ctx-at-trigger 83.6%, median
  summary-only residue 2.2% of 200K window (4,349 tokens), median
  Opus gen cost $0.19. Replaces hypotheticals in README and
  `docs/token-economics.md`; adds `docs/measurements.md` with full
  distributions and methodology.

## [0.1.0] - 2026-05-06

Initial public release.

### Added

- CLI `wl` (entry point: `wormlens.cli:main`) and `python -m wormlens`
  invocation, with stdlib-only dependencies.
- Single-file zipapp distributable via `build_pyz.py` (produces
  `wormlens.pyz`).
- Multi-source extraction:
  - Claude Code (`--source cc`, default): reads JSONL session logs
    under `~/.claude/projects/`.
  - VS Code Copilot (`--source vscode`): reads workspace chat
    sessions.
  - WormLens extracts (`--source wl`): re-ingests previously emitted
    `.wl` files for chained recall.
- Auto-detection of source from input file with `--source auto`.
- Session selection: `--session <id>[,<id>...]`, `--list-sessions`,
  `--summary-stats`.
- Turn selection: `--index N` (single turn) and `--index N-M` (range).
- Output formats: chat (default, agent-optimized XML-style tags),
  markdown (`--format md`), plaintext (`--format txt`), and JSONL
  (`--format jsonl`).
- Recall mode (`--recall`): compact, addressable extracts optimized
  for agent re-ingestion (~10 tokens/turn overhead).
- Full mode (`--full`): unfiltered extraction including pre-compact
  history.
- `--grep PATTERN`: filter messages by regex.
- `--checkpoints`: include checkpoint markers in output.
- `--merge`: merge multiple sessions into a single output document.
- Handoff workflow (`--handoff`): write a handoff marker for the
  next session to pick up.
- Skill install/uninstall: `--install-skill` / `--uninstall-skill`
  bundles a self-contained `wormlens` skill under
  `.claude/skills/wormlens/`. Install merges `statusLine`,
  `UserPromptSubmit`, and `PreToolUse` hook entries into
  `.claude/settings.json`, preserving any pre-existing user content
  (a warning is emitted if a user `statusLine` already exists --
  wormlens does NOT overwrite it). Uninstall removes only wormlens
  entries and deletes `settings.json` only if it was wormlens-only.
- `wl launch` subcommand: outer-loop harness for infinite session
  continuity (flags: `--prompt`, `--ctx-limit`, `--hard-kill`,
  `--grace`, `--poll-interval`, `--project-dir`).
- Harness components: `harness/wormlens.py` (launcher) and
  `harness/wl-hook.py` (hook integration), opt-in via the skill.
- JSONL parser DoS-resistance: line-length and record-count guards
  against malformed or pathologically large session files.
- Settings install hardening: refusal to overwrite a corrupt
  `settings.json`, symlink-aware writes (rewrites through the link
  target instead of replacing the symlink).
- WormLens extract round-trip: `--source wl` re-ingests previously
  emitted `.wl` extracts losslessly across all record types
  (user/assistant/thinking/tools/hooks/bash/teammates/refs).
- Wheel and sdist build improvements: `MANIFEST.in` and
  `pyproject.toml` package-data updates so `skill.md`,
  `harness/wl-hook.py`, and `LICENSE` ship in both distributions.
- `--doctor`: environment and configuration sanity check.
- `--list-sessions` table includes a source column (S) and start
  timestamp (UTC).
- Anti-ouroboros strip set covers Claude Code synthetic blocks
  (`<system-reminder>`, `<local-command-caveat>`,
  `<available-deferred-tools>`, `<fast_mode_info>`,
  `<wormlens-boot>`, `<task-notification>`) so recall does not
  feed an agent its own out-of-band noise.

### Added (continued)

- `--no-color` flag suppresses ANSI escapes and unicode decoration
  in `--doctor` and `--grep` output. Also honors the de-facto
  `NO_COLOR` environment variable (https://no-color.org). The
  existing isatty() autodetect still applies; the new flag/env are
  for cases where stdout IS a TTY but the user wants plain ASCII
  (capture, clipboard, screenshots, CI logs).

### Changed

- `--grep` default scope now mirrors normal extraction: user and
  assistant text only. Previously grep searched every record type
  unconditionally, surfacing `[assistant/tool_use]` and
  `[system/tool_result]` matches the user almost never wanted. Use
  `--tools`, `--thinking`, `--bash`, `--hooks`, `--system-msgs`, or
  `--all` to opt back into broader search, same as for extracts.

### Notes

- Stdlib only. No third-party runtime dependencies.
- Python 3.9 or newer required.
- Source layout: package root is the repository root; the wheel
  installs under the `wormlens.*` namespace via setuptools
  `package-dir` mapping.
- Public artifact target: github.com/apresence/wormlens.
