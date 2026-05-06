"""<wl-checkpoint> parsing: multi-tag in one msg, malformed, escaped."""
from __future__ import annotations

import json

from wormlens.models import FilterOpts
from wormlens.providers.claude_code.parser import (
    ClaudeCodeProvider,
    _WL_CHECKPOINT_RE,
)
from tests.conftest import cc_assistant_record, cc_user_record, write_jsonl


def test_multi_checkpoint_tags_per_message(cc_session_with_checkpoints):
    sessions = ClaudeCodeProvider().parse_file(
        cc_session_with_checkpoints, FilterOpts())
    assert len(sessions) == 1
    cps = sessions[0].checkpoints
    assert len(cps) == 3
    texts = [c["text"] for c in cps]
    assert texts == ["plan drafted", "step 1 complete", "step 2 started"]


def test_checkpoint_turn_tracks_jsonl_line(cc_session_with_checkpoints):
    sessions = ClaudeCodeProvider().parse_file(
        cc_session_with_checkpoints, FilterOpts())
    cps = sessions[0].checkpoints
    # Three checkpoints across two assistant lines: line 2 (one) and
    # line 4 (two). _WL_CHECKPOINT_RE finds both on line 4.
    turns = [c["turn"] for c in cps]
    assert turns[0] == 2
    assert turns[1] == 4
    assert turns[2] == 4


def test_checkpoint_regex_handles_multiline_body():
    text = "<wl-checkpoint>multi\nline\nbody</wl-checkpoint>"
    matches = _WL_CHECKPOINT_RE.findall(text)
    assert len(matches) == 1
    assert "multi\nline\nbody" in matches[0]


def test_checkpoint_regex_ignores_malformed():
    """Malformed (no close) tags are not extracted."""
    text = "<wl-checkpoint>never closed"
    assert _WL_CHECKPOINT_RE.findall(text) == []


def test_checkpoint_extracts_truncates_to_160_chars(tmp_path):
    long_body = "x" * 500
    sid = "66666666-6666-6666-6666-666666666666"
    records = [
        cc_user_record("hi", sid=sid),
        cc_assistant_record(
            f"<wl-checkpoint>{long_body}</wl-checkpoint>",
            sid=sid, ts="2026-05-01T00:00:01.000Z",
        ),
    ]
    p = tmp_path / "cp_long.jsonl"
    write_jsonl(p, records)
    sessions = ClaudeCodeProvider().parse_file(p, FilterOpts())
    assert len(sessions[0].checkpoints) == 1
    assert len(sessions[0].checkpoints[0]["text"]) == 160


def test_no_checkpoints_in_empty_session(cc_session_path):
    sessions = ClaudeCodeProvider().parse_file(cc_session_path, FilterOpts())
    assert sessions[0].checkpoints == []
