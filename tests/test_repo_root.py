"""_find_repo_root walking: stops at home, finds .git/.github/.claude."""
from __future__ import annotations

from pathlib import Path

from wormlens.cli import _find_repo_root


def test_finds_git_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    found = _find_repo_root(sub)
    assert found == repo.resolve()


def test_finds_github_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".github").mkdir()
    found = _find_repo_root(repo / "nested")
    (repo / "nested").mkdir()
    found = _find_repo_root(repo / "nested")
    assert found == repo.resolve()


def test_finds_claude_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".claude").mkdir()
    found = _find_repo_root(repo)
    assert found == repo.resolve()


def test_stops_at_home(tmp_path, monkeypatch):
    """Walk must stop at home dir to avoid matching ~/.claude as a project."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()  # this should NOT count
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    sub = fake_home / "no-marker-here"
    sub.mkdir()
    found = _find_repo_root(sub)
    assert found is None


def test_returns_none_when_no_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _find_repo_root(plain) is None
