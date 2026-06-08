"""Claude.ai web export provider backend.

Parses the `conversations.json` file produced by claude.ai's "Export
Data" feature. The file is a single JSON array containing every
conversation on the account; each conversation has a flat
`chat_messages` list (not the tree-mapped structure ChatGPT exports
use), so the only graph traversal we need is "iterate in order".

Unlike the Claude Code / Codex / VS Code Copilot providers, this one
deals with a one-shot data dump rather than live-on-disk session files.
There is no canonical install location, so discovery is intentionally
disabled -- pass the export path on the command line.

Record types we handle in `chat_messages[].content[]`:
    text                 -- user/assistant text
    thinking             -- extended thinking blocks
    tool_use             -- assistant tool invocations
    tool_result          -- tool outputs (rare; usually inlined into next msg)
    image / *_attachment -- represented as `[attachment: ...]` placeholders
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import orjson  # noqa: F401
    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False

from .._base import Provider, session_id_matches, strip_extract_bookends
from ...models import ChatMessage, ChatSession, FilterOpts


def _loads(data: bytes):
    if _HAS_ORJSON:
        import orjson
        return orjson.loads(data)
    return json.loads(data)


def _load_export(path: Path) -> list[dict] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = _loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    return data


def _content_text(content) -> str:
    """Flatten content blocks of type=text into a single string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            t = item.get("text", "")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)


def _thinking_text(content) -> str:
    """Flatten content blocks of type=thinking into a single string."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "thinking":
            continue
        t = item.get("thinking", "")
        if isinstance(t, str) and t:
            parts.append(t)
    return "\n\n".join(parts)


def _format_tool_use(block: dict) -> str:
    name = block.get("name", "?")
    inp = block.get("input", "")
    if not isinstance(inp, str):
        try:
            inp = json.dumps(inp)
        except (TypeError, ValueError):
            inp = str(inp)
    return f"[tool_call] name={name} input={inp}"


def _format_tool_result(block: dict) -> str:
    out = block.get("content", "")
    if isinstance(out, list):
        parts = []
        for item in out:
            if isinstance(item, dict):
                t = item.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    if not isinstance(out, str):
        try:
            return json.dumps(out)
        except (TypeError, ValueError):
            return str(out)
    return out


def _attachment_refs(msg: dict) -> list[str]:
    refs: list[str] = []
    for att in msg.get("attachments", []) or []:
        if isinstance(att, dict):
            name = att.get("file_name") or att.get("name") or "attachment"
            refs.append(f"[attachment: {name}]")
    for f in msg.get("files", []) or []:
        if isinstance(f, dict):
            name = f.get("file_name") or f.get("name") or "file"
            refs.append(f"[file: {name}]")
    return refs


def _conv_to_session(conv: dict, path: Path, opts: FilterOpts) -> ChatSession | None:
    if not isinstance(conv, dict):
        return None

    session_id = conv.get("uuid") or ""
    if not session_id:
        return None

    title = conv.get("name") or "Untitled"
    start_ts = conv.get("created_at", "") or ""
    end_ts = conv.get("updated_at", "") or start_ts
    summary = conv.get("summary") or ""

    messages: list[ChatMessage] = []
    last_ts = start_ts

    for idx, msg in enumerate(conv.get("chat_messages", []) or []):
        if not isinstance(msg, dict):
            continue
        sender = msg.get("sender", "")
        role = "user" if sender == "human" else "assistant" if sender == "assistant" else (sender or "unknown")
        ts = msg.get("created_at") or last_ts
        last_ts = ts
        msg_uuid = msg.get("uuid", "")
        content_blocks = msg.get("content", []) if isinstance(msg.get("content"), list) else []

        # Thinking blocks (assistant only in practice, but don't gate on role).
        if opts.thinking:
            thinking = _thinking_text(content_blocks)
            if thinking or not opts.skip_empty:
                messages.append(ChatMessage(
                    role="assistant",
                    text=thinking,
                    timestamp=ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="thinking",
                    source_line=idx + 1,
                    metadata={"message_uuid": msg_uuid},
                ))

        # Text body. Prefer concatenated content[] type=text blocks; fall
        # back to top-level `text` field if content is missing.
        text = _content_text(content_blocks)
        if not text:
            top = msg.get("text", "")
            if isinstance(top, str):
                text = top
        if role == "user" and opts.strip_tags:
            text = strip_extract_bookends(text)
        if text or not opts.skip_empty:
            messages.append(ChatMessage(
                role=role,
                text=text,
                timestamp=ts,
                session_id=session_id,
                source_file=str(path),
                msg_type="msg",
                source_line=idx + 1,
                metadata={"message_uuid": msg_uuid},
            ))

        # Tool use / tool result blocks (newer claude.ai exports).
        if opts.tools:
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    messages.append(ChatMessage(
                        role="assistant",
                        text=_format_tool_use(block),
                        timestamp=ts,
                        session_id=session_id,
                        source_file=str(path),
                        msg_type="tool_use",
                        source_line=idx + 1,
                        metadata={
                            "name": block.get("name", ""),
                            "tool_use_id": block.get("id", ""),
                            "message_uuid": msg_uuid,
                        },
                    ))
                elif btype == "tool_result":
                    messages.append(ChatMessage(
                        role="tool",
                        text=_format_tool_result(block),
                        timestamp=ts,
                        session_id=session_id,
                        source_file=str(path),
                        msg_type="tool_result",
                        source_line=idx + 1,
                        metadata={
                            "tool_use_id": block.get("tool_use_id", ""),
                            "message_uuid": msg_uuid,
                        },
                    ))

        # Attachments as refs.
        if opts.refs:
            for ref in _attachment_refs(msg):
                messages.append(ChatMessage(
                    role=role,
                    text=ref,
                    timestamp=ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="ref",
                    source_line=idx + 1,
                    metadata={"message_uuid": msg_uuid},
                ))

    if not messages:
        return None

    return ChatSession(
        session_id=session_id,
        title=title,
        start_ts=start_ts,
        end_ts=end_ts,
        source_file=str(path),
        source_type="claude_ai",
        messages=messages,
        metadata={
            "summary": summary,
            "account_uuid": (conv.get("account") or {}).get("uuid", ""),
            "message_count": len(conv.get("chat_messages", []) or []),
        },
    )


class ClaudeAIProvider(Provider):
    provider_id = "claude_ai"
    provider_label = "Claude.ai web export"

    def discover_sessions(self, **kwargs) -> list[Path]:
        # No canonical install location for a one-shot export -- the
        # user must pass the file path explicitly.
        return []

    def parse_file(
        self,
        path: Path,
        opts: FilterOpts,
        session_id_filter: str | None = None,
        since_last_compact: bool = False,
    ) -> list[ChatSession]:
        data = _load_export(path)
        if data is None:
            return []

        sessions: list[ChatSession] = []
        for conv in data:
            conv_uuid = conv.get("uuid") if isinstance(conv, dict) else None
            if session_id_filter and not session_id_matches(conv_uuid, session_id_filter):
                continue
            sess = _conv_to_session(conv, path, opts)
            if sess is not None:
                sessions.append(sess)
        return sessions

    def list_sessions_metadata(self, paths: list[Path] | None = None, **kwargs) -> list[dict]:
        if not paths:
            return []
        rows: list[dict] = []
        for fpath in paths:
            data = _load_export(fpath)
            if data is None:
                continue
            for conv in data:
                if not isinstance(conv, dict):
                    continue
                msgs = conv.get("chat_messages", []) or []
                user_count = sum(1 for m in msgs if isinstance(m, dict) and m.get("sender") == "human")
                asst_count = sum(1 for m in msgs if isinstance(m, dict) and m.get("sender") == "assistant")
                preview: list[str] = []
                for m in msgs[:2]:
                    if not isinstance(m, dict):
                        continue
                    t = _content_text(m.get("content", []))
                    if not t:
                        t = m.get("text", "") if isinstance(m.get("text"), str) else ""
                    if t:
                        preview.append(t[:120].replace("\n", " "))
                # Approximate per-conversation size by re-serializing.
                # The whole-file size would be the same for every row
                # since they all live in one big JSON array, which is
                # misleading. orjson is ~5x faster for this if present.
                try:
                    if _HAS_ORJSON:
                        import orjson
                        conv_size = len(orjson.dumps(conv))
                    else:
                        conv_size = len(json.dumps(conv).encode("utf-8"))
                except (TypeError, ValueError):
                    conv_size = 0
                rows.append({
                    "session_id": conv.get("uuid", ""),
                    "file": str(fpath),
                    "size": conv_size,
                    "title": conv.get("name") or (preview[0] if preview else "Claude.ai conversation"),
                    "preview": preview,
                    "start_ts": conv.get("created_at", ""),
                    "turn_count": user_count,
                    "user_count": user_count,
                    "assistant_count": asst_count,
                    "source_type": self.provider_id,
                })
        return rows

    @classmethod
    def detect(cls, path: Path) -> bool:
        """Sniff the file head for the claude.ai export shape.

        The export is a single (multi-hundred-MB) JSON array, so loading
        the whole thing on every probe would be ruinous. Instead we just
        confirm the file starts with `[{` and the head contains all
        three of the required top-level keys, which together are unique
        to this format (CC sessions are JSONL, ChatGPT exports start
        with `[{` but use `mapping`/`current_node`, .wl extracts start
        with text).
        """
        try:
            with open(path, "rb") as f:
                head = f.read(65536)
        except (OSError, IOError):
            return False
        stripped = head.lstrip()
        if not stripped.startswith(b"[{"):
            return False
        if b"\"chat_messages\"" not in head:
            return False
        if b"\"account\"" not in head:
            return False
        if b"\"uuid\"" not in head:
            return False
        return True
