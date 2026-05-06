"""Shared pytest fixtures for the wormlens test suite.

Builds synthetic JSONL records (CC-flavored) and .wl extracts. Tests
must NOT touch the user's real ~/.claude tree -- everything here writes
under tmp_path. ASCII-only fixtures, no real session data.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

# Make the project root importable as the `wormlens` package even when
# pytest is invoked outside an editable install. The repo layout flattens
# the package at the root (cli.py, formatters.py, etc.), so we add the
# parent of the wormlens root to sys.path and rely on pyproject's
# `tool.setuptools.package-dir` mapping. For the tests, we import
# directly via `wormlens.<mod>` since wormlens.egg-info already maps it.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT.parent))


# -- CC JSONL builders -------------------------------------------------------


def cc_user_record(text, sid="11111111-2222-3333-4444-555555555555",
                   ts="2026-05-01T00:00:00.000Z", is_meta=False):
    """Build a CC-shaped user record (text-only)."""
    rec = {
        "type": "user",
        "sessionId": sid,
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    if is_meta:
        rec["isMeta"] = True
    return rec


def cc_assistant_record(text, sid="11111111-2222-3333-4444-555555555555",
                        ts="2026-05-01T00:00:01.000Z", thinking=None,
                        tool_uses=None):
    """Build a CC-shaped assistant record. tool_uses: list of (name, input dict)."""
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking, "signature": "sig"})
    content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        name, inp = tu
        content.append({
            "type": "tool_use",
            "id": f"toolu_{uuid.uuid4().hex[:8]}",
            "name": name,
            "input": inp,
        })
    return {
        "type": "assistant",
        "sessionId": sid,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": content,
        },
    }


def cc_compact_record(sid="11111111-2222-3333-4444-555555555555",
                      ts="2026-05-01T00:01:00.000Z"):
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "sessionId": sid,
        "timestamp": ts,
        "compactMetadata": {"trigger": "manual", "preTokens": 12345},
    }


def write_jsonl(path: Path, records):
    """Serialize records to a JSONL file."""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def cc_session_path(tmp_path):
    """A small CC session JSONL with three turns and one tool_use."""
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    records = [
        cc_user_record("hello world", sid=sid, ts="2026-05-01T00:00:00.000Z"),
        cc_assistant_record(
            "hi there",
            sid=sid,
            ts="2026-05-01T00:00:01.000Z",
            thinking="user said hello, respond politely",
            tool_uses=[("Bash", {"command": "ls -la"})],
        ),
        cc_user_record("thanks", sid=sid, ts="2026-05-01T00:00:02.000Z"),
        cc_assistant_record("you are welcome", sid=sid,
                            ts="2026-05-01T00:00:03.000Z"),
    ]
    p = tmp_path / "cc_simple.jsonl"
    return write_jsonl(p, records)


@pytest.fixture
def cc_session_with_checkpoints(tmp_path):
    """CC session with multiple <wl-checkpoint> tags in assistant replies."""
    sid = "11111111-1111-1111-1111-111111111111"
    records = [
        cc_user_record("start", sid=sid),
        cc_assistant_record(
            "ok working\n<wl-checkpoint>plan drafted</wl-checkpoint>",
            sid=sid, ts="2026-05-01T00:00:01.000Z",
        ),
        cc_user_record("more", sid=sid, ts="2026-05-01T00:00:02.000Z"),
        cc_assistant_record(
            "step done.\n<wl-checkpoint>step 1 complete</wl-checkpoint>\n"
            "now: <wl-checkpoint>step 2 started</wl-checkpoint>",
            sid=sid, ts="2026-05-01T00:00:03.000Z",
        ),
    ]
    p = tmp_path / "cc_checkpoints.jsonl"
    return write_jsonl(p, records)


@pytest.fixture
def cc_session_with_summary(tmp_path):
    """CC session whose last assistant message has a <wl-summary> tag."""
    sid = "22222222-2222-2222-2222-222222222222"
    records = [
        cc_user_record("kick off", sid=sid),
        cc_assistant_record(
            "wrapping up.\n<wl-summary>shipped feature X, see PR 12</wl-summary>",
            sid=sid, ts="2026-05-01T00:00:01.000Z",
        ),
    ]
    p = tmp_path / "cc_summary.jsonl"
    return write_jsonl(p, records)


@pytest.fixture
def cc_session_no_summary(tmp_path):
    sid = "33333333-3333-3333-3333-333333333333"
    records = [
        cc_user_record("kick off", sid=sid),
        cc_assistant_record("just working", sid=sid,
                            ts="2026-05-01T00:00:01.000Z"),
    ]
    p = tmp_path / "cc_nosummary.jsonl"
    return write_jsonl(p, records)


@pytest.fixture
def cc_session_partial_lines(tmp_path):
    """Mixes valid records with a corrupt JSON line and a blank line.

    Per CC-JSONL-SPEC: parser must tolerate non-JSON lines (no abort).
    """
    sid = "44444444-4444-4444-4444-444444444444"
    p = tmp_path / "cc_partial.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(cc_user_record("hello", sid=sid)) + "\n")
        f.write("\n")  # blank line
        f.write("this is not json at all\n")
        f.write("{not closed json\n")
        f.write(json.dumps(cc_assistant_record("hi", sid=sid,
                ts="2026-05-01T00:00:01.000Z")) + "\n")
    return p


@pytest.fixture
def wl_chat_extract_path(tmp_path):
    """A minimal wormlens chat-format extract file."""
    body = (
        '<wormlens-extract format="chat">\n'
        '---\n'
        'exported: "2026-05-01 00:00:00 +0000"\n'
        'sessions: 1\n'
        'user_turns: 2\n'
        'source: "cc"\n'
        '---\n'
        '<session id="abc-123" source="cc" date="2026-05-01">\n'
        '<!-- turn = JSONL line number. /tmp/x.jsonl -->\n'
        '<user turn=1>hello\n'
        '<assistant turn=2>hi\n'
        '<user turn=3>bye\n'
        '<assistant turn=4>see ya\n'
        '</session>\n'
        '</wormlens-extract>\n'
    )
    p = tmp_path / "extract.wl"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def isolated_skill_target(tmp_path):
    """A throwaway dir to use as a skill install target.

    Ensures we never write to the user's real ~/.claude.
    """
    root = tmp_path / "fake-repo"
    root.mkdir()
    (root / ".git").mkdir()  # make it a repo root for _find_repo_root
    return root
