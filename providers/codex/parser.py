"""OpenAI Codex CLI provider backend.

Reads rollout JSONL files from $CODEX_HOME/sessions/YYYY/MM/DD/. Each
line is one event with a top-level discriminator (type) and a payload
shaped per OpenAI's Responses API.

Record types we care about:
    session_meta      -- first record per file (one per logical session)
    response_item     -- canonical conversation events (message, reasoning,
                         function_call, function_call_output, etc.)
    compacted         -- summary boundary; recall mode slices after this
    turn_context      -- per-turn config (skipped in v0.1)
    event_msg         -- event-stream noise; duplicates of response_items;
                         filtered out by default

Resume behavior: codex exec resume --last appends to the same rollout
file, so one rollout = one logical session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .._base import Provider, strip_extract_bookends
from ...models import ChatMessage, ChatSession, FilterOpts


# Synthetic role=user messages that wrap real prompts; emitted once per
# session by Codex before the actual user input. Filtered unless
# system_msgs is set.
_SYSTEM_USER_TAGS = (
    "<environment_context>",
    "<apps_instructions>",
    "<skills_instructions>",
    "<permissions instructions>",
)


def _get_codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def _find_rollouts(all_sessions: bool = False) -> list[Path]:
    """Return rollouts under $CODEX_HOME/sessions, sorted newest first."""
    home = _get_codex_home()
    sessions_dir = home / "sessions"
    files: list[Path] = []
    if sessions_dir.is_dir():
        files.extend(sessions_dir.rglob("rollout-*.jsonl"))
    if all_sessions:
        archived = home / "archived_sessions"
        if archived.is_dir():
            files.extend(archived.rglob("rollout-*.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _content_text(content) -> str:
    """Extract concatenated text from a response_item.message content list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in ("input_text", "output_text", "summary_text"):
            txt = item.get("text", "")
            if isinstance(txt, str):
                parts.append(txt)
        elif t == "input_image":
            url = item.get("image_url", "")
            parts.append(f"[image: {url}]")
    return "".join(parts)


def _is_system_user(text: str) -> bool:
    """Detect synthetic role=user messages (env_context, instructions, etc.)."""
    if not text:
        return False
    head = text.lstrip()[:80]
    return any(head.startswith(tag) for tag in _SYSTEM_USER_TAGS)


def _format_function_call(payload: dict) -> str:
    name = payload.get("name", "?")
    namespace = payload.get("namespace", "")
    qualified = f"{namespace}{name}" if namespace else name
    args = payload.get("arguments", "")
    if not isinstance(args, str):
        try:
            args = json.dumps(args)
        except (TypeError, ValueError):
            args = str(args)
    return f"[tool_call] name={qualified} args={args}"


def _format_web_search_call(payload: dict) -> str:
    """web_search_call has no separate output; query lives in payload.action."""
    action = payload.get("action") or {}
    if isinstance(action, dict):
        queries = action.get("queries")
        if isinstance(queries, list) and queries:
            joined = " | ".join(str(q) for q in queries)
        else:
            joined = str(action.get("query", ""))
    else:
        joined = ""
    status = payload.get("status", "")
    return f"[web_search] status={status} queries={joined}"


def _format_function_call_output(payload: dict) -> str:
    out = payload.get("output", "")
    if isinstance(out, list):
        parts = []
        for item in out:
            if isinstance(item, dict) and "text" in item:
                t = item.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return str(out)


def _format_reasoning(payload: dict) -> str:
    parts: list[str] = []
    summary = payload.get("summary", [])
    if isinstance(summary, list):
        for s in summary:
            if isinstance(s, dict):
                t = s.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
    content = payload.get("content")
    if isinstance(content, list):
        body = _content_text(content)
        if body:
            parts.append(body)
    return "\n".join(p for p in parts if p)


def _format_compacted(payload: dict) -> str:
    msg = payload.get("message", "")
    return str(msg)


def _process_record_codex(
    rec: dict,
    opts: FilterOpts,
    state: dict,
) -> list[ChatMessage]:
    """Process one Codex JSONL record into ChatMessage objects.

    Streaming + batch entry point. `state` carries cross-record context
    populated from session_meta records:
      - session_id, start_ts, last_ts
      - cwd, cli_version, model_provider, originator

    Returned ChatMessages do NOT have source_file or source_line set;
    callers stamp those after (batch sets both, streaming may set
    source_file only).

    Returns empty list for ignored records (turn_context, event_msg,
    session_meta itself once consumed).
    """
    if not isinstance(rec, dict):
        return []
    rtype = rec.get("type")
    payload = rec.get("payload", {}) or {}
    ts = rec.get("timestamp", "") or state.get("last_ts", "")
    state["last_ts"] = ts
    out: list[ChatMessage] = []

    if rtype == "session_meta":
        pl = payload
        state.setdefault("session_id", pl.get("id", ""))
        state.setdefault("start_ts", pl.get("timestamp", "") or rec.get("timestamp", ""))
        state.setdefault("cwd", pl.get("cwd", ""))
        state.setdefault("cli_version", pl.get("cli_version", ""))
        state.setdefault("model_provider", pl.get("model_provider", ""))
        state.setdefault("originator", pl.get("originator", ""))
        return out

    if rtype in ("turn_context", "event_msg"):
        return out

    session_id = state.get("session_id", "")

    if rtype == "compacted":
        if opts.compact_markers:
            out.append(ChatMessage(
                role="system",
                text=f"[compact summary] {_format_compacted(payload)}",
                timestamp=ts,
                session_id=session_id,
                msg_type="compact",
            ))
        return out

    if rtype != "response_item":
        return out

    inner = payload.get("type")

    if inner == "message":
        role = payload.get("role", "") or "unknown"
        text = _content_text(payload.get("content", []))

        if role == "developer":
            if opts.system_msgs:
                out.append(ChatMessage(
                    role=role,
                    text=text,
                    timestamp=ts,
                    session_id=session_id,
                    msg_type="system_inject",
                ))
            return out

        if role == "user" and _is_system_user(text):
            if opts.system_msgs:
                out.append(ChatMessage(
                    role=role,
                    text=text,
                    timestamp=ts,
                    session_id=session_id,
                    msg_type="system_inject",
                ))
            return out

        if role == "user" and opts.strip_tags:
            text = strip_extract_bookends(text)

        if not text and opts.skip_empty:
            return out

        out.append(ChatMessage(
            role=role,
            text=text,
            timestamp=ts,
            session_id=session_id,
            msg_type="msg",
        ))
        return out

    if inner == "reasoning":
        if not opts.thinking:
            return out
        text = _format_reasoning(payload)
        if not text and opts.skip_empty:
            return out
        out.append(ChatMessage(
            role="assistant",
            text=text,
            timestamp=ts,
            session_id=session_id,
            msg_type="thinking",
        ))
        return out

    if inner in (
        "function_call",
        "local_shell_call",
        "tool_search_call",
        "custom_tool_call",
    ):
        if not opts.tools:
            return out
        meta = {
            "call_id": payload.get("call_id", ""),
            "name": payload.get("name", ""),
        }
        ns = payload.get("namespace", "")
        if ns:
            meta["namespace"] = ns
        out.append(ChatMessage(
            role="assistant",
            text=_format_function_call(payload),
            timestamp=ts,
            session_id=session_id,
            msg_type="tool_use",
            metadata=meta,
        ))
        return out

    if inner == "web_search_call":
        if not opts.tools:
            return out
        out.append(ChatMessage(
            role="assistant",
            text=_format_web_search_call(payload),
            timestamp=ts,
            session_id=session_id,
            msg_type="tool_use",
            metadata={"name": "web_search", "status": payload.get("status", "")},
        ))
        return out

    if inner in ("function_call_output", "custom_tool_call_output"):
        if not opts.tools:
            return out
        out.append(ChatMessage(
            role="tool",
            text=_format_function_call_output(payload),
            timestamp=ts,
            session_id=session_id,
            msg_type="tool_result",
            metadata={"call_id": payload.get("call_id", "")},
        ))
        return out

    return out


class CodexProvider(Provider):
    provider_id = "codex"
    provider_label = "OpenAI Codex"

    def discover_sessions(self, all_sessions: bool = False, **kwargs) -> list[Path]:
        return _find_rollouts(all_sessions=all_sessions)

    def parse_file(
        self,
        path: Path,
        opts: FilterOpts,
        session_id_filter: str | None = None,
        since_last_compact: bool = False,
    ) -> list[ChatSession]:
        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

        records: list[tuple[int, dict]] = []
        for lineno, line in enumerate(raw_lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            records.append((lineno, rec))

        if not records:
            return []

        # Pre-pass for session_meta: locate it anywhere in the file (safety
        # net for malformed transcripts where meta is not first), and apply
        # path.stem fallback before the session_id_filter check.
        state: dict = {}
        for _, rec in records:
            if rec.get("type") == "session_meta":
                _process_record_codex(rec, opts, state)
                break
        if not state.get("session_id"):
            state["session_id"] = path.stem

        if session_id_filter and state["session_id"] != session_id_filter:
            return []

        # Recall mode: slice records to start AFTER the last compacted record.
        slice_start = 0
        if since_last_compact:
            for i in range(len(records) - 1, -1, -1):
                if records[i][1].get("type") == "compacted":
                    slice_start = i + 1
                    break

        messages: list[ChatMessage] = []
        for lineno, rec in records[slice_start:]:
            for m in _process_record_codex(rec, opts, state):
                m.source_file = str(path)
                m.source_line = lineno
                messages.append(m)

        if not messages:
            return []

        session_id = state.get("session_id", "")
        title = f"Codex {session_id[:8]}" if session_id else "Codex session"

        return [ChatSession(
            session_id=session_id,
            title=title,
            start_ts=state.get("start_ts", ""),
            end_ts=state.get("last_ts", "") or state.get("start_ts", ""),
            source_file=str(path),
            source_type=self.provider_id,
            messages=messages,
            metadata={
                "cwd": state.get("cwd", ""),
                "cli_version": state.get("cli_version", ""),
                "model_provider": state.get("model_provider", ""),
                "originator": state.get("originator", ""),
            },
        )]

    def parse_line(
        self,
        raw_line: str,
        opts: FilterOpts,
        state: dict,
    ) -> list[ChatMessage]:
        """Parse one JSONL line into ChatMessage objects (streaming entry).

        Thin shim over _process_record_codex. `state` carries cross-record
        bookkeeping (session_id, last_ts, etc.) populated lazily from
        session_meta records.
        """
        raw_line = raw_line.strip()
        if not raw_line:
            return []
        try:
            rec = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            return []
        return _process_record_codex(rec, opts, state)

    def list_sessions_metadata(self, **kwargs) -> list[dict]:
        rows: list[dict] = []
        for fpath in _find_rollouts():
            try:
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            session_id = fpath.stem
            start_ts = ""
            cwd = ""
            cli_version = ""
            originator = ""
            preview_parts: list[str] = []
            user_count = 0
            assistant_count = 0

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                pl = rec.get("payload", {}) or {}
                rtype = rec.get("type")
                if rtype == "session_meta":
                    session_id = pl.get("id", session_id)
                    start_ts = pl.get("timestamp", "")
                    cwd = pl.get("cwd", "")
                    cli_version = pl.get("cli_version", "")
                    originator = pl.get("originator", "")
                elif rtype == "response_item" and pl.get("type") == "message":
                    role = pl.get("role", "")
                    text = _content_text(pl.get("content", []))
                    if role == "user":
                        if text and not _is_system_user(text):
                            user_count += 1
                            if len(preview_parts) < 2:
                                preview_parts.append(text[:120].replace("\n", " "))
                    elif role == "assistant":
                        if text:
                            assistant_count += 1

            rows.append({
                "session_id": session_id,
                "file": str(fpath),
                "size": fpath.stat().st_size,
                "title": preview_parts[0] if preview_parts else "Codex session",
                "preview": preview_parts,
                "start_ts": start_ts,
                "turn_count": user_count,
                "user_count": user_count,
                "assistant_count": assistant_count,
                "source_type": self.provider_id,
                "cwd": cwd,
                "cli_version": cli_version,
                "originator": originator,
            })
        return rows

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        return False
                    if not isinstance(rec, dict):
                        return False
                    if rec.get("type") != "session_meta":
                        return False
                    pl = rec.get("payload", {})
                    return (
                        isinstance(pl, dict)
                        and "id" in pl
                        and "cli_version" in pl
                    )
        except (OSError, IOError):
            return False
        return False
