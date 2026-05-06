"""Formatter output-shape stability tests."""
from __future__ import annotations

import io
import json

from wormlens.formatters import (
    format_chat,
    format_md,
    format_txt,
    write_jsonl,
)
from wormlens.models import ChatMessage, ChatSession


def _sample_sessions():
    msgs = [
        ChatMessage(role="user", text="hello", msg_type="msg",
                    timestamp="2026-05-01T00:00:00Z", source_line=1),
        ChatMessage(role="assistant", text="hi", msg_type="msg",
                    timestamp="2026-05-01T00:00:01Z", source_line=2),
    ]
    return [ChatSession(
        session_id="abc-123",
        title="Session abc-123",
        start_ts="2026-05-01T00:00:00Z",
        end_ts="2026-05-01T00:00:01Z",
        source_file="/tmp/x.jsonl",
        source_type="cc",
        messages=msgs,
    )]


def test_chat_format_has_turn_tags():
    out = format_chat(_sample_sessions(), frontmatter=True)
    assert out.startswith("<wormlens-extract")
    assert out.rstrip().endswith("</wormlens-extract>")
    assert "<user turn=" in out
    assert "<assistant turn=" in out
    # Frontmatter present
    assert "---\nexported:" in out


def test_chat_format_recall_caveat_tag():
    """Recall mode swaps wormlens-extract for wl-recall-caveat and adds preamble."""
    out = format_chat(_sample_sessions(), recall=True)
    assert out.startswith("<wl-recall-caveat>")
    assert out.rstrip().endswith("</wl-recall-caveat>")
    assert "extracted session history" in out


def test_chat_format_recall_with_frontmatter_off():
    """When CLI passes recall=True it also sets frontmatter=False."""
    out = format_chat(_sample_sessions(), frontmatter=False, recall=True)
    assert "---\nexported:" not in out


def test_md_format_yaml_frontmatter():
    out = format_md(_sample_sessions(), frontmatter=True)
    assert out.startswith("<wormlens-extract")
    # YAML frontmatter delimiters
    assert "---\nexported:" in out
    assert "user_turns:" in out
    assert "# Chat History Export" in out


def test_md_format_no_frontmatter_when_disabled():
    out = format_md(_sample_sessions(), frontmatter=False)
    assert "---\nexported:" not in out
    assert "**Exported:**" in out  # bolded fallback header


def test_txt_format_plain():
    out = format_txt(_sample_sessions())
    assert "[SESSION_ID]" in out
    assert "[User]" in out
    assert "[Assistant]" in out


def test_jsonl_format_one_record_per_line(tmp_path):
    p = tmp_path / "out.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        write_jsonl(_sample_sessions(), f, include_ts=True)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)  # must be valid JSON
        assert "type" in rec
        assert "from" in rec
        assert "text" in rec


def test_jsonl_format_no_ts_omits_timestamp(tmp_path):
    p = tmp_path / "out.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        write_jsonl(_sample_sessions(), f, include_ts=False)
    for line in p.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        assert "ts" not in rec


def test_chat_format_session_open_tag_attrs():
    out = format_chat(_sample_sessions(), frontmatter=False)
    # source and date attrs present
    assert '<session id="abc-123" source="cc" date="2026-05-01"' in out
