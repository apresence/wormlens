"""Extraction pipeline for wormlens.

Orchestrates: discover -> parse -> filter -> sort -> limit -> output.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .models import ChatSession, FilterOpts
from .providers import PROVIDERS, detect_provider
from .providers._base import Provider


def _detect_skinsuit() -> str | None:
    """Sniff env vars to identify the calling agent runtime.

    Returns a provider_id if we recognize the host CLI. Used by --recall
    and other no-input modes so a codex agent doesn't accidentally pull
    CC sessions just because CC is the historical default. Explicit
    --source always wins.
    """
    if os.environ.get("CLAUDECODE"):
        return "cc"
    if os.environ.get("CODEX_HOME"):
        return "codex"
    if os.environ.get("TERM_PROGRAM") == "vscode":
        return "vscode"
    return None


def resolve_source(source_name: str | None, input_paths: list[Path] | None) -> Provider:
    """Resolve which source backend to use.

    Precedence:
      1. Explicit --source
      2. Auto-detect from first input file's shape
      3. Sniff calling-skinsuit env vars (CLAUDECODE, CODEX_HOME, TERM_PROGRAM)
      4. Fall back to CC
    """
    if source_name and source_name != "auto":
        cls = PROVIDERS.get(source_name)
        if not cls:
            valid = ", ".join(PROVIDERS.keys())
            print(f"Error: unknown source '{source_name}'. Valid: {valid}", file=sys.stderr)
            sys.exit(1)
        return cls()

    if input_paths:
        for path in input_paths:
            if path.is_file():
                src_cls = detect_provider(path)
                if src_cls:
                    return src_cls()

    sniffed = _detect_skinsuit()
    if sniffed and sniffed in PROVIDERS:
        return PROVIDERS[sniffed]()

    return PROVIDERS["cc"]()


def resolve_input_files(
    inputs: list[str] | None,
    source: Provider,
    recovery: bool = False,
    recursive: bool = False,
    **kwargs,
) -> list[Path]:
    """Resolve input arguments to file paths.

    When no inputs given, uses source.discover_sessions().
    Extra kwargs are forwarded to discover_sessions (e.g. storage_id).
    """
    if not inputs:
        paths = source.discover_sessions(recovery=recovery, **kwargs)
        if not paths:
            print(f"Error: no session files found for {source.provider_label}", file=sys.stderr)
            print("", file=sys.stderr)
            print("Hints:", file=sys.stderr)
            print("  wl --doctor          Run diagnostics to check your environment", file=sys.stderr)
            print("  wl --list-sessions   List available sessions", file=sys.stderr)
            print("  wl --source vscode   Try a different source", file=sys.stderr)
            sys.exit(1)
        if kwargs.get("all_sessions"):
            print(f"Scanning {len(paths)} session file(s) ({source.provider_label})", file=sys.stderr)
        else:
            print(f"Auto-selected: {paths[0]}", file=sys.stderr)
        return paths

    import glob as globmod

    paths = []
    for inp in inputs:
        pp = Path(inp)

        if pp.is_dir() and recursive:
            jsonl_files = sorted(
                f for f in pp.rglob("*.jsonl")
                if "subagents" not in f.parts
            )
            if jsonl_files:
                paths.extend(jsonl_files)
                print(f"Found {len(jsonl_files)} JSONL files in {inp} (recursive)", file=sys.stderr)
            else:
                print(f"Warning: no JSONL files found in {inp}", file=sys.stderr)
            continue

        expanded = sorted(globmod.glob(inp))
        if expanded:
            for p in expanded:
                pp2 = Path(p)
                if pp2.is_file():
                    paths.append(pp2)
        elif pp.exists() and pp.is_file():
            paths.append(pp)
        else:
            print(f"Warning: {inp} not found, skipping", file=sys.stderr)

    if not paths:
        print("Error: no valid input files found", file=sys.stderr)
        sys.exit(1)

    return paths


def extract_sessions(
    source: Provider,
    input_paths: list[Path],
    opts: FilterOpts,
    session_id_filter: str | None = None,
    since_last_compact: bool = False,
) -> list[ChatSession]:
    """Parse all input files and return ChatSession objects."""
    all_sessions: list[ChatSession] = []
    for path in input_paths:
        sessions = source.parse_file(
            path, opts,
            session_id_filter=session_id_filter,
            since_last_compact=since_last_compact,
        )
        all_sessions.extend(sessions)
    return all_sessions


def dedupe_sessions(sessions: list[ChatSession], keep: str = "newest") -> list[ChatSession]:
    """Collapse duplicate sessions that came from more than one file.

    A "duplicate" is the same (source_type, session_id) appearing in multiple
    source files -- e.g. a backup copy plus the live one when both are on the
    discovery path. `keep`:

      "newest" -- keep the copy whose source file has the latest mtime (default)
      "oldest" -- keep the earliest copy
      "all"    -- no dedup (return as-is)

    First-seen order is preserved; callers sort afterwards.
    """
    if keep == "all" or len(sessions) < 2:
        return sessions

    def _mtime(s: ChatSession) -> float:
        try:
            return os.path.getmtime(s.source_file) if s.source_file else 0.0
        except OSError:
            return 0.0

    best: dict[tuple, ChatSession] = {}
    order: list[tuple] = []
    for s in sessions:
        key = (s.source_type, s.session_id)
        if key not in best:
            best[key] = s
            order.append(key)
        elif keep == "newest" and _mtime(s) >= _mtime(best[key]):
            best[key] = s
        elif keep == "oldest" and _mtime(s) < _mtime(best[key]):
            best[key] = s
    return [best[k] for k in order]


def filter_and_sort(
    sessions: list[ChatSession],
    opts: FilterOpts,
    newest_first: bool = False,
    limit_n: int | None = None,
    reverse_limit: bool = False,
) -> list[ChatSession]:
    """Apply message filtering, sorting, and record limits."""
    for session in sessions:
        session.messages = [m for m in session.messages if opts.should_include(m)]
        if opts.skip_empty:
            session.messages = [
                m for m in session.messages
                if m.text.strip() and m.text not in ("*[empty message]*", "*[no response]*")
            ]

    sessions = [s for s in sessions if s.messages]

    # Stamp each message with its sequential turn number BEFORE the
    # possible -n / --rev / --tail slice. format_chat picks this up
    # when present so the slice preserves the original turn labels
    # (e.g. --tail 3 shows turns 25/26/27, not 0/1/2).
    for session in sessions:
        seq_turn = 0
        for msg in session.messages:
            if msg.msg_type == "msg" and msg.role == "user":
                seq_turn += 1
            msg.display_turn = seq_turn

    sessions.sort(
        key=lambda s: s.start_ts or "",
        reverse=newest_first,
    )

    if limit_n is not None:
        all_msgs = []
        for s in sessions:
            for m in s.messages:
                all_msgs.append((s, m))

        if reverse_limit:
            all_msgs = all_msgs[-limit_n:]
        else:
            all_msgs = all_msgs[:limit_n]

        kept_by_session: dict[str, list] = {}
        for s, m in all_msgs:
            if s.session_id not in kept_by_session:
                kept_by_session[s.session_id] = []
            kept_by_session[s.session_id].append(m)

        filtered = []
        for s in sessions:
            if s.session_id in kept_by_session:
                s.messages = kept_by_session[s.session_id]
                filtered.append(s)
        sessions = filtered

    return sessions
