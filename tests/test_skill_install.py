"""Skill install/uninstall path resolution.

Tests against /tmp throwaway dirs only -- never the user's real ~/.claude.
"""
from __future__ import annotations

from pathlib import Path

from wormlens.cli import (
    _install_skill,
    _uninstall_skill,
    _SKILL_REL_DIR,
    _SETTINGS_REL,
)


def test_install_writes_skill_and_hook(isolated_skill_target, capsys):
    _install_skill(str(isolated_skill_target))
    skill_dir = isolated_skill_target / _SKILL_REL_DIR
    assert skill_dir.is_dir()
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "wl-hook.py").is_file()
    # Hook should be executable
    mode = (skill_dir / "wl-hook.py").stat().st_mode & 0o777
    assert mode & 0o100, f"wl-hook.py not user-executable: {oct(mode)}"


def test_install_writes_settings_hooks(isolated_skill_target):
    _install_skill(str(isolated_skill_target))
    assert (isolated_skill_target / _SETTINGS_REL).is_file()


def test_uninstall_cleans_up(isolated_skill_target, capsys):
    _install_skill(str(isolated_skill_target))
    _uninstall_skill(str(isolated_skill_target))
    skill_dir = isolated_skill_target / _SKILL_REL_DIR
    assert not skill_dir.exists()
    # Settings file should be gone (we created a fresh repo with no other data)
    assert not (isolated_skill_target / _SETTINGS_REL).is_file()


def test_install_uses_explicit_target_not_cwd(tmp_path, monkeypatch):
    """When --skill-target is supplied, cwd is not consulted."""
    target = tmp_path / "explicit"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".git").mkdir()
    monkeypatch.chdir(elsewhere)
    _install_skill(str(target))
    assert (target / _SKILL_REL_DIR / "SKILL.md").is_file()
    assert not (elsewhere / _SKILL_REL_DIR).exists()


def test_install_idempotent_with_existing_install(isolated_skill_target, capsys):
    _install_skill(str(isolated_skill_target))
    # Re-running should not error and should be a no-op for settings
    _install_skill(str(isolated_skill_target))
    skill_dir = isolated_skill_target / _SKILL_REL_DIR
    assert (skill_dir / "SKILL.md").is_file()
