#!/usr/bin/env python3
"""wormlens CC hook -- statusline + context injection.

Handles two modes:
  - StatusLine: writes ctx.json, prints minimal status (wl:on)
  - UserPromptSubmit/PreToolUse: injects time + context_left

Designed to be project-scoped, not global. Install via wl --install-skill.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"

_VALID_SID_RE = re.compile(r"^[a-f0-9-]{1,64}$")


def _valid_session_id(sid: str | None) -> bool:
    return bool(sid and _VALID_SID_RE.match(sid))


def ctx_path(session_id: str) -> Path | None:
    if not _valid_session_id(session_id):
        return None
    d = SESSIONS_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "ctx.json"


def get_local_time() -> str:
    """ISO 8601 local time with UTC offset and DOW."""
    now = datetime.now(timezone.utc).astimezone()
    offset = now.strftime("%z")  # e.g. -0400
    dow = now.strftime("%A")
    return now.strftime(f"%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d} {offset} {dow}"


def read_ctx(session_id: str) -> dict | None:
    """Read ctx.json for a session. Returns None if unavailable."""
    if not _valid_session_id(session_id):
        return None
    p = ctx_path(session_id)
    if p is None:
        return None
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def handle_statusline(data: dict):
    """Write ctx.json, print minimal statusline."""
    sid = data.get("session_id")
    if _valid_session_id(sid):
        p = ctx_path(sid)
        if p is not None:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(p)

    print("wl:on")


def handle_hook(data: dict):
    """Inject time + context_left into UserPromptSubmit/PreToolUse."""
    event = data.get("hook_event_name", "")
    sid = data.get("session_id")

    prefix = {"time": get_local_time()}

    ctx_data = read_ctx(sid) if _valid_session_id(sid) else None
    if ctx_data:
        cw = ctx_data.get("context_window", {})
        used = cw.get("used_percentage")
        remaining = cw.get("remaining_percentage")
        if used is not None and remaining is not None:
            prefix["context_used_pct"] = round(used, 1)
            prefix["context_remaining_pct"] = round(remaining, 1)
            if used >= 90:
                urgent_msg = (
                    f"URGENT: Context critically low ({round(used, 1)}% used). "
                    f"Write <wl-summary>session description</wl-summary> then "
                    f"call `wl --handoff --session {sid}` NOW."
                ) if _valid_session_id(sid) else (
                    f"URGENT: Context critically low ({round(used, 1)}% used). "
                    f"Write <wl-summary>session description</wl-summary> NOW."
                )
                prefix["urgent"] = urgent_msg
    else:
        prefix["context_used_pct"] = "unavailable (first turn)"
        prefix["context_remaining_pct"] = "unavailable (first turn)"

    result = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": json.dumps(prefix),
        }
    }
    print(json.dumps(result))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        print('{"continue": true}')
        return

    event = data.get("hook_event_name")
    has_ctx = "context_window" in data

    if event:
        handle_hook(data)
    elif has_ctx:
        handle_statusline(data)
    else:
        print('{"continue": true}')


if __name__ == "__main__":
    main()
