""".wl source provider round-trip: extract -> parse-as-wl -> re-extract."""
from __future__ import annotations

import pytest

from wormlens.formatters import format_chat
from wormlens.models import ChatMessage, ChatSession, FilterOpts
from wormlens.providers.wl_extract.parser import (
    WlExtractProvider,
    WlFormatError,
    _has_wl_marker,
)


def _seed_sessions():
    return [ChatSession(
        session_id="round-trip-1",
        title="Roundtrip",
        start_ts="2026-05-01T00:00:00Z",
        end_ts="2026-05-01T00:00:02Z",
        source_file="/tmp/orig.jsonl",
        source_type="cc",
        messages=[
            ChatMessage(role="user", text="alpha", msg_type="msg",
                        source_line=1),
            ChatMessage(role="assistant", text="beta", msg_type="msg",
                        source_line=2),
            ChatMessage(role="user", text="gamma", msg_type="msg",
                        source_line=3),
            ChatMessage(role="assistant", text="delta", msg_type="msg",
                        source_line=4),
        ],
    )]


def test_round_trip_chat_format(tmp_path):
    """Render to chat .wl, parse back, message bodies must survive."""
    out = format_chat(_seed_sessions(), frontmatter=True)
    p = tmp_path / "rt.wl"
    p.write_text(out, encoding="utf-8")

    # Detect must recognize it
    assert WlExtractProvider.detect(p) is True

    parsed = WlExtractProvider().parse_file(p, FilterOpts())
    assert len(parsed) == 1
    s = parsed[0]
    assert s.source_type == "wl"
    bodies = [m.text for m in s.messages]
    assert bodies == ["alpha", "beta", "gamma", "delta"]
    roles = [m.role for m in s.messages]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_parse_static_fixture(wl_chat_extract_path):
    parsed = WlExtractProvider().parse_file(wl_chat_extract_path, FilterOpts())
    assert len(parsed) == 1
    s = parsed[0]
    assert s.session_id == "abc-123"
    assert len(s.messages) == 4


def test_missing_wrapper_raises_format_error(tmp_path):
    p = tmp_path / "junk.wl"
    p.write_text("just some text without any tags\n", encoding="utf-8")
    with pytest.raises(WlFormatError):
        WlExtractProvider().parse_file(p, FilterOpts())


def test_marker_detection_recall_caveat(tmp_path):
    p = tmp_path / "rec.wl"
    p.write_text(
        "<wl-recall-caveat>\n<session id=\"x\" source=\"cc\" date=\"\">"
        "\n<user turn=1>hi\n</session>\n</wl-recall-caveat>\n",
        encoding="utf-8",
    )
    assert WlExtractProvider.detect(p) is True
    parsed = WlExtractProvider().parse_file(p, FilterOpts())
    assert len(parsed) == 1


def test_has_wl_marker_recognizes_frontmatter_with_source():
    text = '---\nsource: "cc"\nexported: "now"\n---\nbody\n'
    assert _has_wl_marker(text) is True


def test_has_wl_marker_rejects_plain_text():
    assert _has_wl_marker("just plain text") is False
