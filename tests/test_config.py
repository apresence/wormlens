"""Discovery configuration: extra globs, default toggles, partial session ids.

Covers wormlens.config (file/env/CLI precedence, glob expansion) plus the
provider-side wiring that consumes it, and the shared partial-id matcher.
Everything runs under tmp_path / monkeypatched env -- never the real trees.
"""
from __future__ import annotations

import json

import pytest

from wormlens import config as wlconfig
from wormlens.providers._base import session_id_matches


# -- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Reset the config singleton and clear env/cwd influence per test."""
    monkeypatch.delenv("WORMLENS_CONFIG", raising=False)
    monkeypatch.delenv("WORMLENS_EXTRA_GLOBS", raising=False)
    monkeypatch.delenv("WORMLENS_NO_DEFAULTS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # cwd to an empty dir so a stray ./.wormlens.* never bleeds in.
    monkeypatch.chdir(tmp_path)
    wlconfig.reset_config()
    yield
    wlconfig.reset_config()


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _touch_jsonl(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


# -- partial session-id matcher ---------------------------------------------


def test_session_id_matches_exact_and_prefix():
    full = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert session_id_matches(full, full)         # exact
    assert session_id_matches(full, "aaaaaa")     # 6-hex prefix
    assert session_id_matches(full, "aaaaaaaa-bbbb")
    assert not session_id_matches(full, "bbbbbb")  # not a prefix
    assert not session_id_matches(full, "zzz")


def test_session_id_matches_empty_selector_matches_all():
    assert session_id_matches("anything", None)
    assert session_id_matches("anything", "")


def test_session_id_matches_none_actual():
    assert not session_id_matches(None, "abc")


# -- config loading: defaults + toggles --------------------------------------


def test_no_config_uses_defaults():
    cfg = wlconfig.WormlensConfig.load()
    assert cfg.loaded_path is None
    assert cfg.error is None
    assert cfg.use_defaults("cc") is True
    assert cfg.globs("cc") == []


def test_json_config_globs_and_toggle(tmp_path):
    cfg_path = _write_json(tmp_path / "c.json", {
        "use_defaults": False,
        "extra_globs": ["/generic/*.jsonl"],
        "sources": {
            "cc": {"extra_globs": ["/cc/**/*.jsonl"], "use_defaults": True},
            "codex": {"extra_globs": ["/codex/*.jsonl"]},
        },
    })
    cfg = wlconfig.WormlensConfig.load(config_path=str(cfg_path))
    assert cfg.loaded_path == cfg_path
    assert cfg.global_use_defaults is False
    assert cfg.use_defaults("cc") is True       # source override wins
    assert cfg.use_defaults("codex") is False   # inherits global
    # generic globs apply to every provider; source globs stack on top
    assert cfg.globs("cc") == ["/generic/*.jsonl", "/cc/**/*.jsonl"]
    assert cfg.globs("codex") == ["/generic/*.jsonl", "/codex/*.jsonl"]


def test_config_source_aliases(tmp_path):
    cfg_path = _write_json(tmp_path / "c.json", {
        "sources": {"claude_code": {"extra_globs": ["/aliased/*.jsonl"]}},
    })
    cfg = wlconfig.WormlensConfig.load(config_path=str(cfg_path))
    assert cfg.globs("cc") == ["/aliased/*.jsonl"]


def test_missing_explicit_config_reports_error(tmp_path):
    cfg = wlconfig.WormlensConfig.load(config_path=str(tmp_path / "nope.json"))
    assert cfg.error and "not found" in cfg.error


def test_bad_json_config_reports_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    cfg = wlconfig.WormlensConfig.load(config_path=str(bad))
    assert cfg.error is not None


def test_env_var_explicit_config(tmp_path, monkeypatch):
    cfg_path = _write_json(tmp_path / "env.json", {"extra_globs": ["/from/env/*.jsonl"]})
    monkeypatch.setenv("WORMLENS_CONFIG", str(cfg_path))
    cfg = wlconfig.WormlensConfig.load()
    assert cfg.globs("cc") == ["/from/env/*.jsonl"]


@pytest.mark.skipif(wlconfig._tomllib is None, reason="tomllib (3.11+) not available")
def test_toml_config(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(
        'use_defaults = false\n'
        'extra_globs = ["/g/*.jsonl"]\n\n'
        '[sources.cc]\n'
        'extra_globs = ["/cc/*.jsonl"]\n',
        encoding="utf-8",
    )
    cfg = wlconfig.WormlensConfig.load(config_path=str(toml))
    assert cfg.global_use_defaults is False
    assert cfg.globs("cc") == ["/g/*.jsonl", "/cc/*.jsonl"]


# -- env + CLI precedence ----------------------------------------------------


def test_env_globs_and_no_defaults(monkeypatch):
    monkeypatch.setenv("WORMLENS_EXTRA_GLOBS", "/a/*.jsonl,/b/*.jsonl")
    monkeypatch.setenv("WORMLENS_NO_DEFAULTS", "1")
    cfg = wlconfig.WormlensConfig.load()
    assert cfg.global_use_defaults is False
    assert cfg.globs("cc") == ["/a/*.jsonl", "/b/*.jsonl"]


def test_cli_overrides():
    cfg = wlconfig.WormlensConfig.load(
        cli_extra_globs=["/cli/one/*.jsonl", "/cli/two/*.jsonl"],
        cli_no_defaults=True,
    )
    assert cfg.global_use_defaults is False
    assert cfg.globs("cc") == ["/cli/one/*.jsonl", "/cli/two/*.jsonl"]


def test_configure_singleton_roundtrip():
    cfg = wlconfig.configure(extra_globs=["/x/*.jsonl"], no_defaults=True)
    assert wlconfig.get_config() is cfg
    assert cfg.use_defaults("cc") is False


# -- glob expansion ----------------------------------------------------------


def test_glob_expands_flat_dir(tmp_path):
    d = tmp_path / "dump"
    _touch_jsonl(d / "a.jsonl")
    _touch_jsonl(d / "b.jsonl")
    (d / "note.txt").write_text("x")  # non-jsonl ignored
    cfg = wlconfig.WormlensConfig.load(cli_extra_globs=[str(d / "*.jsonl")])
    files = cfg.extra_files("cc")
    assert {p.name for p in files} == {"a.jsonl", "b.jsonl"}


def test_glob_recursive(tmp_path):
    _touch_jsonl(tmp_path / "proj1" / "x.jsonl")
    _touch_jsonl(tmp_path / "proj2" / "sub" / "y.jsonl")
    cfg = wlconfig.WormlensConfig.load(cli_extra_globs=[str(tmp_path / "**" / "*.jsonl")])
    assert {p.name for p in cfg.extra_files("cc")} == {"x.jsonl", "y.jsonl"}


def test_glob_exact_file(tmp_path):
    f = _touch_jsonl(tmp_path / "one.jsonl")
    cfg = wlconfig.WormlensConfig.load(cli_extra_globs=[str(f)])
    assert [p.name for p in cfg.extra_files("cc")] == ["one.jsonl"]


def test_bare_dir_matches_nothing(tmp_path):
    """A plain dir path (no glob, no /*.jsonl) matches the dir itself, not its
    files -- the documented 'no magic' behavior. Doctor flags this as 0 files."""
    d = tmp_path / "projects"
    _touch_jsonl(d / "a.jsonl")
    cfg = wlconfig.WormlensConfig.load(cli_extra_globs=[str(d)])
    assert cfg.extra_files("cc") == []
    matches = cfg.glob_matches("cc")
    assert matches[0][1] == []   # (pattern, []) -- zero match, doctor will warn


def test_glob_dedups_across_patterns(tmp_path):
    f = _touch_jsonl(tmp_path / "dir" / "dup.jsonl")
    cfg = wlconfig.WormlensConfig.load(
        cli_extra_globs=[str(tmp_path / "dir" / "*.jsonl"),
                         str(tmp_path / "**" / "*.jsonl")],
    )
    assert [p.name for p in cfg.extra_files("cc")] == ["dup.jsonl"]


# -- provider integration (claude code) --------------------------------------


def _make_cc_projects(root, sid):
    proj = root / "-tmp-fake-project"
    rec = {
        "type": "user",
        "sessionId": sid,
        "timestamp": "2026-05-01T00:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return proj


def test_cc_glob_discovery_no_defaults(tmp_path):
    from wormlens.providers.claude_code import parser as cc
    extra = tmp_path / "extra_projects"
    sid = "abcdef12-0000-0000-0000-000000000000"
    _make_cc_projects(extra, sid)
    # point a recursive glob at the extra tree; defaults off
    wlconfig.configure(extra_globs=[str(extra / "**" / "*.jsonl")], no_defaults=True)
    found = cc._all_session_jsonls()
    assert any(p.name == f"{sid}.jsonl" for p in found)
    assert cc._projects_dirs() == []  # defaults disabled


def test_cc_partial_session_id_filter(tmp_path):
    from wormlens.providers.claude_code.parser import ClaudeCodeProvider
    from wormlens.models import FilterOpts
    sid = "abcdef12-3456-7890-abcd-ef1234567890"
    rec = {
        "type": "user", "sessionId": sid, "timestamp": "2026-05-01T00:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    got = ClaudeCodeProvider().parse_file(p, FilterOpts(), session_id_filter="abcdef")
    assert len(got) == 1 and got[0].session_id == sid
    miss = ClaudeCodeProvider().parse_file(p, FilterOpts(), session_id_filter="ffffff")
    assert miss == []
