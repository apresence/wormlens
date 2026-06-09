"""Uniform session-discovery scope across commands (the 0.4.2 refactor).

Covers:
  * `list_sessions_metadata(paths=...)` is a pure summarizer -- it summarizes
    exactly the passed files and never re-discovers (the vscode-single-workspace
    / codex-dropped-archived bug). Bare fallbacks now scan the full set.
  * `_apply_last` / `_collect_session_paths` -- the central --last N scope knob.
  * recall flood-control: -n tails, --last >1 warns.
  * UTF-8 stdout reconfigure (charmap-on-pipe fix).
  * config extra_globs surfaces in --list-sessions AND --checkpoints, not just
    --grep.

Everything runs under tmp_path with monkeypatched discovery -- never the real
~/.claude / ~/.codex / VS Code trees.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from wormlens import cli
from wormlens import config as wlconfig


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Reset the config singleton + env, and run from an empty cwd so a stray
    ./.wormlens.* never bleeds into a test."""
    for var in ("WORMLENS_CONFIG", "WORMLENS_EXTRA_GLOBS",
                "WORMLENS_NO_DEFAULTS", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    wlconfig.reset_config()
    yield
    wlconfig.reset_config()


# -- builders ----------------------------------------------------------------


class _StubProvider:
    """Minimal provider stand-in for the cli discovery helpers."""

    def __init__(self, pid, paths):
        self.provider_id = pid
        self.provider_label = pid
        self._paths = list(paths)

    def discover_sessions(self, **kwargs):
        return list(self._paths)


def _touch(tmp_path, name, mtime):
    p = tmp_path / name
    p.write_text("{}\n", encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def _cc_jsonl(directory, name, sid, mtime=1000, checkpoint=None):
    """A minimal Claude Code session file (one user turn, optional checkpoint).

    cc derives the listed session_id from the filename stem, so name the file
    after the sid when the test asserts on it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    recs = [{
        "type": "user", "sessionId": sid,
        "timestamp": "2026-05-01T00:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }]
    asst_text = "ok"
    if checkpoint:
        asst_text = f"ok\n<wl-checkpoint>{checkpoint}</wl-checkpoint>"
    recs.append({
        "type": "assistant", "sessionId": sid,
        "timestamp": "2026-05-01T00:00:01.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": asst_text}]},
    })
    p = directory / name
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def _args(**kw):
    base = dict(input=None, storage_id=None, last=None)
    base.update(kw)
    return SimpleNamespace(**base)


# -- _apply_last -------------------------------------------------------------


def test_apply_last_orders_by_mtime_and_slices(tmp_path):
    a = _touch(tmp_path, "a.jsonl", 1000)
    b = _touch(tmp_path, "b.jsonl", 3000)
    c = _touch(tmp_path, "c.jsonl", 2000)
    src = _StubProvider("cc", [a, b, c])
    out = cli._apply_last([(src, [a, b, c])], 2)
    flat = [p for _s, paths in out for p in paths]
    assert flat == [b, c]  # newest two, newest first


def test_apply_last_none_is_noop(tmp_path):
    a = _touch(tmp_path, "a.jsonl", 1000)
    src = _StubProvider("cc", [a])
    assert cli._apply_last([(src, [a])], None) == [(src, [a])]
    assert cli._apply_last([(src, [a])], 0) == [(src, [a])]


def test_apply_last_regroups_across_providers(tmp_path):
    a = _touch(tmp_path, "a.jsonl", 1000)  # cc
    c = _touch(tmp_path, "c.jsonl", 3000)  # cc
    b = _touch(tmp_path, "b.jsonl", 4000)  # codex
    d = _touch(tmp_path, "d.jsonl", 2000)  # codex
    cc = _StubProvider("cc", [a, c])
    cx = _StubProvider("codex", [b, d])
    out = cli._apply_last([(cc, [a, c]), (cx, [b, d])], 3)
    by_pid = {s.provider_id: paths for s, paths in out}
    # newest 3 globally: b(4000), c(3000), d(2000) -- a(1000) dropped
    assert by_pid["codex"] == [b, d]
    assert by_pid["cc"] == [c]


# -- _collect_session_paths --------------------------------------------------


def test_collect_applies_default_last(tmp_path):
    a = _touch(tmp_path, "a.jsonl", 1000)
    b = _touch(tmp_path, "b.jsonl", 2000)
    src = _StubProvider("cc", [a, b])
    out = cli._collect_session_paths(_args(), [src], all_sessions=True, last_default=1)
    assert [p for _s, paths in out for p in paths] == [b]


def test_collect_explicit_last_overrides_default(tmp_path):
    a = _touch(tmp_path, "a.jsonl", 1000)
    b = _touch(tmp_path, "b.jsonl", 2000)
    src = _StubProvider("cc", [a, b])
    out = cli._collect_session_paths(_args(last=2), [src], all_sessions=True, last_default=1)
    assert {p for _s, paths in out for p in paths} == {a, b}


def test_collect_no_default_returns_all(tmp_path):
    a = _touch(tmp_path, "a.jsonl", 1000)
    b = _touch(tmp_path, "b.jsonl", 2000)
    src = _StubProvider("cc", [a, b])
    out = cli._collect_session_paths(_args(), [src], all_sessions=True)
    assert {p for _s, paths in out for p in paths} == {a, b}


def test_collect_explicit_paths_grouped_by_detected_provider(tmp_path):
    from wormlens.providers import PROVIDERS
    p1 = _cc_jsonl(tmp_path, "s1.jsonl", "sid-1", mtime=1000)
    p2 = _cc_jsonl(tmp_path, "s2.jsonl", "sid-2", mtime=2000)
    sources = [c() for c in PROVIDERS.values()]
    out = cli._collect_session_paths(_args(input=[str(p1), str(p2)]),
                                     sources, all_sessions=True)
    by_pid = {s.provider_id: paths for s, paths in out}
    assert "cc" in by_pid
    assert set(by_pid["cc"]) == {p1, p2}


# -- list_sessions_metadata is a pure summarizer -----------------------------


def test_cc_list_summarizes_passed_paths_without_rediscovery(tmp_path, monkeypatch):
    from wormlens.providers.claude_code import parser as cc

    def boom():
        raise AssertionError("re-discovered despite explicit paths")

    monkeypatch.setattr(cc, "_all_session_jsonls", boom)
    f = _cc_jsonl(tmp_path, "cc-1.jsonl", "cc-1")
    rows = cc.ClaudeCodeProvider().list_sessions_metadata(paths=[f])
    assert len(rows) == 1
    assert rows[0]["session_id"] == "cc-1"
    assert rows[0]["source_type"] == "cc"


def test_codex_list_summarizes_passed_paths_without_rediscovery(tmp_path, monkeypatch):
    from wormlens.providers.codex import parser as cx

    def boom(*a, **k):
        raise AssertionError("re-discovered despite explicit paths")

    monkeypatch.setattr(cx, "_find_rollouts", boom)
    meta = {"type": "session_meta", "timestamp": "2026-05-01T00:00:00Z",
            "payload": {"id": "cx-1", "timestamp": "2026-05-01T00:00:00Z",
                        "cwd": "/x", "cli_version": "1.0"}}
    user = {"type": "response_item", "timestamp": "2026-05-01T00:00:01Z",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}]}}
    f = tmp_path / "rollout-x.jsonl"
    f.write_text(json.dumps(meta) + "\n" + json.dumps(user) + "\n", encoding="utf-8")
    rows = cx.CodexProvider().list_sessions_metadata(paths=[f])
    assert len(rows) == 1 and rows[0]["session_id"] == "cx-1"


def test_codex_list_bare_fallback_includes_archived(monkeypatch):
    from wormlens.providers.codex import parser as cx
    captured = {}

    def fake(all_sessions=False, **k):
        captured["all_sessions"] = all_sessions
        return []

    monkeypatch.setattr(cx, "_find_rollouts", fake)
    cx.CodexProvider().list_sessions_metadata()
    assert captured["all_sessions"] is True


def test_vscode_list_summarizes_passed_paths_without_rediscovery(tmp_path, monkeypatch):
    from wormlens.providers.vscode_copilot import parser as vsc

    def boom(*a, **k):
        raise AssertionError("re-discovered despite explicit paths")

    monkeypatch.setattr(vsc, "_find_chat_sessions", boom)
    state = {"kind": 0, "v": {"sessionId": "vs-1", "customTitle": "T",
                              "creationDate": 1700000000000,
                              "requests": [{"message": {"text": "hi"}}]}}
    f = tmp_path / "vs.jsonl"
    f.write_text(json.dumps(state) + "\n", encoding="utf-8")
    rows = vsc.VSCodeCopilotProvider().list_sessions_metadata(paths=[f])
    assert len(rows) == 1 and rows[0]["session_id"] == "vs-1"
    assert rows[0]["source_type"] == "vscode"


def test_vscode_list_bare_fallback_scans_all_workspaces(monkeypatch):
    from wormlens.providers.vscode_copilot import parser as vsc
    captured = {}

    def fake(*a, all_workspaces=False, **k):
        captured["all_workspaces"] = all_workspaces
        return []

    monkeypatch.setattr(vsc, "_find_chat_sessions", fake)
    vsc.VSCodeCopilotProvider().list_sessions_metadata()
    assert captured["all_workspaces"] is True


# -- UTF-8 stdout fix --------------------------------------------------------


def test_force_utf8_stdio_reconfigures(monkeypatch):
    calls = []

    class FakeStream:
        def reconfigure(self, **k):
            calls.append(k)

    monkeypatch.setattr(sys, "stdout", FakeStream())
    monkeypatch.setattr(sys, "stderr", FakeStream())
    cli._force_utf8_stdio()
    assert len(calls) == 2
    assert all(c == {"encoding": "utf-8", "errors": "replace"} for c in calls)


def test_force_utf8_stdio_tolerates_unreconfigurable_stream(monkeypatch):
    # capsys-style streams have no reconfigure(); must not raise.
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())
    cli._force_utf8_stdio()  # no exception


# -- recall flood-control (integration) --------------------------------------


def test_recall_n_tails_last_records(tmp_path, monkeypatch, capfd):
    # capfd (fd-level) not capsys: write_output opens sys.stdout.fileno().
    from wormlens.providers.claude_code import parser as cc
    recs = [
        {"type": "user", "sessionId": "s", "timestamp": "2026-05-01T00:00:00.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "FIRSTQ"}]}},
        {"type": "assistant", "sessionId": "s", "timestamp": "2026-05-01T00:00:01.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "FIRSTA"}]}},
        {"type": "user", "sessionId": "s", "timestamp": "2026-05-01T00:00:02.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "SECONDQ"}]}},
        {"type": "assistant", "sessionId": "s", "timestamp": "2026-05-01T00:00:03.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "LASTA"}]}},
    ]
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    monkeypatch.setattr(cc, "_all_session_jsonls", lambda: [f])
    monkeypatch.setattr(sys, "argv", ["wl", "--recall", "--source", "cc", "-n", "1"])
    cli.main()
    out = capfd.readouterr().out
    # -n on recall tails: only the final record survives.
    assert "LASTA" in out
    assert "FIRSTQ" not in out and "FIRSTA" not in out


def test_recall_last_gt1_warns(tmp_path, monkeypatch, capfd):
    from wormlens.providers.claude_code import parser as cc
    f1 = _cc_jsonl(tmp_path, "one.jsonl", "one", mtime=1000)
    f2 = _cc_jsonl(tmp_path, "two.jsonl", "two", mtime=2000)
    monkeypatch.setattr(cc, "_all_session_jsonls", lambda: [f2, f1])
    monkeypatch.setattr(sys, "argv",
                        ["wl", "--recall", "--source", "cc", "--last", "2"])
    cli.main()
    err = capfd.readouterr().err
    assert "flood" in err.lower()


# -- config extra_globs honored by all commands ------------------------------


def test_config_extra_glob_surfaces_in_list_sessions(tmp_path, monkeypatch, capsys):
    sid = "cfgsid12-0000-0000-0000-000000000000"
    extra = tmp_path / "extra"
    _cc_jsonl(extra, f"{sid}.jsonl", sid)
    monkeypatch.setattr(sys, "argv", [
        "wl", "--list-sessions", "--source", "cc",
        "--no-default-dirs", "--extra-glob", str(extra / "**" / "*.jsonl"),
        "--min-turns", "1",
    ])
    cli.main()
    out = capsys.readouterr().out
    assert sid in out


def test_config_extra_glob_surfaces_in_checkpoints(tmp_path, monkeypatch, capsys):
    sid = "cfgchk12-0000-0000-0000-000000000000"
    extra = tmp_path / "extra"
    _cc_jsonl(extra, f"{sid}.jsonl", sid, checkpoint="reached milestone")
    monkeypatch.setattr(sys, "argv", [
        "wl", "--checkpoints", "--source", "cc",
        "--no-default-dirs", "--extra-glob", str(extra / "**" / "*.jsonl"),
    ])
    cli.main()
    out = capsys.readouterr().out
    assert "reached milestone" in out
