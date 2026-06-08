"""Duplicate-session collapsing (pipeline.dedupe_sessions).

Models the backup-dir collision: the same session id discovered in more than
one file. keep = newest|oldest (by source-file mtime) | all.
"""
from __future__ import annotations

import os

from wormlens.models import ChatSession
from wormlens.pipeline import dedupe_sessions


def _session(tmp_path, sid, name, mtime, source_type="cc"):
    """A minimal ChatSession backed by a real file with a controlled mtime."""
    f = tmp_path / name
    f.write_text("{}\n", encoding="utf-8")
    os.utime(f, (mtime, mtime))
    return ChatSession(
        session_id=sid,
        title=sid,
        start_ts="",
        end_ts="",
        source_file=str(f),
        source_type=source_type,
        messages=[],
    )


def test_keep_newest(tmp_path):
    old = _session(tmp_path, "sid-1", "old.jsonl", mtime=1000)
    new = _session(tmp_path, "sid-1", "new.jsonl", mtime=2000)
    out = dedupe_sessions([old, new], "newest")
    assert len(out) == 1
    assert out[0].source_file.endswith("new.jsonl")


def test_keep_oldest(tmp_path):
    old = _session(tmp_path, "sid-1", "old.jsonl", mtime=1000)
    new = _session(tmp_path, "sid-1", "new.jsonl", mtime=2000)
    out = dedupe_sessions([new, old], "oldest")
    assert len(out) == 1
    assert out[0].source_file.endswith("old.jsonl")


def test_keep_all(tmp_path):
    a = _session(tmp_path, "sid-1", "a.jsonl", mtime=1000)
    b = _session(tmp_path, "sid-1", "b.jsonl", mtime=2000)
    out = dedupe_sessions([a, b], "all")
    assert len(out) == 2


def test_distinct_sessions_untouched(tmp_path):
    a = _session(tmp_path, "sid-1", "a.jsonl", mtime=1000)
    b = _session(tmp_path, "sid-2", "b.jsonl", mtime=2000)
    out = dedupe_sessions([a, b], "newest")
    assert {s.session_id for s in out} == {"sid-1", "sid-2"}


def test_same_id_different_provider_not_merged(tmp_path):
    a = _session(tmp_path, "sid-1", "a.jsonl", mtime=1000, source_type="cc")
    b = _session(tmp_path, "sid-1", "b.jsonl", mtime=2000, source_type="codex")
    out = dedupe_sessions([a, b], "newest")
    assert len(out) == 2  # dedup key is (source_type, session_id)


def test_first_seen_order_preserved(tmp_path):
    a = _session(tmp_path, "sid-a", "a.jsonl", mtime=1000)
    b = _session(tmp_path, "sid-b", "b.jsonl", mtime=2000)
    c = _session(tmp_path, "sid-a", "c.jsonl", mtime=3000)  # dup of a
    out = dedupe_sessions([a, b, c], "newest")
    assert [s.session_id for s in out] == ["sid-a", "sid-b"]
    assert out[0].source_file.endswith("c.jsonl")  # newest copy of sid-a
