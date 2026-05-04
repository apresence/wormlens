#!/usr/bin/env python3
"""
wormlens-monitor — Background context % monitor for Claude Code.

Watches the active CC JSONL session file, parses token usage from
assistant response records, estimates context fill %, and writes
a status file the agent (or outer loop) can poll.

Runs as a background process alongside CC. Killed by the outer loop
when a session handoff occurs.

Usage:
    python -m wormlens.harness.monitor [--ctx-limit 200000] [--status-file .wormlens/status]
"""

import json
import os
import sys
import time
from pathlib import Path


DEFAULT_CTX_LIMIT = 200_000  # tokens — CC default for Opus/Sonnet
STATUS_DIR = ".wormlens"
STATUS_FILE = "status"
POLL_INTERVAL = 5  # seconds


def find_active_session() -> Path | None:
    """Find the most recently modified CC JSONL session file."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    projects_dir = Path(config_dir) / "projects"
    if not projects_dir.is_dir():
        return None

    newest = None
    newest_mtime = 0
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.iterdir():
            if f.suffix == ".jsonl" and f.is_file():
                mt = f.stat().st_mtime
                if mt > newest_mtime:
                    newest = f
                    newest_mtime = mt
    return newest


def parse_token_usage(session_path: Path) -> dict:
    """Parse the session JSONL for token usage stats.

    CC assistant records contain a top-level `usage` dict with:
      inputTokens, outputTokens, cacheCreationInputTokens, cacheReadInputTokens

    We also count records to estimate conversation depth.
    """
    total_input = 0
    total_output = 0
    total_cache_create = 0
    total_cache_read = 0
    user_turns = 0
    assistant_turns = 0
    last_usage = {}
    compact_count = 0
    file_size = session_path.stat().st_size

    with open(session_path, "rb") as f:
        for raw_line in f:
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue

            rec_type = record.get("type", "")

            if rec_type == "user":
                user_turns += 1
            elif rec_type == "assistant":
                assistant_turns += 1
                usage = record.get("usage", {})
                if usage:
                    last_usage = usage
                    total_input = max(total_input, usage.get("inputTokens", 0))
                    total_output += usage.get("outputTokens", 0)
                    total_cache_create = usage.get("cacheCreationInputTokens", 0)
                    total_cache_read = usage.get("cacheReadInputTokens", 0)
            elif rec_type == "system" and record.get("subtype") == "compact_boundary":
                compact_count += 1

    # Best estimate of current context window usage:
    # The last assistant record's inputTokens is the closest proxy —
    # it represents everything the model saw on its most recent turn.
    current_ctx = last_usage.get("inputTokens", total_input)

    return {
        "current_ctx_tokens": current_ctx,
        "total_output_tokens": total_output,
        "cache_create": total_cache_create,
        "cache_read": total_cache_read,
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "compact_count": compact_count,
        "file_size_bytes": file_size,
        "session_file": str(session_path),
    }


def compute_status(usage: dict, ctx_limit: int) -> dict:
    """Compute context fill % and warning level."""
    current = usage["current_ctx_tokens"]
    pct = (current / ctx_limit * 100) if ctx_limit > 0 else 0

    if pct >= 95:
        level = "CRITICAL"
    elif pct >= 90:
        level = "HANDOFF_NOW"
    elif pct >= 80:
        level = "WARNING"
    elif pct >= 70:
        level = "CAUTION"
    else:
        level = "OK"

    return {
        "context_pct": round(pct, 1),
        "context_tokens": current,
        "context_limit": ctx_limit,
        "context_remaining": ctx_limit - current,
        "level": level,
        "turns": usage["user_turns"] + usage["assistant_turns"],
        "user_turns": usage["user_turns"],
        "compacts": usage["compact_count"],
        "file_size": usage["file_size_bytes"],
        "session_file": usage["session_file"],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def write_status(status: dict, status_dir: str = STATUS_DIR):
    """Write status to a file the agent or outer loop can read."""
    Path(status_dir).mkdir(parents=True, exist_ok=True)
    status_path = Path(status_dir) / STATUS_FILE

    # Write as simple key=value for easy bash parsing AND json for agent
    with open(status_path, "w") as f:
        f.write(json.dumps(status, indent=2))

    # Also write a one-liner for quick bash reads
    oneliner_path = Path(status_dir) / "ctx_pct"
    with open(oneliner_path, "w") as f:
        f.write(f"{status['context_pct']}")


def monitor_loop(ctx_limit: int, status_dir: str, poll_interval: float):
    """Main monitoring loop. Runs until killed."""
    print(f"[wormlens-monitor] Context limit: {ctx_limit:,} tokens", file=sys.stderr)
    print(f"[wormlens-monitor] Status dir: {status_dir}", file=sys.stderr)
    print(f"[wormlens-monitor] Poll interval: {poll_interval}s", file=sys.stderr)

    last_level = None
    last_session = None

    while True:
        try:
            session = find_active_session()
            if session is None:
                time.sleep(poll_interval)
                continue

            if str(session) != last_session:
                print(f"[wormlens-monitor] Tracking: {session.name}", file=sys.stderr)
                last_session = str(session)

            usage = parse_token_usage(session)
            status = compute_status(usage, ctx_limit)
            write_status(status, status_dir)

            # Log level changes to stderr
            if status["level"] != last_level:
                pct = status["context_pct"]
                remaining = status["context_remaining"]
                print(
                    f"[wormlens-monitor] {status['level']}: "
                    f"{pct}% ({remaining:,} tokens remaining)",
                    file=sys.stderr,
                )
                last_level = status["level"]

        except Exception as e:
            print(f"[wormlens-monitor] Error: {e}", file=sys.stderr)

        time.sleep(poll_interval)


def main():
    import argparse
    p = argparse.ArgumentParser(description="Wormlens context monitor")
    p.add_argument("--ctx-limit", type=int, default=DEFAULT_CTX_LIMIT,
                    help=f"Context window size in tokens (default: {DEFAULT_CTX_LIMIT:,})")
    p.add_argument("--status-dir", default=STATUS_DIR)
    p.add_argument("--poll-interval", type=float, default=POLL_INTERVAL)
    args = p.parse_args()

    monitor_loop(args.ctx_limit, args.status_dir, args.poll_interval)


if __name__ == "__main__":
    main()
