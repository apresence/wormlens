"""Output-side options: --max-message-bytes, --line-numbers, skinsuit detection.

These features cross formatter, pipeline, and provider-resolution code,
so regressions tend to break them silently (caller-supplied env or a
caller forgetting to wire the flag through write_output).
"""
from __future__ import annotations

import os
from unittest.mock import patch

from wormlens.formatters import format_chat, format_md, write_jsonl
from wormlens.models import ChatMessage, ChatSession, FilterOpts
from wormlens.pipeline import _detect_skinsuit, resolve_source


def _session_with(text, role="assistant"):
    return ChatSession(
        session_id="s1", title="t", start_ts="2026-05-01T00:00:00Z",
        end_ts="2026-05-01T00:00:01Z", source_file="/tmp/x", source_type="cc",
        messages=[
            ChatMessage(role="user", text="hi", msg_type="msg", source_line=1),
            ChatMessage(role=role, text=text, msg_type="msg", source_line=2),
        ],
    )


# ---- --max-message-bytes -------------------------------------------------


def test_chat_truncates_at_default_30k():
    big = "x" * 50000
    out = format_chat([_session_with(big)])
    assert "[truncated -- exceeded 30000 chars]" in out
    assert "x" * 30001 not in out  # truncation actually happened


def test_chat_truncates_at_custom_cap():
    out = format_chat([_session_with("y" * 100)], max_message_bytes=50)
    assert "[truncated -- exceeded 50 chars]" in out
    assert "y" * 51 not in out


def test_chat_unlimited_when_cap_zero():
    big = "z" * 50000
    out = format_chat([_session_with(big)], max_message_bytes=0)
    assert "truncated" not in out
    assert "z" * 50000 in out


def test_md_truncates_at_custom_cap():
    out = format_md([_session_with("q" * 200)], max_message_bytes=80)
    assert "[response truncated -- exceeded 80 chars]" in out


def test_jsonl_truncates_at_custom_cap(tmp_path):
    p = tmp_path / "out.jsonl"
    with open(p, "w") as f:
        write_jsonl([_session_with("a" * 500)], f, max_message_bytes=100)
    body = p.read_text()
    assert "truncated -- exceeded 100 chars" in body


# ---- --line-numbers ------------------------------------------------------


def test_chat_omits_line_attr_by_default():
    out = format_chat([_session_with("hello")])
    assert " line=" not in out


def test_chat_adds_line_attr_when_enabled():
    out = format_chat([_session_with("hello")], line_numbers=True)
    assert "line=1" in out
    assert "line=2" in out


def test_chat_skips_line_attr_when_source_line_zero():
    s = _session_with("hello")
    for m in s.messages:
        m.source_line = 0
    out = format_chat([s], line_numbers=True)
    assert "line=" not in out


def test_jsonl_includes_line_field_when_enabled(tmp_path):
    p = tmp_path / "out.jsonl"
    with open(p, "w") as f:
        write_jsonl([_session_with("hi")], f, include_line=True)
    body = p.read_text()
    assert '"line": 1' in body
    assert '"line": 2' in body


def test_jsonl_omits_line_field_by_default(tmp_path):
    p = tmp_path / "out.jsonl"
    with open(p, "w") as f:
        write_jsonl([_session_with("hi")], f)
    body = p.read_text()
    assert '"line":' not in body


# ---- skinsuit detection ------------------------------------------------


def test_detect_skinsuit_claudecode():
    with patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=False):
        # Ensure other markers aren't shadowing us
        for k in ("CODEX_HOME", "TERM_PROGRAM"):
            os.environ.pop(k, None)
        assert _detect_skinsuit() == "cc"


def test_detect_skinsuit_codex():
    with patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex"}, clear=False):
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("TERM_PROGRAM", None)
        assert _detect_skinsuit() == "codex"


def test_detect_skinsuit_vscode():
    with patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}, clear=False):
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("CODEX_HOME", None)
        assert _detect_skinsuit() == "vscode"


def test_detect_skinsuit_unknown_returns_none():
    with patch.dict(os.environ, {}, clear=True):
        assert _detect_skinsuit() is None


def test_resolve_source_falls_back_to_cc_when_no_signals():
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_source(None, None).provider_id == "cc"


def test_resolve_source_respects_skinsuit_for_codex():
    with patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex"}, clear=True):
        assert resolve_source(None, None).provider_id == "codex"


def test_resolve_source_explicit_wins_over_skinsuit():
    with patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex"}, clear=True):
        assert resolve_source("cc", None).provider_id == "cc"


# ---- turn-label preservation across slice (--rev / -n) -----------------


def _multi_turn_session(n_turns: int):
    msgs = []
    for i in range(1, n_turns + 1):
        msgs.append(ChatMessage(role="user", text=f"q{i}", msg_type="msg", source_line=i*2-1))
        msgs.append(ChatMessage(role="assistant", text=f"a{i}", msg_type="msg", source_line=i*2))
    return ChatSession(
        session_id="multi", title="m", start_ts="2026-05-01T00:00:00Z",
        end_ts="2026-05-01T00:00:01Z", source_file="/tmp/x",
        source_type="claude_ai",  # NOT cc/wl, so uses_line_index is False
        messages=msgs,
    )


def test_tail_preserves_original_turn_labels():
    """--rev -n 3 on a 27-turn claude_ai session must show turns 26/27/27, not 0/1/1."""
    from wormlens.pipeline import filter_and_sort
    sess = _multi_turn_session(27)
    sliced = filter_and_sort([sess], FilterOpts(), limit_n=3, reverse_limit=True)
    out = format_chat(sliced, frontmatter=False)
    assert "turn=26" in out
    assert "turn=27" in out
    assert "turn=0" not in out
    assert "turn=1>q1" not in out  # the head turn should not appear in tail


def test_head_preserves_original_turn_labels():
    """-n 3 on a 27-turn session shows turns 1/1/2."""
    from wormlens.pipeline import filter_and_sort
    sess = _multi_turn_session(27)
    sliced = filter_and_sort([sess], FilterOpts(), limit_n=3, reverse_limit=False)
    out = format_chat(sliced, frontmatter=False)
    assert "turn=1>q1" in out
    assert "turn=2>q2" in out


def test_no_slice_does_not_set_display_turn():
    """Without a slice, the legacy seq_turn path is exercised."""
    from wormlens.pipeline import filter_and_sort
    sess = _multi_turn_session(3)
    out_sessions = filter_and_sort([sess], FilterOpts())
    # filter_and_sort always stamps display_turn now -- this asserts it's stamped
    assert all(m.display_turn > 0 for m in out_sessions[0].messages)
    assert out_sessions[0].messages[0].display_turn == 1
    assert out_sessions[0].messages[-1].display_turn == 3
