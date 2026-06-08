"""Wormlens-extract chat provider backend.

Reads .wl / .md files produced by wormlens itself: text wrapped in
<wormlens-extract ...> or <wl-recall-caveat> tags, optionally with a
YAML frontmatter block, and a body in either the compact "chat" format
(<session> with <user turn=N>/<assistant turn=N> lines) or the
markdown format (## Session ... with **User:**/**Assistant:** blocks).

This lets agents chain recall: feed prior extracts back into wl to
filter/regrep/re-export a longer-lived memory trail.
"""

from __future__ import annotations

import re
from pathlib import Path

from .._base import Provider, session_id_matches, strip_extract_bookends
from ...models import ChatMessage, ChatSession, FilterOpts


_OUTER_OPEN_RE = re.compile(
    r'<(?:wormlens-extract(?:\s[^>]*)?|wl-recall-caveat)>\s*\n?',
)
_OUTER_CLOSE_RE = re.compile(
    r'\n?</(?:wormlens-extract|wl-recall-caveat)>\s*\Z',
)

_FRONTMATTER_RE = re.compile(
    r'\A---\s*\n(.*?)\n---\s*\n',
    re.DOTALL,
)

# Anchor to line start so that content lines mentioning <session ...> do
# not split a real session boundary. Format emitter writes session tags
# at column 0; non-tag content beginning with `<` is escaped to `\<`.
_SESSION_OPEN_RE = re.compile(
    r'^<session\s+([^>]*)>',
    re.MULTILINE,
)

_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

# Match an opening turn tag. Tag name may be:
#   - "user" or "assistant" (msg_type=msg)
#   - "<role>/<subtype>" e.g. "assistant/team_msg", "system/compact"
#   - One of the special-render tag names emitted by formatters._SPECIAL_RENDER:
#     thinking, tool_use, tool_result, bash_output, hook, system_inject,
#     code_edit, ref. These map back to (role, msg_type) via _SPECIAL_TAG_MAP.
# Attributes (if any) appear before turn=N. We capture name="..." for tool_use
# round-trip via metadata.
_TURN_LINE_RE = re.compile(
    r'^<([A-Za-z][\w/_-]*)\s+([^>]*\bturn=(\d+)[^>]*)>(.*)$',
)

_NAME_ATTR_RE = re.compile(r'\bname="([^"]*)"')

# Special tag name -> (role, msg_type). These are tags emitted by
# formatters._SPECIAL_RENDER for non-msg ChatMessages, where the tag name
# alone (without a role/ prefix) carries the msg_type. We pin role to whatever
# the source providers use when creating these messages (see
# providers/claude_code/parser.py _process_record) so a re-render is byte-
# identical.
_SPECIAL_TAG_MAP = {
    "thinking":      ("assistant", "thinking"),
    "tool_use":      ("assistant", "tool_use"),
    "tool_result":   ("system",    "tool_result"),
    "bash_output":   ("system",    "bash"),
    "hook":          ("system",    "hook"),
    "system_inject": ("user",      "system_inject"),
    "code_edit":     ("system",    "code_edit"),
    "ref":           ("system",    "ref"),
}

_MD_SESSION_HEADER_RE = re.compile(r'^##\s+(.+?)\s*$', re.MULTILINE)
_MD_TURN_HEADER_RE = re.compile(r'^###\s+Turn\s+(\d+)\s*$')

# Comment line emitted by formatters.format_chat right after a <session>
# open. Captures the original session source_file path so we can
# preserve it across round-trips via metadata["original_source_file"].
_TURN_COMMENT_RE = re.compile(
    r'^<!--\s*turn\s*=\s*(?:JSONL\s+line\s+number|sequential)\.\s*(.*?)\s*-->\s*$',
)


def _parse_attrs(s: str) -> dict:
    return {m.group(1): m.group(2) for m in _ATTR_RE.finditer(s)}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract simple key:value pairs from a leading --- block.

    Stdlib only -- this is a deliberately minimal YAML reader. Handles
    `key: "value"`, `key: value`, and `key: |` block scalars (treated
    as a multi-line string). Nested mappings/lists are not supported;
    they are skipped.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    body_start = m.end()
    fm_text = m.group(1)
    out: dict = {}

    lines = fm_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        kv = re.match(r'^(\w[\w_-]*)\s*:\s*(.*)$', line)
        if not kv:
            i += 1
            continue
        key = kv.group(1)
        val = kv.group(2).strip()

        if val == "|" or val == ">":
            block_lines = []
            i += 1
            while i < len(lines):
                ln = lines[i]
                if ln.startswith("  "):
                    block_lines.append(ln[2:])
                    i += 1
                    continue
                if not ln.strip():
                    block_lines.append("")
                    i += 1
                    continue
                break
            out[key] = "\n".join(block_lines).rstrip()
            continue

        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        out[key] = val
        i += 1

    return out, text[body_start:]


def _strip_outer(text: str) -> str:
    text = _OUTER_OPEN_RE.sub("", text, count=1)
    text = _OUTER_CLOSE_RE.sub("", text, count=1)
    return text


def _unescape_chat_line(line: str) -> str:
    if line.startswith("\\\\"):
        return "\\" + line[1:]
    if line.startswith("\\<"):
        return line[1:]
    return line


def _unescape_chat_content(text: str) -> str:
    return "\n".join(_unescape_chat_line(l) for l in text.split("\n"))


_SESSION_CLOSE_RE = re.compile(r'^</session>', re.MULTILINE)


def _split_sessions_chat(body: str) -> list[tuple[dict, str]]:
    """Find each <session ...>...</session> block. Returns (attrs, inner).

    Both the open and close are matched at line start so that content
    lines mentioning the literal `<session ...>` or `</session>` (e.g.
    a tool_result that displays wormlens source code) do not falsely
    split a session block.
    """
    results = []
    for m in _SESSION_OPEN_RE.finditer(body):
        attrs = _parse_attrs(m.group(1))
        close = _SESSION_CLOSE_RE.search(body, m.end())
        if close is None:
            inner = body[m.end():]
        else:
            inner = body[m.end():close.start()]
        results.append((attrs, inner))
    return results


def _parse_chat_session(
    attrs: dict,
    inner: str,
    source_file: str,
    fallback_meta: dict,
) -> ChatSession | None:
    sid = attrs.get("id") or fallback_meta.get("session_id") or ""
    src = attrs.get("source") or fallback_meta.get("source") or "wl"
    date = attrs.get("date") or ""
    title = attrs.get("title") or (f"Session {sid[:8]}" if sid else "Session")

    messages: list[ChatMessage] = []
    original_source_file = ""

    lines = inner.split("\n")
    cur_role: str | None = None
    cur_msg_type = "msg"
    cur_turn = 0
    cur_metadata: dict = {}
    cur_lines: list[str] = []

    def flush():
        if cur_role is None:
            return
        # Strip only leading/trailing newlines (the tag boundaries) --
        # do not whitespace-strip the whole text or we lose trailing
        # tabs/spaces on content lines (e.g. tool_result records that
        # include line-numbered file dumps with trailing whitespace).
        text = "\n".join(cur_lines).strip("\n")
        text = _unescape_chat_content(text)
        if not text.strip():
            return
        messages.append(ChatMessage(
            role=cur_role,
            text=text,
            timestamp=date,
            session_id=sid,
            source_file=source_file,
            source_line=cur_turn,
            msg_type=cur_msg_type,
            metadata=dict(cur_metadata),
        ))

    for raw in lines:
        m = _TURN_LINE_RE.match(raw)
        if m:
            flush()
            tag_name = m.group(1)
            attrs_str = m.group(2) or ""
            try:
                cur_turn = int(m.group(3))
            except ValueError:
                cur_turn = 0
            cur_lines = [m.group(4)]
            cur_metadata = {}

            if "/" in tag_name:
                # role/subtype form (e.g. assistant/team_msg, system/compact)
                role_part, _, subtype = tag_name.partition("/")
                cur_role = role_part
                cur_msg_type = subtype or "msg"
                if cur_msg_type == "team_msg":
                    cur_metadata["teammate_id"] = role_part
            elif tag_name in _SPECIAL_TAG_MAP:
                cur_role, cur_msg_type = _SPECIAL_TAG_MAP[tag_name]
                if tag_name == "tool_use":
                    nm = _NAME_ATTR_RE.search(attrs_str)
                    if nm:
                        cur_metadata["tool"] = nm.group(1)
            elif tag_name in ("user", "assistant"):
                cur_role = tag_name
                cur_msg_type = "msg"
            else:
                # Unknown tag -- treat as a generic role/msg pair so we
                # don't drop the content silently.
                cur_role = tag_name
                cur_msg_type = "msg"
            continue
        if raw.startswith("<!--") and raw.rstrip().endswith("-->") and cur_role is None:
            cm = _TURN_COMMENT_RE.match(raw.strip())
            if cm:
                original_source_file = cm.group(1)
            continue
        if cur_role is None:
            continue
        cur_lines.append(raw)

    flush()

    if not messages:
        return None

    sess_meta: dict = {"original_source": src}
    if original_source_file:
        sess_meta["original_source_file"] = original_source_file
    return ChatSession(
        session_id=sid or "wl-extract",
        title=title,
        start_ts=date,
        end_ts=date,
        source_file=source_file,
        source_type="wl",
        messages=messages,
        metadata=sess_meta,
    )


_MD_LABEL_RE = re.compile(r'^\*\*(User|Assistant|[\w/-]+):\*\*\s*$')


def _parse_md_sessions(
    body: str,
    source_file: str,
    fallback_meta: dict,
) -> list[ChatSession]:
    """Parse the wl markdown body into sessions.

    Splits on `## ` headings, and within each section reads
    `**User:**` / `**Assistant:**` blocks separated by `---` rules.
    Best-effort: malformed sections produce no messages rather than
    raising.
    """
    sessions: list[ChatSession] = []

    headers = list(_MD_SESSION_HEADER_RE.finditer(body))
    if not headers:
        return []

    for i, hm in enumerate(headers):
        title = hm.group(1).strip()
        start = hm.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        section = body[start:end]

        sid = ""
        sid_match = re.search(r'\*\*Session ID:\*\*\s*(\S+)', section)
        if sid_match:
            sid = sid_match.group(1)
        if not sid:
            sid = fallback_meta.get("session_id", "")

        date = ""
        d_match = re.search(r'\*\*Start Date:\*\*\s*(.+)', section)
        if d_match:
            date = d_match.group(1).strip()

        messages: list[ChatMessage] = []

        # Walk line by line, switching role on **User:** / **Assistant:**
        # boundaries; collect lines until the next role or `---`.
        cur_role = None
        cur_lines: list[str] = []
        cur_turn = 0

        def flush():
            if cur_role is None:
                return
            text = "\n".join(cur_lines).strip()
            if not text:
                return
            messages.append(ChatMessage(
                role=cur_role,
                text=text,
                timestamp=date,
                session_id=sid,
                source_file=source_file,
                source_line=cur_turn,
                msg_type="msg",
            ))

        for raw in section.split("\n"):
            stripped = raw.strip()
            t_hdr = _MD_TURN_HEADER_RE.match(stripped)
            if t_hdr:
                flush()
                cur_role = None
                cur_lines = []
                try:
                    cur_turn = int(t_hdr.group(1))
                except ValueError:
                    cur_turn = 0
                continue
            label = _MD_LABEL_RE.match(stripped)
            if label:
                flush()
                role_label = label.group(1)
                if role_label == "User":
                    cur_role = "user"
                elif role_label == "Assistant":
                    cur_role = "assistant"
                else:
                    cur_role = role_label
                cur_lines = []
                continue
            if stripped == "---":
                flush()
                cur_role = None
                cur_lines = []
                continue
            if cur_role is not None:
                cur_lines.append(raw)

        flush()

        if not messages:
            continue

        sessions.append(ChatSession(
            session_id=sid or f"wl-md-{i}",
            title=title,
            start_ts=date,
            end_ts=date,
            source_file=source_file,
            source_type="wl",
            messages=messages,
            metadata={"original_source": fallback_meta.get("source", "wl")},
        ))

    return sessions


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _has_wl_marker(text: str) -> bool:
    head = text.lstrip()[:4096]
    if "<wormlens-extract" in head or "<wl-recall-caveat" in head:
        return True
    if head.startswith("---"):
        m = _FRONTMATTER_RE.match(head)
        if m and re.search(r'^source\s*:', m.group(1), re.MULTILINE):
            return True
    return False


class WlFormatError(Exception):
    """Raised when input passed to the wl provider lacks a wl wrapper.

    cli.py catches this and converts to a friendly stderr message + exit 1.
    Without this, the parser silently returns no sessions and the rest of
    the pipeline emits a phantom empty <wormlens-extract> wrapper.
    """


class WlExtractProvider(Provider):
    provider_id = "wl"
    provider_label = "Wormlens Extract"

    def discover_sessions(self, **kwargs) -> list[Path]:
        # No standard on-disk location; this provider is file-driven.
        return []

    def parse_file(
        self,
        path: Path,
        opts: FilterOpts,
        session_id_filter: str | None = None,
        since_last_compact: bool = False,
    ) -> list[ChatSession]:
        text = _read_text(path)
        if not _has_wl_marker(text):
            raise WlFormatError(
                f"{path} does not contain a wormlens extract wrapper "
                f"(no <wormlens-extract> or <wl-recall-caveat> found). "
                f"Did you mean --source cc?"
            )
        text = _strip_outer(text).strip()
        # Note: do NOT call strip_extract_bookends() here. _strip_outer
        # already removed the outer wrapper; calling the broader bookend
        # stripper would clobber any literal `<wormlens-extract>` /
        # `</wormlens-extract>` mentions inside content (e.g. README
        # excerpts in tool_result records), breaking round-trip fidelity.

        meta, body = _parse_frontmatter(text)

        chat_sessions_raw = _split_sessions_chat(body)
        sessions: list[ChatSession] = []

        if chat_sessions_raw:
            for attrs, inner in chat_sessions_raw:
                s = _parse_chat_session(attrs, inner, str(path), meta)
                if s is None:
                    continue
                if session_id_filter and not session_id_matches(s.session_id, session_id_filter):
                    continue
                sessions.append(s)
        else:
            md_sessions = _parse_md_sessions(body, str(path), meta)
            for s in md_sessions:
                if session_id_filter and not session_id_matches(s.session_id, session_id_filter):
                    continue
                sessions.append(s)

        return sessions

    def list_sessions_metadata(self, **kwargs) -> list[dict]:
        # Nothing to discover; users point at files explicitly.
        return []

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except (OSError, IOError):
            return False
        return _has_wl_marker(head)
