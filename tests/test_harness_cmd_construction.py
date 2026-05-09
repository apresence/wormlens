"""Harness cmd-construction tests.

Acceptance for the harness-passthrough fix: `--dangerously-skip-permissions`
must NOT appear in the constructed `claude` invocation unless the user
explicitly passes it via `--` passthru, and arbitrary user-provided flags
must be forwarded verbatim.
"""
from __future__ import annotations

import pytest

from wormlens.harness.wormlens import (
    _extract_session_id,
    _split_passthru,
    launch_claude,
)


# ---------- _split_passthru ----------

def test_split_passthru_no_separator():
    own, extra = _split_passthru(["--prompt", "hi", "--ctx-limit", "85"])
    assert own == ["--prompt", "hi", "--ctx-limit", "85"]
    assert extra == []


def test_split_passthru_with_separator():
    own, extra = _split_passthru(
        ["--prompt", "hi", "--", "--model", "claude-opus-4-7"]
    )
    assert own == ["--prompt", "hi"]
    assert extra == ["--model", "claude-opus-4-7"]


def test_split_passthru_separator_only():
    own, extra = _split_passthru(["--"])
    assert own == []
    assert extra == []


def test_split_passthru_empty_passthru():
    own, extra = _split_passthru(["--prompt", "hi", "--"])
    assert own == ["--prompt", "hi"]
    assert extra == []


# ---------- _extract_session_id ----------

def test_extract_session_id_absent():
    sid, filtered = _extract_session_id(["--model", "claude-opus-4-7"])
    assert sid is None
    assert filtered == ["--model", "claude-opus-4-7"]


def test_extract_session_id_space_form():
    sid, filtered = _extract_session_id(
        ["--session-id", "abc-123", "--model", "claude-opus-4-7"]
    )
    assert sid == "abc-123"
    assert filtered == ["--model", "claude-opus-4-7"]


def test_extract_session_id_equals_form():
    sid, filtered = _extract_session_id(
        ["--session-id=abc-123", "--model", "claude-opus-4-7"]
    )
    assert sid == "abc-123"
    assert filtered == ["--model", "claude-opus-4-7"]


def test_extract_session_id_only():
    sid, filtered = _extract_session_id(["--session-id", "abc-123"])
    assert sid == "abc-123"
    assert filtered == []


# ---------- launch_claude cmd construction ----------

class _CapturedPopen:
    """Capture subprocess.Popen args without actually launching anything."""
    instances = []

    def __init__(self, cmd, **kwargs):
        type(self).instances.append((cmd, kwargs))
        self.pid = 99999

    def wait(self, *a, **kw):
        return 0

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.fixture
def capture_popen(monkeypatch):
    _CapturedPopen.instances = []
    import wormlens.harness.wormlens as harness
    monkeypatch.setattr(harness.subprocess, "Popen", _CapturedPopen)
    # avoid pty + fcntl side-effects that don't matter for cmd construction
    monkeypatch.setattr(harness.pty, "openpty", lambda: (0, 1))
    monkeypatch.setattr(harness.os, "close", lambda fd: None)
    monkeypatch.setattr(harness.fcntl, "fcntl", lambda *a, **kw: 0)
    monkeypatch.setattr(harness.sys.stdin, "isatty", lambda: False)
    yield _CapturedPopen
    _CapturedPopen.instances = []


def test_default_cmd_has_no_skip_permissions(capture_popen):
    launch_claude("uuid-1234")
    cmd, _ = capture_popen.instances[-1]
    assert "--dangerously-skip-permissions" not in cmd, (
        "skip-permissions must never appear unless passed via passthru"
    )


def test_default_cmd_shape(capture_popen):
    launch_claude("uuid-1234")
    cmd, _ = capture_popen.instances[-1]
    assert cmd == ["claude", "--session-id", "uuid-1234"]


def test_passthru_args_forwarded(capture_popen):
    launch_claude(
        "uuid-1234",
        ["--model", "claude-haiku-4-5-20251001", "--append-system-prompt", "X"],
    )
    cmd, _ = capture_popen.instances[-1]
    assert cmd == [
        "claude",
        "--session-id", "uuid-1234",
        "--model", "claude-haiku-4-5-20251001",
        "--append-system-prompt", "X",
    ]


def test_user_can_explicitly_pass_skip_permissions(capture_popen):
    """User-supplied passthru is not validated; if they want the dangerous
    flag they get it (and own the risk). This documents the escape hatch."""
    launch_claude("uuid-1234", ["--dangerously-skip-permissions"])
    cmd, _ = capture_popen.instances[-1]
    assert cmd == [
        "claude",
        "--session-id", "uuid-1234",
        "--dangerously-skip-permissions",
    ]
