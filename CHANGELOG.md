# Changelog

All notable changes to wormlens are documented in this file. The format
is loosely based on Keep a Changelog, and the project adheres to
Semantic Versioning.

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

### Notes

- Stdlib only. No third-party runtime dependencies.
- Python 3.9 or newer required.
- Source layout: package root is the repository root; the wheel
  installs under the `wormlens.*` namespace via setuptools
  `package-dir` mapping.
- Public artifact target: github.com/apresence/wormlens.
