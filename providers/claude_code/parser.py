"""Claude Code chat provider backend.

Reads JSONL session logs from $CLAUDE_CONFIG_DIR/projects/ and emits
ChatSession/ChatMessage objects. Handles compact_boundary markers,
recovery mode, and all CC-specific record types.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

from .._base import Provider, strip_extract_bookends
from ...models import ChatMessage, ChatSession, FilterOpts


MIN_SESSION_SIZE = 100 * 1024  # 100KB

_SYSTEM_TAG_RES = [
    re.compile(r'<local-command-caveat>.*?</local-command-caveat>', re.DOTALL),
    re.compile(r'<system-reminder>.*?</system-reminder>', re.DOTALL),
    re.compile(r'<available-deferred-tools>.*?</available-deferred-tools>', re.DOTALL),
    re.compile(r'<fast_mode_info>.*?</fast_mode_info>', re.DOTALL),
    re.compile(r'<wormlens-boot>.*?</wormlens-boot>', re.DOTALL),
]

# Tags that indicate a user record is actually a system-injected message
_SYSTEM_INJECT_TAGS = [
    '<local-command-caveat>',
    '<local-command-stdout>',
    '<local-command-stderr>',
    '<command-name>',
]

_COMMAND_RE = re.compile(
    r'\s*<command-name>(.*?)</command-name>\s*'
    r'(?:<command-message>.*?</command-message>)?\s*'
    r'(?:<command-args>.*?</command-args>)?\s*',
    re.DOTALL,
)

_TEAMMATE_RE = re.compile(
    r'<teammate-message\s+teammate_id="([^"]+)">\s*(.*?)\s*</teammate-message>',
    re.DOTALL,
)

_CONTINUATION_PREFIX = "This session is being continued from a previous conversation"


# -- Helpers -----------------------------------------------------------------


def _get_projects_dir() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        projects = Path(config_dir) / "projects"
        if projects.is_dir():
            return projects
    home = os.environ.get("HOME", str(Path.home()))
    return Path(home) / ".claude" / "projects"


def _all_session_jsonls() -> list[Path]:
    projects_dir = _get_projects_dir()
    if not projects_dir.is_dir():
        return []
    candidates = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.iterdir():
            if f.suffix == ".jsonl" and f.is_file():
                candidates.append(f)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates


def _guess_project_path(session_file: Path) -> str:
    parent_name = session_file.parent.name
    if parent_name.startswith("-"):
        candidate = "/" + parent_name[1:].replace("-", "/")
        if os.path.isdir(candidate):
            return candidate
    return parent_name


def _extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _extract_thinking(content) -> list[str]:
    results = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                thinking = block.get("thinking", "")
                if thinking:
                    results.append(thinking)
    return results


def _extract_tool_uses(content) -> list[dict]:
    results = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                if isinstance(inp, dict):
                    input_text = "\n".join(
                        f"  {k}: {_flatten(v)}" for k, v in inp.items()
                    )
                else:
                    input_text = str(inp)
                results.append({"name": name, "input": input_text, "id": block.get("id", "")})
    return results


def _extract_tool_results(content) -> list[dict]:
    results = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "?")
                rc = block.get("content", "")
                if isinstance(rc, list):
                    text = "\n".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in rc
                    )
                elif isinstance(rc, str):
                    text = rc
                else:
                    text = str(rc)
                results.append({
                    "tool_use_id": tool_id,
                    "text": text,
                    "is_error": block.get("is_error", False),
                })
    return results


def _flatten(v, max_len=500) -> str:
    if isinstance(v, str):
        s = v
    elif isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    return s[:max_len] + "..." if len(s) > max_len else s


def _strip_system_tags(text: str) -> str:
    for pat in _SYSTEM_TAG_RES:
        text = pat.sub("", text)
    return text.strip()


def _parse_user_command(text: str) -> str | None:
    match = _COMMAND_RE.search(text)
    if match:
        return match.group(1).strip()
    return None


def _extract_teammate_messages(text: str) -> list[dict]:
    results = []
    for match in _TEAMMATE_RE.finditer(text):
        results.append({"from": match.group(1), "text": match.group(2).strip()})
    return results


def _format_compact_text(record: dict) -> str:
    meta = record.get("compactMetadata", {}) or {}
    trigger = meta.get("trigger", "unknown")
    pre_tokens = meta.get("preTokens", "?")
    return f"[COMPACT] {trigger} | {pre_tokens} tokens -> compacted"


def _find_last_compact_line(input_path: Path) -> int | None:
    last_idx = None
    with open(input_path, "rb") as f:
        for idx, raw_line in enumerate(f):
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue
            if (record.get("type") == "system"
                    and record.get("subtype") == "compact_boundary"):
                last_idx = idx
    return last_idx


# -- Record processing ------------------------------------------------------


def _process_record(record: dict, opts: FilterOpts) -> list[ChatMessage]:
    """Process a single JSONL record into ChatMessage objects."""
    rec_type = record.get("type", "")
    ts = record.get("timestamp", "")
    session_id = record.get("sessionId", "")
    is_meta = record.get("isMeta", False) is True
    out: list[ChatMessage] = []

    if rec_type == "system" and record.get("subtype") == "compact_boundary":
        out.append(ChatMessage(
            role="system", text=_format_compact_text(record),
            timestamp=ts, session_id=session_id, msg_type="compact",
        ))
        return out

    if rec_type in ("user", "assistant"):
        msg = record.get("message", {})
        if not isinstance(msg, dict):
            return out
        role = msg.get("role", rec_type)
        content = msg.get("content", [])
        sender = "user" if role == "user" else "assistant"

        if opts.thinking and sender == "assistant":
            for thinking in _extract_thinking(content):
                out.append(ChatMessage(
                    role="assistant", text=thinking,
                    timestamp=ts, session_id=session_id, msg_type="thinking",
                ))

        text = _extract_text_from_content(content)

        if sender == "user":
            teammates = _extract_teammate_messages(text)
            if teammates:
                if opts.teammates:
                    for tm in teammates:
                        out.append(ChatMessage(
                            role=tm["from"], text=tm["text"],
                            timestamp=ts, session_id=session_id, msg_type="team_msg",
                            metadata={"teammate_id": tm["from"]},
                        ))
                text = _TEAMMATE_RE.sub("", text)

            if opts.parse_commands:
                cmd = _parse_user_command(text)
                if cmd is not None:
                    text = cmd

            if opts.strip_tags:
                text = _strip_system_tags(text)
                text = strip_extract_bookends(text)

        if text.strip():
            # Detect system-injected messages: isMeta flag or known system tags
            is_system_inject = False
            if sender == "user":
                if is_meta:
                    is_system_inject = True
                else:
                    raw_text = _extract_text_from_content(content)
                    if any(tag in raw_text for tag in _SYSTEM_INJECT_TAGS):
                        is_system_inject = True
            out.append(ChatMessage(
                role=sender, text=text.strip(),
                timestamp=ts, session_id=session_id,
                msg_type="system_inject" if is_system_inject else "msg",
            ))

        if opts.tools and sender == "assistant":
            for tu in _extract_tool_uses(content):
                out.append(ChatMessage(
                    role="assistant", text=f"{tu['name']}\n{tu['input']}",
                    timestamp=ts, session_id=session_id, msg_type="tool_use",
                    metadata={"tool": tu["name"]},
                ))

        if opts.tools and sender == "user":
            for tr in _extract_tool_results(content):
                prefix = "[ERROR] " if tr["is_error"] else ""
                out.append(ChatMessage(
                    role="system", text=f"{prefix}{tr['text']}",
                    timestamp=ts, session_id=session_id, msg_type="tool_result",
                    metadata={"tool_use_id": tr["tool_use_id"]},
                ))

    elif rec_type == "progress":
        data = record.get("data", {})
        data_type = data.get("type", "")

        if data_type == "hook_progress" and opts.hooks:
            event = data.get("hookEvent", "?")
            name = data.get("hookName", "?")
            cmd = data.get("command", "")
            out.append(ChatMessage(
                role="system", text=f"[{event}] {name}: {cmd}",
                timestamp=ts, session_id=session_id, msg_type="hook",
            ))

        elif data_type == "bash_progress" and opts.bash:
            output = data.get("output", "")
            out.append(ChatMessage(
                role="system", text=output,
                timestamp=ts, session_id=session_id, msg_type="bash",
                metadata={
                    "lines": data.get("totalLines", "?"),
                    "elapsed_s": data.get("elapsedTimeSeconds", "?"),
                },
            ))

    return out


# -- Provider class ---------------------------------------------------------


class ClaudeCodeProvider(Provider):
    provider_id = "cc"
    provider_label = "Claude Code"

    def discover_sessions(self, recovery: bool = False, all_sessions: bool = False, **kwargs) -> list[Path]:
        candidates = _all_session_jsonls()
        if not candidates:
            return []

        if all_sessions:
            return candidates

        if recovery:
            newest = candidates[0]
            if newest.stat().st_size < MIN_SESSION_SIZE and len(candidates) > 1:
                for f in candidates[1:]:
                    if f.stat().st_size >= MIN_SESSION_SIZE:
                        print(
                            f"Recovery: newest file is small ({newest.stat().st_size} bytes), "
                            f"using previous session: {f.name}",
                            file=sys.stderr,
                        )
                        return [f]
                return [newest]

        for f in candidates:
            if f.stat().st_size >= MIN_SESSION_SIZE:
                return [f]

        if candidates:
            print(
                f"Warning: no JSONL >= {MIN_SESSION_SIZE // 1024}KB, using newest "
                f"({candidates[0].name}, {candidates[0].stat().st_size} bytes)",
                file=sys.stderr,
            )
            return [candidates[0]]
        return []

    def parse_file(
        self,
        path: Path,
        opts: FilterOpts,
        session_id_filter: str | None = None,
        since_last_compact: bool = False,
    ) -> list[ChatSession]:
        skip_until_line = None
        if since_last_compact:
            last_compact = _find_last_compact_line(path)
            if last_compact is not None:
                skip_until_line = last_compact
                print(
                    f"Recovery: found compact_boundary at line {last_compact + 1}, "
                    f"extracting from there",
                    file=sys.stderr,
                )

        messages_by_session: OrderedDict[str, list[ChatMessage]] = OrderedDict()
        saw_compact = False  # track compact_boundary for continuation summary detection
        compact_summaries: dict[str, str] = {}  # sid -> summary text

        with open(path, "rb") as f:
            for idx, raw_line in enumerate(f):
                if skip_until_line is not None and idx < skip_until_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    continue
                if session_id_filter and record.get("sessionId") != session_id_filter:
                    continue

                rec_type = record.get("type", "")

                # Track compact boundaries
                if rec_type == "system" and record.get("subtype") == "compact_boundary":
                    saw_compact = True

                # Detect continuation summary: first user msg after compact
                if saw_compact and rec_type == "user":
                    msg_obj = record.get("message", {})
                    content = msg_obj.get("content", "") if isinstance(msg_obj, dict) else ""
                    text = _extract_text_from_content(content)
                    if text.startswith(_CONTINUATION_PREFIX):
                        sid = record.get("sessionId", "unknown")
                        compact_summaries[sid] = text
                    saw_compact = False

                msgs = _process_record(record, opts)
                for msg in msgs:
                    msg.source_file = str(path)
                    msg.source_line = idx + 1  # 1-based line number
                    sid = msg.session_id or "unknown"
                    if sid not in messages_by_session:
                        messages_by_session[sid] = []
                    messages_by_session[sid].append(msg)

        sessions = []
        project = _guess_project_path(path)
        for sid, msgs in messages_by_session.items():
            timestamps = [m.timestamp for m in msgs if m.timestamp]
            start = min(timestamps) if timestamps else ""
            end = max(timestamps) if timestamps else ""
            sessions.append(ChatSession(
                session_id=sid,
                title=f"Session {sid[:8]}",
                start_ts=start,
                end_ts=end,
                source_file=str(path),
                source_type=self.provider_id,
                messages=msgs,
                metadata={
                    "project": project,
                    **({"compact_summary": compact_summaries[sid]} if sid in compact_summaries else {}),
                },
            ))
        return sessions

    def list_sessions_metadata(self) -> list[dict]:
        candidates = _all_session_jsonls()
        rows = []
        for fpath in candidates:
            size = fpath.stat().st_size
            compact_count = 0
            user_count = 0
            assistant_count = 0
            first_ts = None
            last_ts = None
            preview_msgs: list[str] = []
            wl_summary = ""

            with open(fpath, "rb") as f:
                for raw_line in f:
                    try:
                        record = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                        continue
                    rtype = record.get("type", "")
                    ts = record.get("timestamp", "")
                    if ts:
                        if first_ts is None or ts < first_ts:
                            first_ts = ts
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
                    if rtype == "system" and record.get("subtype") == "compact_boundary":
                        compact_count += 1
                    elif rtype == "user":
                        user_count += 1
                        if len(preview_msgs) < 2:
                            if record.get("isMeta") or record.get("subtype") == "local-command":
                                continue
                            msg = record.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                parts = [
                                    b.get("text", "")
                                    for b in content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                ]
                                content = " ".join(parts)
                            if isinstance(content, str) and content.strip():
                                line = content.strip().replace("\n", " ")[:120]
                                if line.startswith(("<local-command", "<command-name")):
                                    continue
                                preview_msgs.append(line)
                    elif rtype == "assistant":
                        assistant_count += 1
                        if not wl_summary:
                            msg = record.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                parts = [
                                    b.get("text", "")
                                    for b in content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                ]
                                content = " ".join(parts)
                            if isinstance(content, str):
                                m = re.search(
                                    r"<wl-summary>(.*?)</wl-summary>",
                                    content,
                                    re.DOTALL,
                                )
                                if m:
                                    wl_summary = m.group(1).strip()[:80]

            rows.append({
                "session_id": fpath.stem,
                "file": str(fpath),
                "size": size,
                "start_ts": first_ts or "",
                "end_ts": last_ts or "",
                "compact_count": compact_count,
                "user_count": user_count,
                "assistant_count": assistant_count,
                "source_type": self.provider_id,
                "preview": preview_msgs,
                "wl_summary": wl_summary,
            })
        return rows

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                for raw_line in f:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        record = json.loads(raw_line)
                    except (json.JSONDecodeError, ValueError):
                        return False
                    return (
                        isinstance(record, dict)
                        and "type" in record
                        and "sessionId" in record
                        and "timestamp" in record
                    )
        except (OSError, IOError):
            return False
        return False
