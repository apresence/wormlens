"""Codex CLI rollout parser correctness.

Drives real fixtures captured from `codex exec` runs (Codex CLI v0.128.0)
plus malformed-input edge cases. Filter-flag gating must match the
contract the other providers follow.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wormlens.models import FilterOpts
from wormlens.providers.codex.parser import CodexProvider


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "codex"


def _fixture(slug: str) -> Path:
    return FIXTURE_ROOT / slug / "rollout.jsonl"


# ---- detect ---------------------------------------------------------------


def test_detect_codex_rollout():
    assert CodexProvider.detect(_fixture("01_pure_conversation"))


def test_detect_rejects_cc_jsonl(tmp_path):
    """A CC-style record (type=user) must not match codex detect."""
    p = tmp_path / "cc.jsonl"
    p.write_text(json.dumps({"type": "user", "sessionId": "x", "message": {}}) + "\n")
    assert not CodexProvider.detect(p)


def test_detect_rejects_session_meta_without_id(tmp_path):
    p = tmp_path / "missing.jsonl"
    p.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"cli_version": "0.128.0"},
    }) + "\n")
    assert not CodexProvider.detect(p)


def test_detect_rejects_session_meta_without_cli_version(tmp_path):
    p = tmp_path / "missing.jsonl"
    p.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "abc"},
    }) + "\n")
    assert not CodexProvider.detect(p)


def test_detect_rejects_empty_file(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert not CodexProvider.detect(p)


def test_detect_rejects_nonjson_first_line(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("this is not json\n")
    assert not CodexProvider.detect(p)


def test_detect_rejects_non_dict_record(tmp_path):
    """A bare list or string at the top must not match."""
    p = tmp_path / "list.jsonl"
    p.write_text("[1, 2, 3]\n")
    assert not CodexProvider.detect(p)


# ---- parse: pure conversation --------------------------------------------


def test_parse_simple_session_default_opts():
    sessions = CodexProvider().parse_file(_fixture("01_pure_conversation"), FilterOpts())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.source_type == "codex"
    assert s.session_id == "019e0248-07af-7853-90cd-8737656bb137"
    assert s.metadata["cli_version"] == "0.128.0"
    assert s.metadata["originator"] == "codex_exec"
    msgs = [m for m in s.messages if m.msg_type == "msg"]
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert "episodic memory" in msgs[0].text.lower()


def test_developer_messages_filtered_by_default():
    sessions = CodexProvider().parse_file(_fixture("01_pure_conversation"), FilterOpts())
    roles = {m.role for m in sessions[0].messages}
    assert "developer" not in roles


def test_environment_context_user_messages_filtered_by_default():
    """The synthetic role=user `<environment_context>` injection must not leak."""
    sessions = CodexProvider().parse_file(_fixture("01_pure_conversation"), FilterOpts())
    msgs = sessions[0].messages
    user_msgs = [m for m in msgs if m.role == "user"]
    assert len(user_msgs) == 1
    assert "<environment_context>" not in user_msgs[0].text


def test_system_msgs_flag_surfaces_developer_and_env_inject():
    opts = FilterOpts(system_msgs=True)
    sessions = CodexProvider().parse_file(_fixture("01_pure_conversation"), opts)
    sys_msgs = [m for m in sessions[0].messages if m.msg_type == "system_inject"]
    assert len(sys_msgs) == 2
    assert any(m.role == "developer" for m in sys_msgs)
    assert any(m.role == "user" and "<environment_context>" in m.text for m in sys_msgs)


# ---- parse: tools ---------------------------------------------------------


def test_tools_flag_emits_tool_use_and_result():
    opts = FilterOpts(tools=True)
    sessions = CodexProvider().parse_file(_fixture("02_function_call"), opts)
    msgs = sessions[0].messages
    tool_uses = [m for m in msgs if m.msg_type == "tool_use"]
    tool_results = [m for m in msgs if m.msg_type == "tool_result"]
    assert len(tool_uses) == 1
    assert len(tool_results) == 1
    assert tool_uses[0].metadata.get("call_id")
    assert tool_uses[0].metadata["call_id"] == tool_results[0].metadata["call_id"]
    assert "exec_command" in tool_uses[0].text
    assert "echo hello world" in tool_uses[0].text


def test_tools_flag_off_drops_tool_records():
    sessions = CodexProvider().parse_file(_fixture("02_function_call"), FilterOpts())
    msgs = sessions[0].messages
    assert not [m for m in msgs if m.msg_type in ("tool_use", "tool_result")]


# ---- parse: reasoning -----------------------------------------------------


def test_thinking_flag_surfaces_reasoning():
    opts = FilterOpts(thinking=True)
    sessions = CodexProvider().parse_file(_fixture("04_reasoning"), opts)
    thinking = [m for m in sessions[0].messages if m.msg_type == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].text.strip()  # non-empty
    assert thinking[0].role == "assistant"


def test_thinking_flag_off_drops_reasoning_records():
    sessions = CodexProvider().parse_file(_fixture("04_reasoning"), FilterOpts())
    assert not [m for m in sessions[0].messages if m.msg_type == "thinking"]


# ---- parse: compacted + recall mode --------------------------------------


def test_compact_markers_flag_surfaces_compact_record():
    opts = FilterOpts(compact_markers=True)
    sessions = CodexProvider().parse_file(_fixture("05_compacted"), opts)
    compacts = [m for m in sessions[0].messages if m.msg_type == "compact"]
    assert len(compacts) == 1
    assert "compact summary" in compacts[0].text


def test_recall_mode_slices_after_last_compacted():
    """since_last_compact=True must drop everything before the last compacted record."""
    sessions = CodexProvider().parse_file(
        _fixture("05_compacted"), FilterOpts(), since_last_compact=True
    )
    msgs = [m for m in sessions[0].messages if m.msg_type == "msg"]
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].text.startswith("What number")
    assert msgs[1].text == "42"


def test_recall_mode_without_compact_returns_full_session():
    sessions = CodexProvider().parse_file(
        _fixture("01_pure_conversation"), FilterOpts(), since_last_compact=True
    )
    msgs = [m for m in sessions[0].messages if m.msg_type == "msg"]
    assert len(msgs) == 2  # full session preserved


# ---- parse: resume (multi-turn in one file) ------------------------------


def test_resume_session_yields_all_turns():
    """codex exec resume --last appends to the same rollout; parser must see both turns."""
    sessions = CodexProvider().parse_file(_fixture("06_resume"), FilterOpts())
    msgs = [m for m in sessions[0].messages if m.msg_type == "msg"]
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert "42" in msgs[1].text
    assert msgs[3].text.strip() == "42"


# ---- parse: malformed / edge cases ---------------------------------------


def test_parse_tolerates_partial_and_blank_lines(tmp_path):
    p = tmp_path / "partial.jsonl"
    valid_meta = json.dumps({
        "type": "session_meta",
        "payload": {"id": "abc-123", "cli_version": "0.128.0", "timestamp": "2026-05-07T00:00:00Z"},
    })
    valid_user = json.dumps({
        "type": "response_item",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}]},
    })
    valid_asst = json.dumps({
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}]},
    })
    # Mix valid + blank + non-JSON + truncated lines
    p.write_text(
        valid_meta + "\n"
        "\n"
        "this is not JSON\n"
        + valid_user + "\n"
        + '{"type": "response_item", "payload": {"type": "messa'   # truncated, no newline
        + "\n"
        + valid_asst + "\n"
    )
    sessions = CodexProvider().parse_file(p, FilterOpts())
    msgs = [m for m in sessions[0].messages if m.msg_type == "msg"]
    assert [m.text for m in msgs] == ["hello", "hi"]


def test_parse_tolerates_non_dict_record(tmp_path):
    """Per ADR 0010 / R3 fix shape: non-dict records skipped, parsing continues."""
    p = tmp_path / "weird.jsonl"
    valid_meta = json.dumps({
        "type": "session_meta",
        "payload": {"id": "abc", "cli_version": "0.128.0"},
    })
    valid_user = json.dumps({
        "type": "response_item",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "ok"}]},
    })
    p.write_text(valid_meta + "\n[1,2,3]\n\"a string\"\n42\n" + valid_user + "\n")
    sessions = CodexProvider().parse_file(p, FilterOpts())
    assert len(sessions) == 1
    msgs = [m for m in sessions[0].messages if m.msg_type == "msg"]
    assert len(msgs) == 1
    assert msgs[0].text == "ok"


def test_parse_returns_empty_on_missing_file(tmp_path):
    p = tmp_path / "nonexistent.jsonl"
    sessions = CodexProvider().parse_file(p, FilterOpts())
    assert sessions == []


def test_parse_returns_empty_on_blank_file(tmp_path):
    p = tmp_path / "blank.jsonl"
    p.write_text("")
    sessions = CodexProvider().parse_file(p, FilterOpts())
    assert sessions == []


# ---- session id filter ---------------------------------------------------


def test_session_id_filter_match_returns_session():
    sessions = CodexProvider().parse_file(
        _fixture("01_pure_conversation"),
        FilterOpts(),
        session_id_filter="019e0248-07af-7853-90cd-8737656bb137",
    )
    assert len(sessions) == 1


def test_session_id_filter_miss_returns_empty():
    sessions = CodexProvider().parse_file(
        _fixture("01_pure_conversation"),
        FilterOpts(),
        session_id_filter="00000000-0000-0000-0000-000000000000",
    )
    assert sessions == []


# ---- multi-content message rendering -------------------------------------


def test_multi_content_message_concatenates_text(tmp_path):
    p = tmp_path / "multi.jsonl"
    meta = json.dumps({"type": "session_meta",
                       "payload": {"id": "x", "cli_version": "0.128.0"}})
    user = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message", "role": "user",
            "content": [
                {"type": "input_text", "text": "first part. "},
                {"type": "input_text", "text": "second part."},
            ],
        },
    })
    p.write_text(meta + "\n" + user + "\n")
    sessions = CodexProvider().parse_file(p, FilterOpts())
    msgs = [m for m in sessions[0].messages if m.msg_type == "msg"]
    assert msgs[0].text == "first part. second part."


def test_image_content_renders_placeholder(tmp_path):
    p = tmp_path / "img.jsonl"
    meta = json.dumps({"type": "session_meta",
                       "payload": {"id": "x", "cli_version": "0.128.0"}})
    user = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message", "role": "user",
            "content": [
                {"type": "input_text", "text": "Look at "},
                {"type": "input_image", "image_url": "https://x/a.png"},
            ],
        },
    })
    p.write_text(meta + "\n" + user + "\n")
    sessions = CodexProvider().parse_file(p, FilterOpts())
    msgs = [m for m in sessions[0].messages if m.msg_type == "msg"]
    assert msgs[0].text == "Look at [image: https://x/a.png]"


# ---- discovery via $CODEX_HOME -------------------------------------------


def test_discover_sessions_reads_codex_home(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions" / "2026" / "05" / "07"
    sessions_dir.mkdir(parents=True)
    f1 = sessions_dir / "rollout-2026-05-07T00-00-00-aaa.jsonl"
    f1.write_text("{}\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    found = CodexProvider().discover_sessions()
    assert f1 in found


def test_discover_sessions_returns_empty_when_no_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # No sessions/ dir created
    found = CodexProvider().discover_sessions()
    assert found == []


# ---- registration --------------------------------------------------------


def test_provider_is_auto_registered():
    from wormlens.providers import PROVIDERS
    assert "codex" in PROVIDERS
    assert PROVIDERS["codex"] is CodexProvider


def test_auto_detect_picks_codex_for_rollout():
    from wormlens.providers import detect_provider
    cls = detect_provider(_fixture("02_function_call"))
    assert cls is CodexProvider


# ---- plan updates --------------------------------------------------------


def test_plan_update_emerges_as_function_call_tool_use():
    """update_plan tool surfaces as function_call with name=update_plan."""
    opts = FilterOpts(tools=True)
    sessions = CodexProvider().parse_file(_fixture("08_plan_update"), opts)
    plan_calls = [
        m for m in sessions[0].messages
        if m.msg_type == "tool_use" and m.metadata.get("name") == "update_plan"
    ]
    assert len(plan_calls) >= 3  # multiple plan updates as steps complete
    # Plan args are structured JSON; should contain step + status fields.
    assert "in_progress" in plan_calls[0].text or "completed" in plan_calls[0].text


# ---- web search ----------------------------------------------------------


def test_web_search_call_emitted_as_tool_use():
    opts = FilterOpts(tools=True)
    sessions = CodexProvider().parse_file(_fixture("09_web_search"), opts)
    web = [m for m in sessions[0].messages if m.metadata.get("name") == "web_search"]
    assert web  # at least one web_search_call
    assert "[web_search]" in web[0].text
    assert web[0].metadata.get("status") == "completed"


def test_web_search_off_when_tools_disabled():
    sessions = CodexProvider().parse_file(_fixture("09_web_search"), FilterOpts())
    msgs = sessions[0].messages
    assert not any("web_search" in (m.metadata or {}).get("name", "") for m in msgs)


# ---- MCP -----------------------------------------------------------------


def test_mcp_tool_call_carries_namespace_metadata():
    """MCP tools surface as function_call with a `namespace` field set."""
    opts = FilterOpts(tools=True)
    sessions = CodexProvider().parse_file(_fixture("10_mcp"), opts)
    mcp_calls = [
        m for m in sessions[0].messages
        if m.msg_type == "tool_use" and m.metadata.get("namespace", "").startswith("mcp__")
    ]
    assert len(mcp_calls) == 1
    assert mcp_calls[0].metadata["namespace"] == "mcp__demo__"
    assert mcp_calls[0].metadata["name"] == "get_sum"
    # Qualified name should appear in rendered text
    assert "mcp__demo__get_sum" in mcp_calls[0].text


# ---- list_sessions_metadata ----------------------------------------------


def test_list_sessions_metadata_extracts_preview(tmp_path, monkeypatch):
    """Copy a fixture into a synthetic CODEX_HOME and confirm metadata fields."""
    import shutil
    sessions_dir = tmp_path / "sessions" / "2026" / "05" / "07"
    sessions_dir.mkdir(parents=True)
    shutil.copy(_fixture("01_pure_conversation"), sessions_dir / "rollout-test.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    rows = CodexProvider().list_sessions_metadata()
    assert len(rows) == 1
    row = rows[0]
    assert row["source_type"] == "codex"
    assert row["session_id"] == "019e0248-07af-7853-90cd-8737656bb137"
    assert row["cli_version"] == "0.128.0"
    assert row["turn_count"] == 1  # one real user turn (env_context filtered)
    assert "episodic memory" in row["title"].lower()
