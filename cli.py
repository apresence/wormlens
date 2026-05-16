"""CLI entry point for wormlens.

Usage:
    python -m wormlens [INPUT...] [options]
    python wormlens.pyz [INPUT...] [options]
    wl [INPUT...] [options]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .formatters import write_output
from .models import FilterOpts
from .pipeline import (
    extract_sessions,
    filter_and_sort,
    resolve_input_files,
    resolve_source,
)
from .providers import PROVIDERS
from .providers.wl_extract.parser import WlFormatError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wl",
        description="WormLens: lossless episodic memory for Claude Code and VS Code Copilot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  wl --recall --session <UUID>         # recover prior session into context
  wl --full                            # full CC session (ignore compacts)
  wl --source vscode                   # latest VS Code Copilot session
  wl session.jsonl                     # auto-detect source from file
  wl --list-sessions                   # list all sessions (S column: C/V/W/X)
  wl --list-sessions --source vscode   # list VS Code sessions only
  wl -t 20                             # last 20 messages
  wl --format jsonl --all -o full.jsonl
  wl *.jsonl --merge -o merged.md
  wl --session abc-123,def-456
  wl --index 42                        # single turn by line number
  wl --index 42-80                     # range of turns
  wl --index 42,55,80                  # specific turns
  wl --index 42-80,90-100             # multiple ranges
  wl --summary-stats                   # session stats without output
  wl launch --prompt "build X"         # outer-loop harness for continuity
        """,
    )

    p.add_argument("--version", action="version",
                   version=f"wormlens {__version__}")
    p.add_argument("input", nargs="*", default=None,
                   help="Input JSONL file(s), glob patterns, or directories")
    p.add_argument("--source", choices=list(PROVIDERS.keys()) + ["auto"], default="auto",
                   help="Source type (default: auto-detect)")
    p.add_argument("--recursive", action="store_true",
                   help="Recursively scan directories for *.jsonl files")

    modes = p.add_argument_group("modes")
    modes.add_argument("--list-sessions", action="store_true",
                       help="List available sessions with metadata")
    modes.add_argument("--full", action="store_true",
                       help="Extract entire file ignoring compact boundaries (CC)")
    modes.add_argument("--install-skill", action="store_true",
                       help="Install the wormlens agent skill into a repo")
    modes.add_argument("--uninstall-skill", action="store_true",
                       help="Remove the wormlens agent skill from a repo")
    modes.add_argument("--skill-target", default=None, metavar="DIR",
                       help="Target repo root for skill install/uninstall (default: auto-detect from cwd)")
    modes.add_argument("--grep", default=None, metavar="PATTERN",
                       help="Search all sessions for regex pattern")
    modes.add_argument("--summary-stats", "--stats", action="store_true",
                       help="Print session stats (turns, tokens, size) without extracting")
    modes.add_argument("--doctor", action="store_true",
                       help="Run diagnostics (provider imports, session discovery, env)")
    modes.add_argument("--recall", action="store_true",
                       help="Agent recall mode: strip frontmatter, add instruction caveat, stdout")
    modes.add_argument("--handoff", action="store_true",
                       help="Create handoff marker from session's <wl-summary> tag (requires --session)")
    modes.add_argument("--checkpoints", action="store_true",
                       help="Extract <wl-checkpoint> tags as ordered list (one per line)")

    # Note: `wl launch [...]` is dispatched as a subcommand at the top of
    # _main(); its arguments (--prompt, --ctx-limit, --hard-kill, --grace,
    # --poll-interval, --project-dir) are parsed by harness.wormlens.main()'s
    # own argparse, not this one. Run `wl launch --help` for that flag set.

    grep = p.add_argument_group("grep options")
    grep.add_argument("-A", "--after", type=int, default=0, metavar="N",
                      help="Show N messages after each match (default: 0)")
    grep.add_argument("-B", "--before", type=int, default=0, metavar="N",
                      help="Show N messages before each match (default: 0)")
    grep.add_argument("-i", "--ignore-case", action="store_true",
                      help="Case-insensitive pattern matching")

    output = p.add_argument_group("output")
    output.add_argument("-o", "--output", default=None,
                        help="Output file or directory (default: stdout)")
    output.add_argument("--format", choices=["chat", "md", "txt", "jsonl"], default="chat",
                        dest="fmt", help="Output format (default: chat)")
    output.add_argument("--merge", action="store_true",
                        help="Merge all inputs into single output")
    output.add_argument("--frontmatter", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="YAML frontmatter in md output (default: on for md, off otherwise)")
    output.add_argument("--summary", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Include summary in frontmatter (default: auto = compact summary if present)")
    output.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color codes and unicode decoration "
                             "in --doctor and --grep output. Honored automatically "
                             "when stdout is not a TTY or NO_COLOR env var is set.")
    output.add_argument("--max-message-bytes", type=int, default=30000, metavar="N",
                        help="Truncate any single message longer than N characters "
                             "(default 30000). Pass 0 to disable.")
    output.add_argument("--line-numbers", action="store_true",
                        help="Include source line numbers (line=N attribute in chat "
                             "format, line: N field in jsonl) for traceability back "
                             "to the source file.")

    filt = p.add_argument_group("filtering")
    filt.add_argument("--thinking", action="store_true",
                      help="Include thinking/reasoning blocks")
    filt.add_argument("--tools", action="store_true",
                      help="Include tool calls and results")
    filt.add_argument("--code-edits", action="store_true",
                      help="Include code edit groups (VS Code)")
    filt.add_argument("--hooks", action="store_true",
                      help="Include hook events (CC)")
    filt.add_argument("--bash", action="store_true",
                      help="Include bash output (CC)")
    filt.add_argument("--teammates", action="store_true",
                      help="Include teammate messages (CC)")
    filt.add_argument("--refs", action="store_true",
                      help="Include inline references (VS Code)")
    filt.add_argument("--system-msgs", action="store_true",
                      help="Include system-injected messages (CC: isMeta, local-command, etc.)")
    filt.add_argument("--compact-markers", action=argparse.BooleanOptionalAction,
                      default=False,
                      help="Include compact boundary markers inline (default: off)")
    filt.add_argument("--all", action="store_true",
                      help="Include everything")

    sel = p.add_argument_group("record selection")
    sel.add_argument("-n", type=int, default=None, metavar="N",
                     help="Limit to N output records")
    sel.add_argument("--rev", action="store_true",
                     help="Reverse: take last N (requires -n)")
    sel.add_argument("-t", "--tail", type=int, default=None, metavar="N",
                     help="Last N records (alias for --rev -n N)")
    sel.add_argument("--newest-first", action="store_true",
                     help="Reverse chronological order")
    sel.add_argument("--index", default=None, metavar="SPEC",
                     help="Retrieve specific turns by number (e.g. 42, 42-80, 42,55,80)")
    sel.add_argument("--session", default=None, metavar="ID[,ID,...]",
                     help="Extract specific session(s) by UUID")
    sel.add_argument("--session-id", default=None,
                     help="Filter to specific sessionId within a file")
    sel.add_argument("--skip-empty", action="store_true", default=True)
    sel.add_argument("--no-skip-empty", action="store_false", dest="skip_empty")
    sel.add_argument("--min-turns", type=int, default=None, metavar="N",
                     help="Minimum user+assistant turns to include a session (default: 2 for --list-sessions)")
    sel.add_argument("--min-size", type=str, default=None, metavar="SIZE",
                     help="Minimum session file size, e.g. 10KB, 1MB (default: none)")

    misc = p.add_argument_group("misc")
    misc.add_argument("--agent", default=os.environ.get("USER", "agent"),
                      help="Agent name for output filenames")
    misc.add_argument("--no-ts", action="store_true",
                      help="Omit timestamps from JSONL output")
    misc.add_argument("--no-strip-tags", action="store_true",
                      help="Keep system-injected XML tags (CC)")
    misc.add_argument("--no-parse-commands", action="store_true",
                      help="Keep raw command XML (CC)")
    misc.add_argument("--storage-id", default=None,
                      help="Override VS Code workspace storage ID")

    return p


_SOURCE_CHAR = {"cc": "C", "vscode": "V", "wl": "W", "codex": "X", "claude_ai": "A"}


def _print_sessions_table(rows: list[dict]):
    """Print a formatted table of session metadata."""
    if not rows:
        print("No sessions found.", file=sys.stderr)
        return

    has_matches = any("match_count" in r for r in rows)

    header = "S "
    header += f"{'SESSION':<38} {'SIZE':>8} {'USER':>6} {'ASST':>6} {'START (UTC)':>20}"
    if has_matches:
        header += f"  {'MATCHES':>7}"
    header += "  PREVIEW"
    print(header)
    print("-" * (140 if has_matches else 132))
    for row in rows:
        size_kb = row["size"] / 1024
        size_str = f"{size_kb / 1024:.1f}MB" if size_kb >= 1024 else f"{size_kb:.0f}KB"
        start = row.get("start_ts", "")[:16]
        last_checkpoint = row.get("last_checkpoint", "")
        wl_summary = row.get("wl_summary", "")
        if last_checkpoint:
            preview = last_checkpoint
        elif wl_summary:
            preview = wl_summary
        else:
            preview_msgs = row.get("preview", [])
            title = row.get("title", "")
            if preview_msgs:
                preview = " | ".join(preview_msgs)[:80]
            elif title:
                preview = title[:80]
            else:
                preview = ""
        user_count = row.get("user_count", row.get("turn_count", 0))
        asst_count = row.get("assistant_count", 0)
        line = _SOURCE_CHAR.get(row.get("source_type", ""), "?") + " "
        line += (
            f"{row['session_id']:<38} {size_str:>8} "
            f"{user_count:>6} {asst_count:>6} "
            f"{start:>20}"
        )
        if has_matches:
            line += f"  {row.get('match_count', 0):>7}"
        line += f"  {preview}"
        print(line)

    print(f"\n{len(rows)} session(s) found")


def _parse_size(size_str: str) -> int:
    """Parse a human-readable size like '10KB' or '1MB' into bytes."""
    size_str = size_str.strip().upper()
    multipliers = {"KB": 1024, "MB": 1024 * 1024, "GB": 1024 ** 3, "B": 1}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if size_str.endswith(suffix):
            try:
                return int(float(size_str[:-len(suffix)]) * mult)
            except ValueError:
                break
    try:
        return int(size_str)
    except ValueError:
        print(f"Error: invalid size '{size_str}'. Use e.g. 10KB, 1MB", file=sys.stderr)
        sys.exit(1)


def parse_index_spec(spec: str) -> set[int]:
    """Parse a turn-index specification into a set of turn numbers.

    Accepts:
        "42"          -> {42}
        "42-80"       -> {42, 43, ..., 80}
        "42,55,80"    -> {42, 55, 80}
        "42-80,90-100"-> {42..80, 90..100}

    Raises ValueError on malformed input.
    """
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            pieces = part.split("-", 1)
            try:
                lo = int(pieces[0])
                hi = int(pieces[1])
            except ValueError:
                raise ValueError(f"invalid index range: {part!r}")
            if lo > hi:
                raise ValueError(f"invalid index range (start > end): {part!r}")
            result.update(range(lo, hi + 1))
        else:
            try:
                result.add(int(part))
            except ValueError:
                raise ValueError(f"invalid index number: {part!r}")
    if not result:
        raise ValueError(f"empty index spec: {spec!r}")
    return result


def _filter_by_index(
    sessions: list,
    index_set: set[int],
) -> list:
    """Filter session messages to only those matching index_set turn numbers.

    For CC sessions (source_type == "cc"), the turn number is msg.source_line
    (1-based JSONL line number).

    For wl-sourced re-imports (source_type == "wl"), msg.source_line carries
    the original CC JSONL line number that the wl_extract parser preserved
    from the upstream extract -- so a CC -> wl chain can still be addressed
    by the original line number, which is the whole point of the round-trip.

    For VS Code sessions, the turn number is the sequential counter that the
    formatters assign: increments on each user/msg message.  All messages
    sharing a turn number (user prompt + assistant reply) are kept together.
    """
    from .formatters import _is_display_msg  # noqa: local import to avoid circular

    for session in sessions:
        uses_line_index = (session.source_type in ("cc", "wl"))

        if uses_line_index:
            session.messages = [
                m for m in session.messages
                if m.source_line in index_set
            ]
        else:
            # Assign sequential turn numbers matching the formatter logic,
            # then filter.  seq_turn increments on user/msg messages.
            seq_turn = 0
            keep = []
            for msg in session.messages:
                if msg.role == "user" and msg.msg_type == "msg":
                    seq_turn += 1
                turn_num = seq_turn
                if turn_num in index_set:
                    keep.append(msg)
            session.messages = keep

    # Drop sessions that ended up empty after filtering
    return [s for s in sessions if s.messages]


def _print_summary_stats(sessions: list, input_paths: list) -> None:
    """Print concise stats about extracted sessions."""
    from .formatters import _is_display_msg

    total_msgs = sum(len(s.messages) for s in sessions)
    total_display = sum(
        sum(1 for m in s.messages if _is_display_msg(m))
        for s in sessions
    )
    total_user = sum(
        sum(1 for m in s.messages if _is_display_msg(m) and m.role == "user")
        for s in sessions
    )
    total_asst = sum(
        sum(1 for m in s.messages if _is_display_msg(m) and m.role == "assistant")
        for s in sessions
    )
    total_bytes = sum(
        sum(len(m.text.encode("utf-8")) for m in s.messages)
        for s in sessions
    )
    total_chars = sum(
        sum(len(m.text) for m in s.messages)
        for s in sessions
    )
    total_words = sum(
        sum(len(m.text.split()) for m in s.messages)
        for s in sessions
    )
    tokens_approx = int(total_chars / 3.0)

    size_str = (
        f"{total_bytes / (1024 * 1024):.1f}MB"
        if total_bytes >= 1024 * 1024
        else f"{total_bytes / 1024:.0f}KB"
    )

    print(f"Sessions:        {len(sessions)}")
    print(f"User turns:      {total_user}")
    print(f"Assistant turns:  {total_asst}")
    print(f"Display messages: {total_display}")
    print(f"Total messages:  {total_msgs} (incl. tool calls, thinking, etc.)")
    print(f"Words:           {total_words}")
    print(f"Chars:           {total_chars}")
    print(f"Bytes:           {size_str}")
    print(f"Tokens (approx): {tokens_approx}")
    print(f"Source file(s):  {', '.join(p.name for p in input_paths)}")

    # Per-session breakdown when multiple sessions
    if len(sessions) > 1:
        print()
        print(f"{'SESSION ID':<38} {'USER':>6} {'ASST':>6} {'MSGS':>6} {'SIZE':>8}")
        print("-" * 70)
        for s in sessions:
            u = sum(1 for m in s.messages if _is_display_msg(m) and m.role == "user")
            a = sum(1 for m in s.messages if _is_display_msg(m) and m.role == "assistant")
            sz = sum(len(m.text.encode("utf-8")) for m in s.messages)
            sz_str = f"{sz / 1024:.0f}KB" if sz < 1024 * 1024 else f"{sz / (1024 * 1024):.1f}MB"
            print(f"{s.session_id:<38} {u:>6} {a:>6} {len(s.messages):>6} {sz_str:>8}")


def _filter_session_rows(
    rows: list[dict],
    min_turns: int | None,
    min_bytes: int | None,
) -> list[dict]:
    """Filter session metadata rows by turn count and/or file size."""
    filtered = []
    for row in rows:
        if min_bytes is not None and row.get("size", 0) < min_bytes:
            continue
        if min_turns is not None:
            # CC: user_count + assistant_count; VS Code: turn_count
            turns = row.get("turn_count", row.get("user_count", 0) + row.get("assistant_count", 0))
            if turns < min_turns:
                continue
        filtered.append(row)
    return filtered


# -- Doctor diagnostics -------------------------------------------------------


def _should_color(no_color_flag: bool = False) -> bool:
    """Decide whether to emit ANSI color and unicode decoration.

    False if --no-color was passed, NO_COLOR env var is set (per
    https://no-color.org), or stdout is not a TTY.
    """
    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _run_doctor(no_color: bool = False):
    """Run diagnostics and print a summary of environment health."""
    use_color = _should_color(no_color)

    def ok(msg: str) -> str:
        if use_color:
            return f"\033[32m[OK]\033[0m {msg}"
        return f"[OK] {msg}"

    def fail(msg: str) -> str:
        if use_color:
            return f"\033[31m[FAIL]\033[0m {msg}"
        return f"[FAIL] {msg}"

    def info(msg: str) -> str:
        if use_color:
            return f"\033[36m[INFO]\033[0m {msg}"
        return f"[INFO] {msg}"

    print("wormlens doctor -- environment diagnostics\n")

    # 1. Provider imports
    # PROVIDERS is auto-populated by providers/__init__._discover_providers(),
    # which is zipimport-safe (filesystem walk with importlib fallback).
    # Iterating PROVIDERS here avoids the previous Path(__file__).parent /
    # "providers" .iterdir() pattern that raised NotADirectoryError when
    # wormlens runs from a zipapp -- inside a zip, __file__ is a virtual path,
    # not a real directory. The auto-discovery already imported these modules,
    # so a successful entry in PROVIDERS implies a successful import.
    if not PROVIDERS:
        print(fail("No providers registered (provider auto-discovery failed)"))
    else:
        for pid, cls in sorted(PROVIDERS.items()):
            label = getattr(cls, "provider_label", pid)
            module = getattr(cls, "__module__", "?")
            print(ok(f"Provider import: {pid} ({label}) [{module}]"))

    # 2. CLAUDE_CONFIG_DIR / default ~/.claude
    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_config:
        config_path = Path(claude_config)
        if config_path.is_dir():
            print(ok(f"CLAUDE_CONFIG_DIR={claude_config}"))
        else:
            print(fail(f"CLAUDE_CONFIG_DIR={claude_config} (directory not found)"))
    else:
        default_claude = Path.home() / ".claude"
        if default_claude.is_dir():
            print(ok(f"Default config dir exists: {default_claude}"))
        else:
            print(fail(f"No CLAUDE_CONFIG_DIR set and {default_claude} not found"))

    # 3. CC session discovery
    try:
        cc_cls = PROVIDERS.get("cc")
        if cc_cls:
            cc_paths = cc_cls().discover_sessions(all_sessions=True)
            if cc_paths:
                print(ok(f"CC sessions found: {len(cc_paths)}"))
            else:
                print(fail("CC sessions: none found"))
        else:
            print(fail("CC provider not registered"))
    except Exception as exc:
        print(fail(f"CC session discovery error: {exc}"))

    # 4. VS Code session discovery
    # VS Code Copilot is an OPTIONAL provider. A host without VS Code
    # installed should not be reported as a failure. We split the report
    # into two tiers: if the workspaceStorage root is absent the provider
    # is "not installed" (INFO); if it exists but is empty/unreadable, that
    # is closer to a real misconfiguration (still reported, but as INFO
    # since the user may not run VS Code on this host). Provider not
    # registered is the only true FAIL here.
    try:
        vsc_cls = PROVIDERS.get("vscode")
        if vsc_cls:
            vsc_paths = vsc_cls().discover_sessions(all_sessions=True)
            if vsc_paths:
                print(ok(f"VS Code sessions found: {len(vsc_paths)}"))
            else:
                # Probe whether VS Code itself is present on this host.
                vscode_present = False
                try:
                    from .providers.vscode_copilot.parser import (
                        _get_workspace_store as _vsc_ws_root,
                    )
                    vscode_present = _vsc_ws_root().is_dir()
                except Exception:
                    vscode_present = False
                if vscode_present:
                    print(info(
                        "VS Code sessions: none found "
                        "(VS Code installed but no Copilot chat sessions; optional)"
                    ))
                else:
                    print(info(
                        "VS Code sessions: none found "
                        "(VS Code Copilot not detected; optional provider)"
                    ))
        else:
            print(fail("VS Code provider not registered"))
    except Exception as exc:
        print(fail(f"VS Code session discovery error: {exc}"))

    # 5. WL_INSTANCE_ID (harness active)
    wl_id = os.environ.get("WL_INSTANCE_ID")
    if wl_id:
        print(ok(f"WL_INSTANCE_ID={wl_id} (harness active)"))
    else:
        print(info("WL_INSTANCE_ID not set (harness not active)"))

    # 6. ctx.json
    ctx_candidates = []
    if claude_config:
        ctx_candidates.append(Path(claude_config) / "ctx.json")
    ctx_candidates.append(Path.home() / ".claude" / "ctx.json")
    found_ctx = False
    for cp in ctx_candidates:
        if cp.is_file():
            print(ok(f"ctx.json found: {cp}"))
            found_ctx = True
            break
    if not found_ctx:
        print(info("ctx.json not found (optional -- used by harness)"))

    print("\nDone.")


# -- Skill install/uninstall -------------------------------------------------

_SKILL_REL_DIR = ".claude/skills/wormlens"
_SETTINGS_REL = ".claude/settings.json"
_HOOK_CMD = "python3 .claude/skills/wormlens/wl-hook.py"
_HOOK_MARKER = "wormlens/wl-hook.py"  # substring used to identify our entries
_HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse")


def _get_skill_source():
    """Return a Traversable for the canonical SKILL.md bundled with the package.

    importlib.resources works for regular installs, editable installs, and
    zipapps -- Path(__file__).parent fails inside a zipapp because the
    'directory' is virtual.
    """
    from importlib.resources import files
    return files("wormlens") / "skill.md"


def _get_hook_source():
    """Return a Traversable for the canonical wl-hook.py bundled with the package."""
    from importlib.resources import files
    return files("wormlens") / "harness" / "wl-hook.py"


def _find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from start (default: cwd) looking for a repo root.

    Stops at home dir to avoid matching ~/.claude as a project root.
    """
    home = Path.home().resolve()
    d = (start or Path.cwd()).resolve()
    for _ in range(20):
        if d == home:
            break
        if (d / ".git").exists() or (d / ".github").exists() or (d / ".claude").exists():
            return d
        parent = d.parent
        if parent == d:
            break
        d = parent
    return None


class SettingsCorruptError(Exception):
    """Raised when an existing settings.json cannot be parsed.

    The install/uninstall handlers convert this to a non-zero exit with a
    user-facing message, so we never silently overwrite a user's broken-but-
    recoverable settings.json (e.g. mid-edit, with a typo or comment).
    """

    def __init__(self, path: Path, detail: str):
        self.path = path
        self.detail = detail
        super().__init__(f"settings.json at {path} is corrupt JSON: {detail}")


def _read_settings(path: Path) -> dict:
    """Load settings.json or return {} if absent.

    Raises SettingsCorruptError if the file exists but does not parse as
    JSON (or cannot be read at all). Callers must catch this and exit
    rather than overwrite -- previously this returned {} on parse failure,
    which caused _write_settings to silently clobber the user's file.
    """
    import json as _json
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SettingsCorruptError(path, f"cannot read file ({e})") from e
    if not text.strip():
        return {}
    try:
        return _json.loads(text)
    except ValueError as e:
        raise SettingsCorruptError(path, str(e)) from e


def _write_settings(path: Path, data: dict) -> None:
    """Atomically write settings.json, preserving a symlink at `path`.

    Without the symlink branch, `tmp.replace(path)` would rename(2) over
    the symlink and silently turn it into a regular file -- breaking
    setups where the user has symlinked .claude/settings.json into a
    dotfiles repo. When `path` is a symlink we resolve it and write
    THROUGH the symlink (writing the target file directly). A broken
    symlink (target's parent dir does not exist) is rejected with a
    clear error rather than papered over.
    """
    import json as _json
    payload = _json.dumps(data, indent=2) + "\n"
    if path.is_symlink():
        real = path.resolve()
        if not real.parent.is_dir():
            print(
                f"Error: settings.json at {path} is a broken symlink "
                f"(target {real} unreachable). Repair or remove the "
                f"symlink before --install-skill / --uninstall-skill.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Write directly to the resolved target. We do NOT use a
        # tempfile + rename here, because rename(2) over a symlink is
        # exactly the bug we are avoiding. The symlink itself is left
        # intact; only the file it points to is updated.
        real.write_text(payload, encoding="utf-8")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _wl_entry() -> dict:
    return {"matcher": "", "hooks": [{"type": "command", "command": _HOOK_CMD}]}


def _entry_is_wormlens(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and _HOOK_MARKER in str(h.get("command", "")):
            return True
    return False


def _install_settings_hooks(root: Path) -> list[str]:
    """Merge wormlens hook entries into project settings.json. Returns changes."""
    path = root / _SETTINGS_REL
    data = _read_settings(path)
    changes = []

    # Top-level statusLine (CC reads context_window stats here every render).
    # Three cases:
    #   1. absent              -> install wormlens's statusLine
    #   2. present + wormlens  -> no-op (idempotent reinstall)
    #   3. present + user's    -> warn to stderr, leave user's intact, continue
    #                             with hook installs. wl:on indicator and
    #                             authoritative ctx_used_pct injection are lost
    #                             until the user composes manually, but no data
    #                             loss across the install/uninstall round-trip.
    sl = data.get("statusLine")
    if "statusLine" not in data or sl is None:
        data["statusLine"] = {"type": "command", "command": _HOOK_CMD}
        changes.append("statusLine")
    elif isinstance(sl, dict) and _HOOK_MARKER in str(sl.get("command", "")):
        pass
    else:
        print(
            "Warning: Existing statusLine detected -- not overwriting; "
            "wormlens hooks installed without statusLine. wl:on indicator "
            "and authoritative ctx_used_pct injection require wormlens's "
            "statusLine. Remove your existing statusLine and rerun "
            "--install-skill, or compose via wrapper.",
            file=sys.stderr,
        )

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    for event in _HOOK_EVENTS:
        arr = hooks.setdefault(event, [])
        if not isinstance(arr, list):
            arr = []
            hooks[event] = arr
        if any(_entry_is_wormlens(e) for e in arr):
            continue
        arr.append(_wl_entry())
        changes.append(f"hooks.{event}")

    if changes:
        _write_settings(path, data)
    return changes


def _uninstall_settings_hooks(root: Path) -> list[str]:
    """Remove wormlens hook entries from project settings.json. Returns changes."""
    path = root / _SETTINGS_REL
    if not path.is_file():
        return []
    data = _read_settings(path)
    changes = []

    sl = data.get("statusLine")
    if isinstance(sl, dict) and _HOOK_MARKER in str(sl.get("command", "")):
        del data["statusLine"]
        changes.append("statusLine")

    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks.keys()):
            arr = hooks.get(event)
            if not isinstance(arr, list):
                continue
            kept = [e for e in arr if not _entry_is_wormlens(e)]
            if len(kept) != len(arr):
                changes.append(f"hooks.{event}")
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
        if not hooks:
            del data["hooks"]

    if changes:
        if data:
            _write_settings(path, data)
        else:
            path.unlink()
    return changes


def _install_skill(target_dir: str | None):
    """Install the wormlens SKILL.md, wl-hook.py, and managed hooks into a repo."""
    skill_source = _get_skill_source()
    hook_source = _get_hook_source()
    if not skill_source.is_file():
        print(f"Error: bundled skill.md not found at {skill_source}", file=sys.stderr)
        sys.exit(1)
    if not hook_source.is_file():
        print(f"Error: bundled wl-hook.py not found at {hook_source}", file=sys.stderr)
        sys.exit(1)

    if target_dir:
        root = Path(target_dir).resolve()
    else:
        root = _find_repo_root()
        if not root:
            print("Error: no repo root found from cwd. Use --skill-target DIR.", file=sys.stderr)
            sys.exit(1)

    skill_content = skill_source.read_text(encoding="utf-8")
    hook_content = hook_source.read_text(encoding="utf-8")

    skill_dir = root / _SKILL_REL_DIR
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_dest = skill_dir / "SKILL.md"
    hook_dest = skill_dir / "wl-hook.py"
    skill_dest.write_text(skill_content, encoding="utf-8")
    hook_dest.write_text(hook_content, encoding="utf-8")
    hook_dest.chmod(0o755)

    print(f"Installed: {skill_dest.relative_to(root)}")
    print(f"Installed: {hook_dest.relative_to(root)}")

    try:
        changes = _install_settings_hooks(root)
    except SettingsCorruptError as e:
        print(
            f"Error: Existing settings.json at {e.path} is corrupt JSON; "
            f"refusing to overwrite. Fix or remove it before "
            f"--install-skill.\n  Detail: {e.detail}",
            file=sys.stderr,
        )
        sys.exit(1)
    for c in changes:
        print(f"Configured: {_SETTINGS_REL} ({c})")
    if not changes:
        print(f"Already configured: {_SETTINGS_REL}")


def _uninstall_skill(target_dir: str | None):
    """Remove the wormlens skill and managed hooks from a repo."""
    import shutil

    if target_dir:
        root = Path(target_dir).resolve()
    else:
        root = _find_repo_root()
        if not root:
            print("Error: no repo root found from cwd. Use --skill-target DIR.", file=sys.stderr)
            sys.exit(1)

    skill_dir = root / _SKILL_REL_DIR
    removed = []
    if skill_dir.is_dir():
        shutil.rmtree(skill_dir)
        removed.append(str(skill_dir.relative_to(root)))

    try:
        changes = _uninstall_settings_hooks(root)
    except SettingsCorruptError as e:
        print(
            f"Error: Existing settings.json at {e.path} is corrupt JSON; "
            f"refusing to overwrite. Fix or remove it before "
            f"--uninstall-skill.\n  Detail: {e.detail}",
            file=sys.stderr,
        )
        sys.exit(1)

    for p in removed:
        print(f"Removed: {p}")
    for c in changes:
        print(f"Cleaned: {_SETTINGS_REL} ({c})")

    if not removed and not changes:
        print("No wormlens install found to remove.", file=sys.stderr)


# -- Grep search -------------------------------------------------------------


def _grep_sessions(sessions: list, pattern: str, ignore_case: bool = False,
                   before: int = 0, after: int = 0, no_color: bool = False) -> int:
    """Search extracted sessions for a regex pattern. Returns match count."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        print(f"Error: invalid regex: {e}", file=sys.stderr)
        sys.exit(1)

    use_color = _should_color(no_color)
    # Box-drawing chars fail on Windows cp1252 when piped, and the no-color
    # path is also a clean-ASCII path for capture/clipboard/CI logs.
    sep = "\u2500\u2500" if use_color else "--"

    def c(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if use_color else text

    total_matches = 0
    matched_sessions = 0

    for session in sessions:
        msgs = [m for m in session.messages if m.text]
        hits = []
        for idx, msg in enumerate(msgs):
            if rx.search(msg.text):
                hits.append(idx)

        if not hits:
            continue

        matched_sessions += 1
        sid_short = session.session_id[:12]
        src_label = session.source_type or "unknown"
        ts = session.start_ts[:19] if session.start_ts else ""
        title = session.metadata.get("title", "")
        header = f"\n{c('1;36', f'{sep} {sid_short} ({src_label}) {ts}')}"
        if title:
            header += f" -- {title}"
        print(header)

        # Compute context windows (merged)
        show_indices: set[int] = set()
        for h in hits:
            lo = max(0, h - before)
            hi = min(len(msgs) - 1, h + after)
            for j in range(lo, hi + 1):
                show_indices.add(j)

        last_shown = -2
        for idx in sorted(show_indices):
            if idx > last_shown + 1:
                print(f"  {c('2', '...')}")
            last_shown = idx

            msg = msgs[idx]
            is_hit = idx in hits
            role_color = "33" if msg.role == "user" else "32"
            label = msg.role if msg.msg_type == "msg" else f"{msg.role}/{msg.msg_type}"
            prefix = f"  {c(role_color, f'[{label}]')} "

            lines = msg.text.splitlines()
            if is_hit:
                total_matches += 1
                match_lines = [l for l in lines if rx.search(l)]
                if match_lines:
                    for ml in match_lines[:3]:
                        highlighted = rx.sub(
                            lambda m: c('1;31', m.group()), ml)
                        print(f"{prefix}{highlighted}")
                        prefix = "         "
                    if len(match_lines) > 3:
                        print(f"         {c('2', f'... {len(match_lines) - 3} more matching lines')}")
                else:
                    print(f"{prefix}{lines[0][:200]}")
            else:
                summary = lines[0][:120] if lines else ""
                if len(lines) > 1:
                    summary += f" (+{len(lines)-1} lines)"
                print(f"{prefix}{c('2', summary)}")

    total_files = len({s.source_file for s in sessions})
    summary_text = (
        f"{total_matches} match(es) in {matched_sessions} session(s), "
        f"{len(sessions)} session(s) from {total_files} file(s) searched"
    )
    print(f"\n{c('1', summary_text)}", file=sys.stderr)
    return total_matches


def _do_handoff(session_id_prefix: str, handoff_marker_path: Path):
    """Scan session for <wl-summary> and create handoff marker file."""
    import json as _json

    sources_to_search = [cls() for cls in PROVIDERS.values()]
    input_paths = []
    for src in sources_to_search:
        all_files = src.discover_sessions(all_sessions=True)
        input_paths.extend(
            f for f in all_files
            if f.stem.startswith(session_id_prefix)
        )

    if not input_paths:
        print(f"Error: no sessions found matching: {session_id_prefix}", file=sys.stderr)
        sys.exit(1)

    session_file = input_paths[0]
    summary_rx = re.compile(r"<wl-summary>(.*?)</wl-summary>", re.DOTALL)
    found_summary = None

    lines = session_file.read_bytes().splitlines()
    for raw_line in reversed(lines):
        try:
            record = _json.loads(raw_line)
        except (ValueError, UnicodeDecodeError):
            continue
        if record.get("type") != "assistant":
            continue
        msg = record.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = " ".join(parts)
        if not isinstance(content, str):
            continue
        m = summary_rx.search(content)
        if m:
            found_summary = m.group(1).strip()
            break

    if not found_summary:
        print(
            "Error: no <wl-summary> tag found in session. "
            "Write a <wl-summary>description</wl-summary> in your response before calling wl --handoff.",
            file=sys.stderr,
        )
        sys.exit(1)

    handoff_marker_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_marker_path.touch()
    print(f"Handoff ready: {found_summary}", file=sys.stderr)


def _do_checkpoints(args):
    """Extract <wl-checkpoint> tags from sessions and print as ordered list."""
    source = resolve_source(
        args.source if args.source != "auto" else None,
        [Path(p) for p in args.input] if args.input else None,
    )

    if args.session:
        session_ids = [s.strip() for s in args.session.split(",")]
        if args.source == "auto":
            sources_to_search = [cls() for cls in PROVIDERS.values()]
        else:
            sources_to_search = [source]
        input_paths = []
        for src in sources_to_search:
            all_files = src.discover_sessions(all_sessions=True)
            input_paths.extend(
                f for f in all_files
                if any(f.stem.startswith(sid) for sid in session_ids)
            )
        if not input_paths:
            print(f"Error: no sessions found matching: {args.session}", file=sys.stderr)
            sys.exit(1)
        if args.source == "auto":
            from .providers import detect_provider
            detected = detect_provider(input_paths[0])
            if detected:
                source = detected()
    else:
        has_explicit_input = bool(args.input)
        recovery_mode = (not has_explicit_input) and (not args.full)
        extra = {}
        if source.provider_id == "vscode":
            extra["storage_id"] = args.storage_id
        input_paths = resolve_input_files(
            args.input or None, source,
            recovery=recovery_mode,
            recursive=args.recursive,
            **extra,
        )

    opts = FilterOpts()
    sessions = extract_sessions(
        source, input_paths, opts,
        session_id_filter=args.session_id,
        since_last_compact=False,
    )

    total = 0
    for session in sessions:
        for cp in session.checkpoints:
            print(f"[turn {cp['turn']}] {cp['text']}")
            total += 1

    if total == 0:
        print("No checkpoints found.", file=sys.stderr)


def main():
    try:
        _main()
    except WlFormatError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _main():
    # Subcommand dispatch BEFORE argparse: `wl launch [...]` forwards its
    # remaining argv to the harness's own argparse so the documented form
    # `wl launch --prompt "..." --ctx-limit 85` works literally. Without
    # this short-circuit, argparse would treat "launch" as a positional
    # input file path and emit the misleading "Warning: launch not found".
    if len(sys.argv) >= 2 and sys.argv[1] == "launch":
        from .harness.wormlens import main as harness_main
        rc = harness_main(sys.argv[2:])
        sys.exit(rc or 0)

    parser = _build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    args = parser.parse_args()

    if args.install_skill:
        _install_skill(args.skill_target)
        return
    if args.uninstall_skill:
        _uninstall_skill(args.skill_target)
        return
    if args.doctor:
        _run_doctor(no_color=args.no_color)
        return
    if args.handoff:
        if not args.session:
            print("Error: --handoff requires --session <session-id>", file=sys.stderr)
            sys.exit(1)
        marker = Path.home() / ".claude" / ".wormlens" / ".handoff"
        _do_handoff(args.session.strip(), marker)
        return

    if args.checkpoints:
        _do_checkpoints(args)
        return

    if args.all:
        args.thinking = args.tools = args.hooks = args.bash = True
        args.code_edits = args.refs = args.teammates = True
        args.system_msgs = True
        args.compact_markers = True

    if args.tail is not None:
        if args.tail <= 0:
            parser.error("--tail requires a positive integer")
        args.rev = True
        args.n = args.tail

    if args.n is not None and args.n <= 0:
        parser.error("-n requires a positive integer")

    if args.rev and args.n is None:
        parser.error("--rev requires -n")

    source = resolve_source(
        args.source if args.source != "auto" else None,
        [Path(p) for p in args.input] if args.input else None,
    )

    min_turns = args.min_turns
    min_bytes = _parse_size(args.min_size) if args.min_size else None

    if args.list_sessions and args.grep:
        grep_flags = re.IGNORECASE if args.ignore_case else 0
        try:
            grep_rx = re.compile(args.grep, grep_flags)
        except re.error as e:
            print(f"Error: invalid regex: {e}", file=sys.stderr)
            sys.exit(1)

        if args.source != "auto":
            grep_sources = [source]
        else:
            grep_sources = [cls() for cls in PROVIDERS.values()]

        explicit_paths = [Path(p) for p in args.input] if args.input else None
        all_rows = []
        for src in grep_sources:
            all_rows.extend(src.list_sessions_metadata(paths=explicit_paths))

        if min_turns is None and min_bytes is None:
            min_turns = 2
        all_rows = _filter_session_rows(all_rows, min_turns, min_bytes)

        matching_rows = []
        for row in all_rows:
            fpath = Path(row["file"])
            if not fpath.is_file():
                continue
            match_count = 0
            with open(fpath, "rb") as f:
                for raw_line in f:
                    try:
                        line_str = raw_line.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if grep_rx.search(line_str):
                        match_count += 1
            if match_count > 0:
                row["match_count"] = match_count
                matching_rows.append(row)

        if not matching_rows:
            print(f"No sessions matched pattern '{args.grep}'.", file=sys.stderr)
            sys.exit(1)
        matching_rows.sort(key=lambda r: r.get("start_ts") or "", reverse=True)
        if args.n is not None:
            matching_rows = matching_rows[-args.n:] if args.rev else matching_rows[:args.n]
        _print_sessions_table(matching_rows)
        return

    if args.list_sessions:
        if args.source == "wl":
            print(
                "Error: the wl provider does not support session discovery; "
                "pass a .wl file path as input.",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.source != "auto":
            list_sources = [source]
        else:
            list_sources = [cls() for cls in PROVIDERS.values()]
        explicit_paths = [Path(p) for p in args.input] if args.input else None
        rows = []
        for src in list_sources:
            rows.extend(src.list_sessions_metadata(paths=explicit_paths))
        if min_turns is None and min_bytes is None:
            min_turns = 2  # default: filter out noise sessions
        rows = _filter_session_rows(rows, min_turns, min_bytes)
        rows.sort(key=lambda r: r.get("start_ts") or "", reverse=True)
        if args.n is not None:
            rows = rows[-args.n:] if args.rev else rows[:args.n]
        _print_sessions_table(rows)
        return

    if args.grep:
        # grep mode: filter scope mirrors the rest of wormlens. By default
        # only user + assistant text is searched; pass --tools / --thinking
        # / --bash / --all to broaden. Default keeps grep output uncluttered
        # by tool_use / tool_result / hook / system_inject matches the user
        # almost never wants.
        grep_opts = FilterOpts(
            thinking=args.thinking,
            tools=args.tools,
            hooks=args.hooks,
            bash=args.bash,
            code_edits=args.code_edits,
            refs=args.refs,
            teammates=args.teammates,
            system_msgs=args.system_msgs,
            compact_markers=args.compact_markers,
            strip_tags=not args.no_strip_tags,
            parse_commands=not args.no_parse_commands,
            skip_empty=True,
        )
        # Determine which sources to search
        if args.source != "auto":
            grep_sources = [source]
        else:
            grep_sources = [cls() for cls in PROVIDERS.values()]

        all_sessions = []
        for src in grep_sources:
            extra = {"all_sessions": True}
            if src.provider_id == "vscode" and args.storage_id:
                extra["storage_id"] = args.storage_id
            paths = src.discover_sessions(**extra)
            if not paths:
                continue
            print(f"Scanning {len(paths)} file(s) ({src.provider_label})", file=sys.stderr)
            sessions = extract_sessions(src, paths, grep_opts, since_last_compact=False)
            all_sessions.extend(sessions)

        total_matches = _grep_sessions(
            all_sessions, args.grep,
            ignore_case=args.ignore_case,
            before=args.before, after=args.after,
            no_color=args.no_color,
        )
        # Per CHECKPOINT 2026-05-04: empty grep returns exit 1, hits exit 0.
        # The --grep + --list-sessions branch already enforces this; this
        # bare --grep branch was previously falling through to implicit 0.
        if not total_matches:
            print(
                f"No matches for pattern '{args.grep}'.",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.exit(0)

    opts = FilterOpts(
        thinking=args.thinking,
        tools=args.tools,
        hooks=args.hooks,
        bash=args.bash,
        code_edits=args.code_edits,
        refs=args.refs,
        teammates=args.teammates,
        system_msgs=args.system_msgs,
        compact_markers=args.compact_markers,
        strip_tags=not args.no_strip_tags,
        parse_commands=not args.no_parse_commands,
        skip_empty=args.skip_empty,
    )

    has_explicit_input = bool(args.input)

    if args.session:
        session_ids = [s.strip() for s in args.session.split(",")]
        # Search all sources when auto-detect, or just the specified source
        if args.source == "auto":
            sources_to_search = [cls() for cls in PROVIDERS.values()]
        else:
            sources_to_search = [source]
        input_paths = []
        for src in sources_to_search:
            extra = {"all_sessions": True}
            if src.provider_id == "vscode" and args.storage_id:
                extra["storage_id"] = args.storage_id
            all_files = src.discover_sessions(**extra)
            input_paths.extend(
                f for f in all_files
                if any(f.stem.startswith(sid) for sid in session_ids)
            )
        if not input_paths:
            print(f"Error: no sessions found matching: {args.session}", file=sys.stderr)
            sys.exit(1)
        # Re-resolve source from the matched file
        if args.source == "auto" and input_paths:
            from .providers import detect_provider
            detected = detect_provider(input_paths[0])
            if detected:
                source = detected()
        use_compact_filter = False
    else:
        recovery_mode = (not has_explicit_input) and (not args.full)
        extra = {}
        if source.provider_id == "vscode":
            extra["storage_id"] = args.storage_id
        input_paths = resolve_input_files(
            args.input or None, source,
            recovery=recovery_mode,
            recursive=args.recursive,
            **extra,
        )
        use_compact_filter = (not has_explicit_input) and (not args.full)

    sessions = extract_sessions(
        source, input_paths, opts,
        session_id_filter=args.session_id,
        since_last_compact=use_compact_filter,
    )

    sessions = filter_and_sort(
        sessions, opts,
        newest_first=args.newest_first,
        limit_n=args.n,
        reverse_limit=args.rev,
    )

    # -- Index filtering (--index SPEC) ----------------------------------------
    if args.index is not None:
        try:
            index_set = parse_index_spec(args.index)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        sessions = _filter_by_index(sessions, index_set)
        if not sessions:
            print(f"No messages matched index spec: {args.index}", file=sys.stderr)
            if use_compact_filter:
                print("Hint: recovery mode skips pre-compact messages. "
                      "Try --full --index " + args.index, file=sys.stderr)
            sys.exit(0)

    # -- Summary stats (--summary-stats) ---------------------------------------
    if args.summary_stats:
        _print_summary_stats(sessions, input_paths)
        return

    project = ""
    if sessions:
        project = sessions[0].metadata.get("project", "")

    # Resolve frontmatter default: on for md/chat, off for txt/jsonl
    # --recall forces frontmatter off (agent already committed to loading)
    use_frontmatter = args.frontmatter
    if args.recall:
        use_frontmatter = False
    elif use_frontmatter is None:
        use_frontmatter = (args.fmt in ("md", "chat"))

    md_meta = {
        "agent": args.agent,
        "include_types": opts.included_types(),
        "project": project,
        "frontmatter": use_frontmatter,
        "summary": args.summary,  # None = auto
        "recall": args.recall,
        "max_message_bytes": args.max_message_bytes,
        "line_numbers": args.line_numbers,
    }

    out_path = None
    if args.output:
        out_path = Path(args.output)
        if not out_path.suffix:
            out_path = out_path.with_suffix(f".{args.fmt}")
    elif args.merge:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = Path(f"{args.agent}_extracted_merged_{ts}.{args.fmt}")

    count = write_output(sessions, out_path, args.fmt, args.no_ts, md_meta)

    src_names = ", ".join(p.name for p in input_paths)
    dest = f" -> {out_path}" if out_path else ""
    print(f"Extracted {count} records from {src_names}{dest}", file=sys.stderr)
    if count == 0 and sessions:
        print("Hint: 0 records extracted. Try --all to include tool calls, "
              "thinking, and system messages.", file=sys.stderr)
