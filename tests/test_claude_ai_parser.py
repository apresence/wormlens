"""Claude.ai web export parser correctness.

The export is a single JSON array of conversations dumped by the
"Export Data" feature on claude.ai. Tests use synthetic fixtures
written into tmp_path so they don't depend on a real export.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wormlens.models import FilterOpts
from wormlens.providers.claude_ai.parser import ClaudeAIProvider


# ---- fixture builders -----------------------------------------------------


def _text_block(text):
    return {
        "start_timestamp": "2026-05-16T00:00:00Z",
        "stop_timestamp": "2026-05-16T00:00:00Z",
        "type": "text",
        "text": text,
        "citations": [],
    }


def _thinking_block(text):
    return {
        "start_timestamp": "2026-05-16T00:00:00Z",
        "stop_timestamp": "2026-05-16T00:00:00Z",
        "type": "thinking",
        "thinking": text,
        "summaries": [],
    }


def _tool_use_block(name, inp, tool_id="t1"):
    return {
        "type": "tool_use",
        "name": name,
        "input": inp,
        "id": tool_id,
    }


def _tool_result_block(content, tool_use_id="t1"):
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }


def _msg(uuid, sender, blocks, ts="2026-05-16T00:00:00Z", attachments=None, files=None):
    return {
        "uuid": uuid,
        "text": "".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
        "content": blocks,
        "sender": sender,
        "created_at": ts,
        "updated_at": ts,
        "attachments": attachments or [],
        "files": files or [],
        "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
    }


def _conv(uuid, name, messages, summary=""):
    return {
        "uuid": uuid,
        "name": name,
        "account": {"uuid": "acct-1"},
        "created_at": "2026-05-16T00:00:00Z",
        "updated_at": "2026-05-16T00:00:01Z",
        "summary": summary,
        "chat_messages": messages,
    }


def _write_export(tmp_path, convs):
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(convs))
    return p


# ---- detect ---------------------------------------------------------------


def test_detect_recognizes_claude_ai_export(tmp_path):
    p = _write_export(tmp_path, [
        _conv("c1", "First", [_msg("m1", "human", [_text_block("hi")])]),
    ])
    assert ClaudeAIProvider.detect(p)


def test_detect_rejects_empty_file(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("")
    assert not ClaudeAIProvider.detect(p)


def test_detect_rejects_chatgpt_export_shape(tmp_path):
    """ChatGPT export uses `mapping` / `current_node`, no `chat_messages`."""
    p = tmp_path / "chatgpt.json"
    p.write_text(json.dumps([{
        "title": "x", "create_time": 1.0, "mapping": {}, "current_node": "n",
    }]))
    assert not ClaudeAIProvider.detect(p)


def test_detect_rejects_cc_jsonl(tmp_path):
    p = tmp_path / "cc.jsonl"
    p.write_text(json.dumps({"type": "user", "sessionId": "x"}) + "\n")
    assert not ClaudeAIProvider.detect(p)


def test_detect_rejects_object_not_array(tmp_path):
    p = tmp_path / "obj.json"
    p.write_text(json.dumps({
        "uuid": "x", "chat_messages": [], "account": {},
    }))
    assert not ClaudeAIProvider.detect(p)


# ---- parse: basic shapes --------------------------------------------------


def test_parse_simple_conv_default_opts(tmp_path):
    p = _write_export(tmp_path, [_conv("c1", "Greeting", [
        _msg("m1", "human", [_text_block("hello")]),
        _msg("m2", "assistant", [_text_block("hi there")]),
    ])])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "c1"
    assert s.title == "Greeting"
    assert s.source_type == "claude_ai"
    assert [m.role for m in s.messages] == ["user", "assistant"]
    assert [m.text for m in s.messages] == ["hello", "hi there"]


def test_parse_returns_one_session_per_conversation(tmp_path):
    p = _write_export(tmp_path, [
        _conv("c1", "A", [_msg("m1", "human", [_text_block("a")])]),
        _conv("c2", "B", [_msg("m2", "human", [_text_block("b")])]),
        _conv("c3", "C", [_msg("m3", "human", [_text_block("c")])]),
    ])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts())
    assert [s.session_id for s in sessions] == ["c1", "c2", "c3"]


def test_parse_session_id_filter_picks_one(tmp_path):
    p = _write_export(tmp_path, [
        _conv("c1", "A", [_msg("m1", "human", [_text_block("a")])]),
        _conv("c2", "B", [_msg("m2", "human", [_text_block("b")])]),
    ])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts(), session_id_filter="c2")
    assert len(sessions) == 1
    assert sessions[0].session_id == "c2"


def test_parse_skips_empty_messages_by_default(tmp_path):
    p = _write_export(tmp_path, [_conv("c1", "Empty", [
        _msg("m1", "human", [_text_block("")]),
        _msg("m2", "assistant", [_text_block("nonempty")]),
    ])])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts())
    assert len(sessions) == 1
    assert [m.text for m in sessions[0].messages] == ["nonempty"]


# ---- filter gating --------------------------------------------------------


def test_thinking_hidden_by_default(tmp_path):
    p = _write_export(tmp_path, [_conv("c1", "T", [
        _msg("m1", "human", [_text_block("ask")]),
        _msg("m2", "assistant", [
            _thinking_block("internal deliberation"),
            _text_block("answer"),
        ]),
    ])])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts(thinking=False))
    types = [m.msg_type for m in sessions[0].messages]
    assert "thinking" not in types
    assert types.count("msg") == 2


def test_thinking_surfaced_when_enabled(tmp_path):
    p = _write_export(tmp_path, [_conv("c1", "T", [
        _msg("m1", "human", [_text_block("ask")]),
        _msg("m2", "assistant", [
            _thinking_block("internal deliberation"),
            _text_block("answer"),
        ]),
    ])])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts(thinking=True))
    msgs = sessions[0].messages
    assert any(m.msg_type == "thinking" and "deliberation" in m.text for m in msgs)


def test_tool_use_hidden_by_default(tmp_path):
    p = _write_export(tmp_path, [_conv("c1", "Tools", [
        _msg("m1", "human", [_text_block("do it")]),
        _msg("m2", "assistant", [
            _text_block("running tool"),
            _tool_use_block("file_read", {"path": "/x"}),
        ]),
        _msg("m3", "human", [_tool_result_block("file contents")]),
    ])])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts(tools=False))
    types = [m.msg_type for m in sessions[0].messages]
    assert "tool_use" not in types
    assert "tool_result" not in types


def test_tool_use_surfaced_when_enabled(tmp_path):
    p = _write_export(tmp_path, [_conv("c1", "Tools", [
        _msg("m1", "human", [_text_block("do it")]),
        _msg("m2", "assistant", [
            _text_block("running tool"),
            _tool_use_block("file_read", {"path": "/x"}),
        ]),
        _msg("m3", "human", [_tool_result_block("file contents")]),
    ])])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts(tools=True))
    msgs = sessions[0].messages
    assert any(m.msg_type == "tool_use" and "file_read" in m.text for m in msgs)
    assert any(m.msg_type == "tool_result" and "file contents" in m.text for m in msgs)


def test_attachments_emitted_as_refs_when_enabled(tmp_path):
    p = _write_export(tmp_path, [_conv("c1", "Att", [
        _msg("m1", "human", [_text_block("see file")],
             attachments=[{"file_name": "design.pdf"}],
             files=[{"file_name": "diagram.png"}]),
    ])])
    sessions = ClaudeAIProvider().parse_file(p, FilterOpts(refs=True))
    ref_msgs = [m for m in sessions[0].messages if m.msg_type == "ref"]
    assert any("design.pdf" in m.text for m in ref_msgs)
    assert any("diagram.png" in m.text for m in ref_msgs)


# ---- list_sessions_metadata ----------------------------------------------


def test_list_sessions_metadata_requires_paths(tmp_path):
    """No auto-discovery: empty list without explicit paths."""
    assert ClaudeAIProvider().list_sessions_metadata() == []
    assert ClaudeAIProvider().list_sessions_metadata(paths=None) == []


def test_list_sessions_metadata_enumerates_each_conv(tmp_path):
    p = _write_export(tmp_path, [
        _conv("c1", "A", [_msg("m1", "human", [_text_block("hi")]),
                          _msg("m2", "assistant", [_text_block("yo")])]),
        _conv("c2", "B", [_msg("m3", "human", [_text_block("a")]),
                          _msg("m4", "assistant", [_text_block("b")])]),
    ])
    rows = ClaudeAIProvider().list_sessions_metadata(paths=[p])
    assert [r["session_id"] for r in rows] == ["c1", "c2"]
    assert all(r["source_type"] == "claude_ai" for r in rows)
    assert rows[0]["user_count"] == 1
    assert rows[0]["assistant_count"] == 1


# ---- discovery is off -----------------------------------------------------


def test_discover_sessions_returns_empty():
    assert ClaudeAIProvider().discover_sessions() == []


# ---- malformed input ------------------------------------------------------


def test_parse_nonexistent_file_returns_empty(tmp_path):
    sessions = ClaudeAIProvider().parse_file(tmp_path / "nope.json", FilterOpts())
    assert sessions == []


def test_parse_invalid_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    assert ClaudeAIProvider().parse_file(p, FilterOpts()) == []


def test_parse_object_not_array_returns_empty(tmp_path):
    p = tmp_path / "obj.json"
    p.write_text(json.dumps({"foo": "bar"}))
    assert ClaudeAIProvider().parse_file(p, FilterOpts()) == []


def test_parse_conv_without_uuid_dropped(tmp_path):
    p = tmp_path / "noid.json"
    p.write_text(json.dumps([
        {"name": "x", "chat_messages": [], "account": {}},
    ]))
    assert ClaudeAIProvider().parse_file(p, FilterOpts()) == []
