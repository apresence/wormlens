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


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wl",
        description="WormLens: lossless episodic memory for Claude Code and VS Code Copilot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  wl                                   # latest CC session, recovery mode
  wl --full                            # full CC session (ignore compacts)
  wl --source vscode                   # latest VS Code Copilot session
  wl session.jsonl                     # auto-detect source from file
  wl --list-sessions                   # list CC sessions
  wl --list-sessions --source vscode   # list VS Code sessions
  wl -t 20                             # last 20 messages
  wl --format jsonl --all -o full.jsonl
  wl *.jsonl --merge -o merged.md
  wl --session abc-123,def-456
  wl --index 42                        # single turn by line number
  wl --index 42-80                     # range of turns
  wl --index 42,55,80                  # specific turns
  wl --index 42-80,90-100             # multiple ranges
  wl --summary-stats                   # session stats without output
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
    modes.add_argument("--summary-stats", action="store_true",
                       help="Print session stats (turns, messages, size) without extracting")
    modes.add_argument("--doctor", action="store_true",
                       help="Run diagnostics (provider imports, session discovery, env)")
    modes.add_argument("--launch", action="store_true",
                       help="Launch the wormlens outer-loop harness")
    modes.add_argument("--recall", action="store_true",
                       help="Agent recall mode: strip frontmatter, add instruction caveat, stdout")
    modes.add_argument("--handoff", action="store_true",
                       help="Create handoff marker from session's <wl-summary> tag (requires --session)")

    launch = p.add_argument_group("launch options (used with --launch)")
    launch.add_argument("--prompt", default=None,
                        help="Initial task prompt for the CC session")
    launch.add_argument("--ctx-limit", type=int, default=90,
                        help="Context used %% for URGENT injection (default: 90)")
    launch.add_argument("--hard-kill", type=int, default=99,
                        help="Context used %% for force kill (default: 99)")
    launch.add_argument("--grace", type=float, default=60.0,
                        help="Seconds after URGENT before forced handoff (default: 60)")
    launch.add_argument("--poll-interval", type=float, default=2.0,
                        help="Harness poll interval in seconds (default: 2.0)")
    launch.add_argument("--project-dir", default=None,
                        help="Project directory for trust dialog (default: cwd)")

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


def _print_sessions_table(rows: list[dict]):
    """Print a formatted table of session metadata."""
    if not rows:
        print("No sessions found.", file=sys.stderr)
        return

    source_type = rows[0].get("source_type", "")

    has_matches = any("match_count" in r for r in rows)

    if source_type == "cc":
        header = f"{'SESSION ID':<38} {'SIZE':>8} {'USER':>6} {'ASST':>6} {'START':>20}"
        if has_matches:
            header += f"  {'MATCHES':>7}"
        header += "  PREVIEW"
        print(header)
        print("-" * (138 if has_matches else 130))
        for row in rows:
            size_kb = row["size"] / 1024
            size_str = f"{size_kb / 1024:.1f}MB" if size_kb >= 1024 else f"{size_kb:.0f}KB"
            start = row.get("start_ts", "")[:16]
            wl_summary = row.get("wl_summary", "")
            if wl_summary:
                preview = wl_summary
            else:
                preview_msgs = row.get("preview", [])
                preview = " | ".join(preview_msgs)[:80] if preview_msgs else ""
            line = (
                f"{row['session_id']:<38} {size_str:>8} "
                f"{row.get('user_count', 0):>6} {row.get('assistant_count', 0):>6} "
                f"{start:>20}"
            )
            if has_matches:
                line += f"  {row.get('match_count', 0):>7}"
            line += f"  {preview}"
            print(line)
    else:
        print(f"{'SESSION ID':<38} {'SIZE':>8} {'TURNS':>6} {'TITLE':<40} {'DATE':>24}")
        print("-" * 120)
        for row in rows:
            size_kb = row["size"] / 1024
            size_str = f"{size_kb / 1024:.1f}MB" if size_kb >= 1024 else f"{size_kb:.0f}KB"
            title = row.get("title", "")[:40]
            start = row.get("start_ts", "")[:19]
            print(
                f"{row['session_id']:<38} {size_str:>8} {row.get('turn_count', 0):>6} "
                f"{title:<40} {start:>24}"
            )

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

    For VS Code sessions, the turn number is the sequential counter that the
    formatters assign: increments on each user/msg message.  All messages
    sharing a turn number (user prompt + assistant reply) are kept together.
    """
    from .formatters import _is_display_msg  # noqa: local import to avoid circular

    for session in sessions:
        uses_line_index = (session.source_type == "cc")

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
    print(f"Approx size:     {size_str}")
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


def _run_doctor():
    """Run diagnostics and print a summary of environment health."""
    use_color = sys.stdout.isatty()

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
    import importlib
    provider_dir = Path(__file__).parent / "providers"
    for entry in sorted(provider_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if not (entry / "__init__.py").is_file():
            continue
        module_name = f"wormlens.providers.{entry.name}"
        try:
            importlib.import_module(module_name)
            print(ok(f"Provider import: {entry.name}"))
        except Exception as exc:
            print(fail(f"Provider import: {entry.name} -- {exc}"))

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
    try:
        vsc_cls = PROVIDERS.get("vscode")
        if vsc_cls:
            vsc_paths = vsc_cls().discover_sessions(all_sessions=True)
            if vsc_paths:
                print(ok(f"VS Code sessions found: {len(vsc_paths)}"))
            else:
                print(fail("VS Code sessions: none found"))
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


def _get_skill_source() -> Path:
    """Return path to the canonical SKILL.md bundled with the package."""
    return Path(__file__).parent / "skill.md"


def _get_hook_source() -> Path:
    """Return path to the canonical wl-hook.py bundled with the package."""
    return Path(__file__).parent / "harness" / "wl-hook.py"


def _find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from start (default: cwd) looking for a repo root."""
    d = (start or Path.cwd()).resolve()
    for _ in range(20):
        if (d / ".git").exists() or (d / ".github").exists() or (d / ".claude").exists():
            return d
        parent = d.parent
        if parent == d:
            break
        d = parent
    return None


def _read_settings(path: Path) -> dict:
    import json as _json
    if not path.is_file():
        return {}
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _write_settings(path: Path, data: dict) -> None:
    import json as _json
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
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

    # Top-level statusLine (CC reads context_window stats here every render)
    sl = data.get("statusLine")
    if not (isinstance(sl, dict) and _HOOK_MARKER in str(sl.get("command", ""))):
        data["statusLine"] = {"type": "command", "command": _HOOK_CMD}
        changes.append("statusLine")

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

    changes = _install_settings_hooks(root)
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

    changes = _uninstall_settings_hooks(root)

    for p in removed:
        print(f"Removed: {p}")
    for c in changes:
        print(f"Cleaned: {_SETTINGS_REL} ({c})")

    if not removed and not changes:
        print("No wormlens install found to remove.", file=sys.stderr)


# -- Grep search -------------------------------------------------------------


def _grep_sessions(sessions: list, pattern: str, ignore_case: bool = False,
                   before: int = 0, after: int = 0) -> int:
    """Search extracted sessions for a regex pattern. Returns match count."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        print(f"Error: invalid regex: {e}", file=sys.stderr)
        sys.exit(1)

    use_color = sys.stdout.isatty()
    # Box-drawing chars fail on Windows cp1252 when piped
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


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if args.install_skill:
        _install_skill(args.skill_target)
        return
    if args.uninstall_skill:
        _uninstall_skill(args.skill_target)
        return
    if args.doctor:
        _run_doctor()
        return
    if args.launch:
        from .harness.wormlens import main as harness_main
        argv = []
        if args.prompt:
            argv += ["--prompt", args.prompt]
        if args.ctx_limit != 90:
            argv += ["--ctx-limit", str(args.ctx_limit)]
        if args.hard_kill != 99:
            argv += ["--hard-kill", str(args.hard_kill)]
        if args.grace != 60.0:
            argv += ["--grace", str(args.grace)]
        if args.poll_interval != 2.0:
            argv += ["--poll-interval", str(args.poll_interval)]
        if args.project_dir:
            argv += ["--project-dir", args.project_dir]
        harness_main(argv if argv else None)
        return
    if args.handoff:
        if not args.session:
            print("Error: --handoff requires --session <session-id>", file=sys.stderr)
            sys.exit(1)
        marker = Path.home() / ".claude" / ".wormlens" / ".handoff"
        _do_handoff(args.session.strip(), marker)
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

        all_rows = []
        for src in grep_sources:
            all_rows.extend(src.list_sessions_metadata())

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
        _print_sessions_table(matching_rows)
        return

    if args.list_sessions:
        rows = source.list_sessions_metadata()
        if min_turns is None and min_bytes is None:
            min_turns = 2  # default: filter out noise sessions
        rows = _filter_session_rows(rows, min_turns, min_bytes)
        _print_sessions_table(rows)
        return

    if args.grep:
        # grep mode: search all message types, all sessions, all sources
        grep_opts = FilterOpts(
            thinking=True, tools=True, hooks=True, bash=True,
            code_edits=True, refs=True, teammates=True, system_msgs=True,
            compact_markers=True, strip_tags=not args.no_strip_tags,
            parse_commands=not args.no_parse_commands, skip_empty=True,
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

        _grep_sessions(all_sessions, args.grep,
                       ignore_case=args.ignore_case,
                       before=args.before, after=args.after)
        return

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
