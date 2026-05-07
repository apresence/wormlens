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

        # session_meta is the first record (or earliest if file is malformed).
        session_id = ""
        start_ts = ""
        cwd = ""
        cli_version = ""
        model_provider = ""
        originator = ""
        for _, rec in records:
            if rec.get("type") == "session_meta":
                pl = rec.get("payload", {}) or {}
                session_id = pl.get("id", "")
                start_ts = pl.get("timestamp", "") or rec.get("timestamp", "")
                cwd = pl.get("cwd", "")
                cli_version = pl.get("cli_version", "")
                model_provider = pl.get("model_provider", "")
                originator = pl.get("originator", "")
                break
        if not session_id:
            session_id = path.stem

        if session_id_filter and session_id != session_id_filter:
            return []

        # Recall mode: slice records to start AFTER the last compacted record.
        slice_start = 0
        if since_last_compact:
            for i in range(len(records) - 1, -1, -1):
                if records[i][1].get("type") == "compacted":
                    slice_start = i + 1
                    break

        messages: list[ChatMessage] = []
        last_ts = start_ts

        for lineno, rec in records[slice_start:]:
            rtype = rec.get("type")
            payload = rec.get("payload", {}) or {}
            ts = rec.get("timestamp", "") or last_ts
            last_ts = ts

            if rtype in ("session_meta", "turn_context"):
                continue

            # event_msg records duplicate response_items (user_message,
            # agent_message, agent_reasoning) and carry lifecycle/usage
            # noise (token_count, task_started, task_complete). Filter all
            # by default. Future flag could surface token_count for stats.
            if rtype == "event_msg":
                continue

            if rtype == "compacted":
                if opts.compact_markers:
                    messages.append(ChatMessage(
                        role="system",
                        text=f"[compact summary] {_format_compacted(payload)}",
                        timestamp=ts,
                        session_id=session_id,
                        source_file=str(path),
                        msg_type="compact",
                        source_line=lineno,
                    ))
                continue

            if rtype != "response_item":
                continue

            inner = payload.get("type")

            if inner == "message":
                role = payload.get("role", "") or "unknown"
                text = _content_text(payload.get("content", []))

                # role=developer is always synthetic permissions/apps/skills.
                if role == "developer":
                    if opts.system_msgs:
                        messages.append(ChatMessage(
                            role=role,
                            text=text,
                            timestamp=ts,
                            session_id=session_id,
                            source_file=str(path),
                            msg_type="system_inject",
                            source_line=lineno,
                        ))
                    continue

                # role=user can be synthetic (env_context). Detect by tag.
                if role == "user" and _is_system_user(text):
                    if opts.system_msgs:
                        messages.append(ChatMessage(
                            role=role,
                            text=text,
                            timestamp=ts,
                            session_id=session_id,
                            source_file=str(path),
                            msg_type="system_inject",
                            source_line=lineno,
                        ))
                    continue

                if role == "user" and opts.strip_tags:
                    text = strip_extract_bookends(text)

                if not text and opts.skip_empty:
                    continue

                messages.append(ChatMessage(
                    role=role,
                    text=text,
                    timestamp=ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="msg",
                    source_line=lineno,
                ))
                continue

            if inner == "reasoning":
                if not opts.thinking:
                    continue
                text = _format_reasoning(payload)
                if not text and opts.skip_empty:
                    continue
                messages.append(ChatMessage(
                    role="assistant",
                    text=text,
                    timestamp=ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="thinking",
                    source_line=lineno,
                ))
                continue

            if inner in (
                "function_call",
                "local_shell_call",
                "tool_search_call",
                "custom_tool_call",
            ):
                if not opts.tools:
                    continue
                meta = {
                    "call_id": payload.get("call_id", ""),
                    "name": payload.get("name", ""),
                }
                ns = payload.get("namespace", "")
                if ns:
                    meta["namespace"] = ns
                messages.append(ChatMessage(
                    role="assistant",
                    text=_format_function_call(payload),
                    timestamp=ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="tool_use",
                    source_line=lineno,
                    metadata=meta,
                ))
                continue

            if inner == "web_search_call":
                if not opts.tools:
                    continue
                messages.append(ChatMessage(
                    role="assistant",
                    text=_format_web_search_call(payload),
                    timestamp=ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="tool_use",
                    source_line=lineno,
                    metadata={"name": "web_search", "status": payload.get("status", "")},
                ))
                continue

            if inner in ("function_call_output", "custom_tool_call_output"):
                if not opts.tools:
                    continue
                messages.append(ChatMessage(
                    role="tool",
                    text=_format_function_call_output(payload),
                    timestamp=ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="tool_result",
                    source_line=lineno,
                    metadata={"call_id": payload.get("call_id", "")},
                ))
                continue

            # Unknown response_item.inner types are ignored.

        if not messages:
            return []

        title = f"Codex {session_id[:8]}" if session_id else "Codex session"

        return [ChatSession(
            session_id=session_id,
            title=title,
            start_ts=start_ts,
            end_ts=last_ts or start_ts,
            source_file=str(path),
            source_type=self.provider_id,
            messages=messages,
            metadata={
                "cwd": cwd,
                "cli_version": cli_version,
                "model_provider": model_provider,
                "originator": originator,
            },
        )]

    def list_sessions_metadata(self) -> list[dict]:
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
