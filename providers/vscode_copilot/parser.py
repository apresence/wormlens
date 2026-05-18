"""VS Code Copilot chat provider backend.

Reads JSONL session files from VS Code's workspaceStorage directory.
These use an incremental state-patching format (kind 0=snapshot, 1=set,
2=splice) that must be reconstructed before extracting messages.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .._base import Provider, strip_extract_bookends
from ...models import ChatMessage, ChatSession, FilterOpts


# -- State machine reconstruction -------------------------------------------


def _deep_set(obj, keys, value):
    for k in keys[:-1]:
        if isinstance(obj, list):
            k = int(k)
            while len(obj) <= k:
                obj.append({})
            obj = obj[k]
        else:
            if k not in obj:
                obj[k] = {}
            obj = obj[k]
    last = keys[-1]
    if isinstance(obj, list):
        last = int(last)
        while len(obj) <= last:
            obj.append(None)
        obj[last] = value
    else:
        obj[last] = value


def _deep_splice(obj, keys, index, delete_count, items):
    for k in keys:
        if isinstance(obj, list):
            k = int(k)
            while len(obj) <= k:
                obj.append({})
            obj = obj[k]
        else:
            if k not in obj:
                obj[k] = []
            obj = obj[k]
    if isinstance(obj, list):
        if isinstance(items, list):
            # JS-style splice: remove delete_count elements at index, insert items
            del obj[index:index + delete_count]
            for i, item in enumerate(items):
                obj.insert(index + i, item)
        elif delete_count > 0:
            del obj[index:index + delete_count]


def _load_session_state(filepath: Path) -> dict | None:
    """Reconstruct final session state from JSONL patch stream."""
    state = None
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(entry, dict):
                continue

            kind = entry.get("kind")
            if kind == 0:
                state = entry.get("v", {})
            elif kind == 1 and state is not None:
                keys = entry.get("k", [])
                value = entry.get("v")
                if keys:
                    try:
                        _deep_set(state, keys, value)
                    except (IndexError, KeyError, TypeError):
                        pass
            elif kind == 2 and state is not None:
                keys = entry.get("k", [])
                value = entry.get("v")
                splice_index = entry.get("i")
                delete_count = entry.get("d", 0)
                if keys:
                    try:
                        if splice_index is not None:
                            items = value if isinstance(value, list) else ([value] if value is not None else [])
                            _deep_splice(state, keys, splice_index, delete_count, items)
                        else:
                            items = value if isinstance(value, list) else ([value] if value is not None else [])
                            _deep_splice(state, keys, 999999, 0, items)
                    except (IndexError, KeyError, TypeError):
                        pass
    return state


# -- Response text extraction ------------------------------------------------


def _extract_response_text(response, opts: FilterOpts) -> str:
    """Extract markdown text from a VS Code response object."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, dict):
        val = response.get("value", "")
        return str(val).strip() if val else ""

    if not isinstance(response, list):
        return str(response).strip()

    parts = []
    for part in response:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue

        kind = part.get("kind")

        if kind is None or kind == "" or kind == "markdownContent":
            if "content" in part:
                content = part["content"]
                val = content.get("value", "") if isinstance(content, dict) else str(content)
            elif "value" in part:
                val = part["value"]
            else:
                val = ""
            if isinstance(val, str) and val.strip():
                parts.append(val)

        elif kind == "thinking":
            if opts.thinking:
                val = part.get("value", "")
                if val and len(val) > 50:
                    parts.append(f"\n<details><summary>Thinking...</summary>\n\n{val}\n\n</details>\n")

        elif kind == "toolInvocationSerialized":
            if opts.tools:
                tool = part.get("toolInvocation", {})
                name = tool.get("toolName", tool.get("name", "unknown tool"))
                inp = tool.get("input", "")
                result = tool.get("result", "")
                parts.append(f"\n> **Tool call:** `{name}`")
                if inp:
                    inp_str = json.dumps(inp, indent=2) if isinstance(inp, (dict, list)) else str(inp)
                    if len(inp_str) > 500:
                        inp_str = inp_str[:500] + "..."
                    parts.append(f"> ```\n> {inp_str}\n> ```")
                if result:
                    res_str = str(result)
                    if len(res_str) > 500:
                        res_str = res_str[:500] + "..."
                    parts.append(f"> **Result:** {res_str}")

        elif kind == "progressTaskSerialized":
            if opts.tools:
                msg = part.get("title", part.get("message", ""))
                if msg:
                    parts.append(f"*[progress: {msg}]*")

        elif kind == "textEditGroup":
            if opts.code_edits:
                uri = part.get("uri", {})
                path = uri.get("path", "") if isinstance(uri, dict) else str(uri)
                parts.append(f"\n> **Code edit:** `{path}`")

        elif kind in ("inlineReference", "codeblockUri"):
            uri = part.get("uri", part.get("inlineReference", {}))
            path = uri.get("path", "") if isinstance(uri, dict) else str(uri)
            if path:
                short = path.rsplit("/", 1)[-1] if "/" in path else path
                parts.append(f"`{short}`")

        elif kind in ("mcpServersStarting", "undoStop", "questionCarousel"):
            pass

        elif "value" in part:
            v = part["value"]
            if isinstance(v, str) and v.strip():
                parts.append(v)

    # Dedup: remove parts whose stripped text is a substring of another
    text_parts = [p for p in parts if isinstance(p, str)]
    deduped = []
    seen = set()
    for p in text_parts:
        ps = p.strip()
        if not ps:
            continue
        is_dup = any(
            ps in q.strip() and ps != q.strip()
            for q in text_parts if q is not p
        )
        if is_dup:
            continue
        if ps in seen:
            continue
        seen.add(ps)
        deduped.append(p)

    text = "\n".join(deduped).strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _extract_user_message(request: dict) -> str:
    msg = request.get("message", {})
    if isinstance(msg, dict):
        return msg.get("text", "").strip()
    if isinstance(msg, str):
        return msg.strip()
    return ""


def _format_timestamp(ts_ms) -> str:
    if not ts_ms:
        return ""
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return dt.isoformat()
    except (OSError, ValueError):
        return ""


# -- Workspace discovery -----------------------------------------------------


def _get_workspace_store() -> Path:
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "Code" / "User" / "workspaceStorage"
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User" / "workspaceStorage"
    return home / ".config" / "Code" / "User" / "workspaceStorage"


def _find_chat_sessions(storage_id: str | None = None, all_workspaces: bool = False) -> list[Path]:
    ws = _get_workspace_store()
    if not ws.is_dir():
        return []

    if storage_id:
        chat_dir = ws / storage_id / "chatSessions"
        if chat_dir.is_dir():
            return sorted(chat_dir.glob("*.jsonl"))
        return []

    candidates = sorted(ws.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)

    if all_workspaces:
        all_files = []
        for d in candidates:
            chat_dir = d / "chatSessions"
            if chat_dir.is_dir():
                all_files.extend(chat_dir.glob("*.jsonl"))
        return sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True)

    for d in candidates:
        chat_dir = d / "chatSessions"
        if chat_dir.is_dir():
            files = sorted(chat_dir.glob("*.jsonl"))
            if files:
                return files
    return []


# -- Provider class ---------------------------------------------------------


class VSCodeCopilotProvider(Provider):
    provider_id = "vscode"
    provider_label = "VS Code Copilot"

    def discovery_roots(self) -> list[Path]:
        return [_get_workspace_store()]

    def discover_sessions(self, storage_id: str | None = None, all_sessions: bool = False, **kwargs) -> list[Path]:
        return _find_chat_sessions(storage_id, all_workspaces=all_sessions)

    def parse_file(
        self,
        path: Path,
        opts: FilterOpts,
        session_id_filter: str | None = None,
        since_last_compact: bool = False,
    ) -> list[ChatSession]:
        state = _load_session_state(path)
        if not state:
            return []

        title = state.get("customTitle", "Untitled Chat")
        creation_date = state.get("creationDate")
        session_id = state.get("sessionId", path.stem)
        requests = state.get("requests", [])

        if not requests:
            return []

        model_ids = set()
        for req in requests:
            if isinstance(req, dict):
                mid = req.get("modelId")
                if mid and mid != "unknown":
                    model_ids.add(mid)

        messages: list[ChatMessage] = []
        start_ts = _format_timestamp(creation_date)

        for req in requests:
            if not isinstance(req, dict):
                continue

            user_msg = strip_extract_bookends(_extract_user_message(req))
            response = req.get("response")
            response_text = _extract_response_text(response, opts)

            if opts.skip_empty and not user_msg and not response_text:
                continue

            if user_msg or not opts.skip_empty:
                messages.append(ChatMessage(
                    role="user",
                    text=user_msg if user_msg else "*[empty message]*",
                    timestamp=start_ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="msg",
                ))

            if response_text or not opts.skip_empty:
                messages.append(ChatMessage(
                    role="assistant",
                    text=response_text if response_text else "*[no response]*",
                    timestamp=start_ts,
                    session_id=session_id,
                    source_file=str(path),
                    msg_type="msg",
                ))

        if not messages:
            return []

        return [ChatSession(
            session_id=session_id,
            title=title or "Untitled Chat",
            start_ts=start_ts,
            end_ts=start_ts,
            source_file=str(path),
            source_type=self.provider_id,
            messages=messages,
            metadata={"model_ids": model_ids} if model_ids else {},
        )]

    def list_sessions_metadata(self, **kwargs) -> list[dict]:
        files = _find_chat_sessions()
        rows = []
        for fpath in files:
            state = _load_session_state(fpath)
            if not state:
                continue

            title = state.get("customTitle", "Untitled Chat")
            creation_date = state.get("creationDate")
            session_id = state.get("sessionId", fpath.stem)
            requests = state.get("requests", [])

            rows.append({
                "session_id": session_id,
                "file": str(fpath),
                "size": fpath.stat().st_size,
                "title": title,
                "start_ts": _format_timestamp(creation_date),
                "turn_count": len(requests),
                "source_type": self.provider_id,
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
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        return False
                    return (
                        isinstance(entry, dict)
                        and "kind" in entry
                        and "v" in entry
                    )
        except (OSError, IOError):
            return False
        return False
