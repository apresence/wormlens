"""CC JSONL parser correctness -- including malformed-line tolerance.

A "line" in a CC JSONL log is not the same as a "logical message":
multi-line content blocks and partial records exist. The parser must
skip non-JSON lines and continue, never abort.
"""
from __future__ import annotations

import json

from wormlens.models import FilterOpts
from wormlens.providers.claude_code.parser import ClaudeCodeProvider


def test_parse_simple_session(cc_session_path):
    sessions = ClaudeCodeProvider().parse_file(cc_session_path, FilterOpts())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.source_type == "cc"
    # Default opts: no tools/thinking, only msg-type messages
    msgs = [m for m in s.messages if m.msg_type == "msg"]
    assert len(msgs) == 4
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0].text == "hello world"


def test_parse_with_thinking_flag(cc_session_path):
    opts = FilterOpts(thinking=True)
    sessions = ClaudeCodeProvider().parse_file(cc_session_path, opts)
    msgs = sessions[0].messages
    thinking = [m for m in msgs if m.msg_type == "thinking"]
    assert len(thinking) == 1
    assert "respond politely" in thinking[0].text


def test_parse_with_tools_flag(cc_session_path):
    opts = FilterOpts(tools=True)
    sessions = ClaudeCodeProvider().parse_file(cc_session_path, opts)
    tool_uses = [m for m in sessions[0].messages if m.msg_type == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0].metadata["tool"] == "Bash"


def test_parse_with_bash_flag_emits_bash_record(cc_session_path):
    """--bash without --tools should still surface Bash invocations."""
    opts = FilterOpts(bash=True)
    sessions = ClaudeCodeProvider().parse_file(cc_session_path, opts)
    bash_msgs = [m for m in sessions[0].messages if m.msg_type == "bash"]
    assert len(bash_msgs) == 1
    assert "ls -la" in bash_msgs[0].text


def test_parser_tolerates_partial_and_blank_lines(cc_session_partial_lines):
    """Per CC-JSONL-SPEC: non-JSON lines and blanks must not abort."""
    sessions = ClaudeCodeProvider().parse_file(cc_session_partial_lines, FilterOpts())
    assert len(sessions) == 1
    msgs = [m for m in sessions[0].messages if m.msg_type == "msg"]
    # Two valid records survive past the corruption.
    assert len(msgs) == 2
    assert msgs[0].text == "hello"
    assert msgs[1].text == "hi"


def test_source_line_is_one_based(cc_session_path):
    """source_line must be 1-based (matches `wl --index N`)."""
    sessions = ClaudeCodeProvider().parse_file(cc_session_path, FilterOpts())
    msgs = sessions[0].messages
    # First user record is on line 1
    assert msgs[0].source_line == 1


def test_session_id_filter(cc_session_path, tmp_path):
    """When sessionId filter is set, only matching records are kept."""
    sessions = ClaudeCodeProvider().parse_file(
        cc_session_path, FilterOpts(),
        session_id_filter="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    assert len(sessions) == 1
    sessions = ClaudeCodeProvider().parse_file(
        cc_session_path, FilterOpts(),
        session_id_filter="not-this-one",
    )
    assert sessions == []


def test_detect_cc_format(cc_session_path):
    assert ClaudeCodeProvider.detect(cc_session_path) is True


def test_detect_cc_rejects_garbage(tmp_path):
    p = tmp_path / "junk.jsonl"
    p.write_text("not json at all\n", encoding="utf-8")
    assert ClaudeCodeProvider.detect(p) is False


def test_multiline_text_block_preserved(tmp_path):
    """Content blocks may include newlines inside a single JSONL record."""
    sid = "55555555-5555-5555-5555-555555555555"
    rec = {
        "type": "user",
        "sessionId": sid,
        "timestamp": "2026-05-01T00:00:00.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "line one\nline two\nline three"}],
        },
    }
    p = tmp_path / "multiline.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    sessions = ClaudeCodeProvider().parse_file(p, FilterOpts())
    assert "\n" in sessions[0].messages[0].text
    assert sessions[0].messages[0].text.count("\n") == 2
