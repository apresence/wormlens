"""settings.json merge / unmerge tests.

These exercise _install_settings_hooks and _uninstall_settings_hooks
against tmp_path roots only -- never the user's real ~/.claude.
"""
from __future__ import annotations

import json
from pathlib import Path

from wormlens.cli import (
    _install_settings_hooks,
    _uninstall_settings_hooks,
    _SETTINGS_REL,
)


def _read(root: Path) -> dict:
    p = root / _SETTINGS_REL
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def test_install_creates_settings_with_hooks(tmp_path):
    changes = _install_settings_hooks(tmp_path)
    assert "statusLine" in changes
    assert "hooks.UserPromptSubmit" in changes
    assert "hooks.PreToolUse" in changes
    data = _read(tmp_path)
    assert "statusLine" in data
    assert "wormlens" in data["statusLine"]["command"]
    assert "UserPromptSubmit" in data["hooks"]
    assert "PreToolUse" in data["hooks"]


def test_install_is_idempotent(tmp_path):
    _install_settings_hooks(tmp_path)
    second = _install_settings_hooks(tmp_path)
    assert second == []  # nothing changed


def test_install_preserves_user_settings(tmp_path):
    settings_path = tmp_path / _SETTINGS_REL
    settings_path.parent.mkdir(parents=True)
    user = {
        "model": "sonnet",
        "permissions": {"allow": ["Bash(npm:*)"]},
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "echo my-hook"}
                ]}
            ]
        },
    }
    settings_path.write_text(json.dumps(user), encoding="utf-8")
    _install_settings_hooks(tmp_path)
    after = _read(tmp_path)
    # user data preserved
    assert after["model"] == "sonnet"
    assert after["permissions"] == {"allow": ["Bash(npm:*)"]}
    # user's hook still there alongside ours
    cmds = [h["command"] for entry in after["hooks"]["UserPromptSubmit"]
            for h in entry["hooks"]]
    assert any("echo my-hook" in c for c in cmds)
    assert any("wormlens" in c for c in cmds)


def test_uninstall_removes_only_wormlens_entries(tmp_path):
    settings_path = tmp_path / _SETTINGS_REL
    settings_path.parent.mkdir(parents=True)
    user = {
        "model": "sonnet",
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "echo my-hook"}
                ]}
            ]
        },
    }
    settings_path.write_text(json.dumps(user), encoding="utf-8")
    _install_settings_hooks(tmp_path)
    _uninstall_settings_hooks(tmp_path)
    after = _read(tmp_path)
    assert after.get("model") == "sonnet"
    assert "statusLine" not in after
    cmds = [h["command"] for entry in after["hooks"]["UserPromptSubmit"]
            for h in entry["hooks"]]
    assert any("echo my-hook" in c for c in cmds)
    assert not any("wormlens" in c for c in cmds)


def test_uninstall_when_only_wormlens_deletes_file(tmp_path):
    """If wormlens was the only data, settings.json should be removed."""
    _install_settings_hooks(tmp_path)
    _uninstall_settings_hooks(tmp_path)
    assert not (tmp_path / _SETTINGS_REL).is_file()


def test_uninstall_noop_when_absent(tmp_path):
    changes = _uninstall_settings_hooks(tmp_path)
    assert changes == []
