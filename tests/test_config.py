"""Discovery configuration: extra dirs, default toggles, partial session ids.

Covers wormlens.config (file/env/CLI precedence) plus the provider-side
wiring that consumes it, and the shared partial-id matcher. Everything runs
under tmp_path / monkeypatched env -- never touches the user's real trees.
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
    monkeypatch.delenv("WORMLENS_EXTRA_DIRS", raising=False)
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


# -- config file loading -----------------------------------------------------


def test_no_config_uses_defaults():
    cfg = wlconfig.WormlensConfig.load()
    assert cfg.loaded_path is None
    assert cfg.error is None
    assert cfg.use_defaults("cc") is True
    assert cfg.extra_dirs("cc") == []


def test_json_config_extra_dirs_and_toggle(tmp_path):
    cfg_path = _write_json(tmp_path / "c.json", {
        "use_defaults": False,
        "extra_dirs": ["/generic/one"],
        "sources": {
            "cc": {"extra_dirs": ["/cc/projects"], "use_defaults": True},
            "codex": {"extra_dirs": ["/codex/sessions"]},
        },
    })
    cfg = wlconfig.WormlensConfig.load(config_path=str(cfg_path))
    assert cfg.loaded_path == cfg_path
    # global default off, but cc overrides back on
    assert cfg.global_use_defaults is False
    assert cfg.use_defaults("cc") is True
    assert cfg.use_defaults("codex") is False  # inherits global
    # generic dirs apply to every provider; source dirs stack on top
    assert [str(p) for p in cfg.extra_dirs("cc")] == ["/generic/one", "/cc/projects"]
    assert [str(p) for p in cfg.extra_dirs("codex")] == ["/generic/one", "/codex/sessions"]


def test_config_source_aliases(tmp_path):
    cfg_path = _write_json(tmp_path / "c.json", {
        "sources": {"claude_code": {"extra_dirs": ["/aliased"]}},
    })
    cfg = wlconfig.WormlensConfig.load(config_path=str(cfg_path))
    assert [str(p) for p in cfg.extra_dirs("cc")] == ["/aliased"]


def test_missing_explicit_config_reports_error(tmp_path):
    cfg = wlconfig.WormlensConfig.load(config_path=str(tmp_path / "nope.json"))
    assert cfg.error and "not found" in cfg.error


def test_bad_json_config_reports_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    cfg = wlconfig.WormlensConfig.load(config_path=str(bad))
    assert cfg.error is not None


def test_env_var_explicit_config(tmp_path, monkeypatch):
    cfg_path = _write_json(tmp_path / "env.json", {"extra_dirs": ["/from/env"]})
    monkeypatch.setenv("WORMLENS_CONFIG", str(cfg_path))
    cfg = wlconfig.WormlensConfig.load()
    assert [str(p) for p in cfg.extra_dirs("cc")] == ["/from/env"]


@pytest.mark.skipif(wlconfig._tomllib is None, reason="tomllib (3.11+) not available")
def test_toml_config(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(
        'use_defaults = false\n'
        'extra_dirs = ["/g"]\n\n'
        '[sources.cc]\n'
        'extra_dirs = ["/cc"]\n',
        encoding="utf-8",
    )
    cfg = wlconfig.WormlensConfig.load(config_path=str(toml))
    assert cfg.global_use_defaults is False
    assert [str(p) for p in cfg.extra_dirs("cc")] == ["/g", "/cc"]


# -- env + CLI precedence ----------------------------------------------------


def test_env_extra_dirs_and_no_defaults(monkeypatch):
    monkeypatch.setenv("WORMLENS_EXTRA_DIRS", "/a,/b")
    monkeypatch.setenv("WORMLENS_NO_DEFAULTS", "1")
    cfg = wlconfig.WormlensConfig.load()
    assert cfg.global_use_defaults is False
    assert [str(p) for p in cfg.extra_dirs("cc")] == ["/a", "/b"]


def test_cli_overrides(monkeypatch):
    cfg = wlconfig.WormlensConfig.load(
        cli_extra_dirs=["/cli/one", "/cli/two"],
        cli_no_defaults=True,
    )
    assert cfg.global_use_defaults is False
    assert [str(p) for p in cfg.extra_dirs("cc")] == ["/cli/one", "/cli/two"]


def test_configure_singleton_roundtrip(monkeypatch):
    cfg = wlconfig.configure(extra_dirs=["/x"], no_defaults=True)
    assert wlconfig.get_config() is cfg
    assert cfg.use_defaults("cc") is False


def test_resolve_roots_dedup_and_order():
    cfg = wlconfig.WormlensConfig(generic_extra=[], source_extra={"cc": []})
    from pathlib import Path
    roots = cfg.resolve_roots("cc", [Path("/default"), Path("/default")])
    assert roots == [Path("/default")]


def test_resolve_roots_disabled_defaults():
    from pathlib import Path
    cfg = wlconfig.WormlensConfig(global_use_defaults=False,
                                  source_extra={"cc": [Path("/only")]})
    assert cfg.resolve_roots("cc", [Path("/default")]) == [Path("/only")]


# -- provider integration (claude code) --------------------------------------


def _make_cc_projects(root, sid):
    """Build <root>/<project>/<sid>.jsonl with one tiny CC record."""
    proj = root / "-tmp-fake-project"
    proj.mkdir(parents=True)
    rec = {
        "type": "user",
        "sessionId": sid,
        "timestamp": "2026-05-01T00:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }
    (proj / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return proj


def test_cc_extra_dir_discovery(tmp_path, monkeypatch):
    """An extra projects dir is scanned alongside (or instead of) the default."""
    from wormlens.providers.claude_code import parser as cc

    extra = tmp_path / "extra_projects"
    sid = "abcdef12-0000-0000-0000-000000000000"
    _make_cc_projects(extra, sid)

    # no_defaults so the test does not depend on the host's real ~/.claude
    wlconfig.configure(extra_dirs=[str(extra)], no_defaults=True)

    dirs = cc._projects_dirs()
    assert extra in dirs
    found = cc._all_session_jsonls()
    assert any(p.name == f"{sid}.jsonl" for p in found)


def test_cc_no_default_dirs_excludes_default(tmp_path, monkeypatch):
    from wormlens.providers.claude_code import parser as cc

    fake_default = tmp_path / "default_projects"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    _make_cc_projects(fake_default, "11111111-0000-0000-0000-000000000000")

    extra = tmp_path / "extra"
    _make_cc_projects(extra, "22222222-0000-0000-0000-000000000000")

    wlconfig.configure(extra_dirs=[str(extra)], no_defaults=True)
    dirs = cc._projects_dirs()
    assert extra in dirs
    assert cc._get_projects_dir() not in dirs


def test_cc_partial_session_id_filter(tmp_path):
    """A 6-hex prefix selects the full-UUID session at record level."""
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
