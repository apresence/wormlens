"""--handoff validation: must require <wl-summary> tag in latest assistant msg."""
from __future__ import annotations

import json
import re

import pytest

from wormlens.cli import _do_handoff


_SUMMARY_RX = re.compile(r"<wl-summary>(.*?)</wl-summary>", re.DOTALL)


def _scan_for_summary(path):
    """Mirror of the byte-level scan inside _do_handoff."""
    found = None
    for raw in path.read_bytes().splitlines()[::-1]:
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        m = _SUMMARY_RX.search(content)
        if m:
            found = m.group(1).strip()
            break
    return found


def test_scan_finds_summary_in_synthetic_file(cc_session_with_summary):
    assert _scan_for_summary(cc_session_with_summary) == "shipped feature X, see PR 12"


def test_scan_returns_none_without_summary(cc_session_no_summary):
    assert _scan_for_summary(cc_session_no_summary) is None


def test_handoff_function_exits_when_no_session_match(tmp_path, monkeypatch):
    """_do_handoff exits 1 when no session prefix matches any provider."""
    from wormlens.providers import PROVIDERS

    for cls in PROVIDERS.values():
        monkeypatch.setattr(cls, "discover_sessions",
                            lambda self, **kw: [], raising=False)

    marker = tmp_path / "marker"
    with pytest.raises(SystemExit) as ei:
        _do_handoff("nonexistent-prefix", marker)
    assert ei.value.code == 1
    assert not marker.exists()


def test_handoff_function_creates_marker_with_summary(
    cc_session_with_summary, tmp_path, monkeypatch,
):
    """When a session matches and contains <wl-summary>, marker is created."""
    from wormlens.providers import PROVIDERS

    sid_prefix = cc_session_with_summary.stem  # full UUID

    cc_cls = PROVIDERS.get("cc")

    def fake_discover(self, **kw):
        if isinstance(self, cc_cls):
            return [cc_session_with_summary]
        return []

    for cls in PROVIDERS.values():
        monkeypatch.setattr(cls, "discover_sessions",
                            fake_discover, raising=False)

    marker = tmp_path / "out" / "handoff.marker"
    _do_handoff(sid_prefix, marker)
    assert marker.is_file()


def test_handoff_function_exits_when_no_summary_tag(
    cc_session_no_summary, tmp_path, monkeypatch,
):
    """Session present but missing <wl-summary> -- must exit 1."""
    from wormlens.providers import PROVIDERS

    sid_prefix = cc_session_no_summary.stem
    cc_cls = PROVIDERS.get("cc")

    def fake_discover(self, **kw):
        if isinstance(self, cc_cls):
            return [cc_session_no_summary]
        return []

    for cls in PROVIDERS.values():
        monkeypatch.setattr(cls, "discover_sessions",
                            fake_discover, raising=False)

    marker = tmp_path / "marker.no"
    with pytest.raises(SystemExit) as ei:
        _do_handoff(sid_prefix, marker)
    assert ei.value.code == 1
    assert not marker.exists()
