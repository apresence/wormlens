"""Argparse smoke tests -- every documented mode/flag must parse cleanly."""
from __future__ import annotations

import pytest

from wormlens.cli import _build_parser, parse_index_spec


def _parse(args):
    return _build_parser().parse_args(args)


def test_bare_no_args_uses_defaults():
    ns = _parse([])
    assert ns.input == []
    assert ns.source == "auto"
    assert ns.fmt == "chat"
    assert ns.full is False


def test_full_flag():
    assert _parse(["--full"]).full is True


def test_tail_alias():
    ns = _parse(["-t", "20"])
    assert ns.tail == 20


def test_all_flag():
    assert _parse(["--all"]).all is True


def test_recall_flag():
    assert _parse(["--recall"]).recall is True


def test_handoff_flag():
    ns = _parse(["--handoff", "--session", "abc"])
    assert ns.handoff is True
    assert ns.session == "abc"


def test_grep_flag():
    ns = _parse(["--grep", "foo"])
    assert ns.grep == "foo"


def test_grep_with_context_flags():
    ns = _parse(["--grep", "foo", "-A", "2", "-B", "1", "-i"])
    assert ns.after == 2 and ns.before == 1 and ns.ignore_case is True


def test_checkpoints_flag():
    assert _parse(["--checkpoints"]).checkpoints is True


def test_doctor_flag():
    assert _parse(["--doctor"]).doctor is True


def test_summary_stats_flag():
    assert _parse(["--summary-stats"]).summary_stats is True


def test_summary_stats_alias():
    assert _parse(["--stats"]).summary_stats is True


def test_list_sessions_flag():
    assert _parse(["--list-sessions"]).list_sessions is True


@pytest.mark.parametrize("source", ["cc", "vscode", "wl", "auto"])
def test_source_choices(source):
    assert _parse(["--source", source]).source == source


def test_unknown_source_rejected():
    with pytest.raises(SystemExit):
        _parse(["--source", "bogus"])


@pytest.mark.parametrize("fmt", ["chat", "md", "txt", "jsonl"])
def test_format_choices(fmt):
    assert _parse(["--format", fmt]).fmt == fmt


def test_unknown_format_rejected():
    with pytest.raises(SystemExit):
        _parse(["--format", "bogus"])


def test_session_id():
    assert _parse(["--session", "abc-123"]).session == "abc-123"


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("42", {42}),
        ("42-44", {42, 43, 44}),
        ("1,3,5", {1, 3, 5}),
        ("1-2,5", {1, 2, 5}),
    ],
)
def test_parse_index_spec_valid(spec, expected):
    assert parse_index_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "abc", "5-3", "5-x", ","])
def test_parse_index_spec_invalid(spec):
    with pytest.raises(ValueError):
        parse_index_spec(spec)


def test_n_and_rev():
    ns = _parse(["-n", "5", "--rev"])
    assert ns.n == 5 and ns.rev is True


def test_min_turns_min_size():
    ns = _parse(["--min-turns", "3", "--min-size", "10KB"])
    assert ns.min_turns == 3 and ns.min_size == "10KB"


def test_merge_and_output():
    ns = _parse(["--merge", "-o", "/tmp/out.md"])
    assert ns.merge is True and ns.output == "/tmp/out.md"


def test_filter_flags():
    ns = _parse([
        "--code-edits", "--hooks", "--bash", "--teammates", "--refs",
        "--system-msgs", "--thinking", "--tools",
    ])
    for attr in ("code_edits", "hooks", "bash", "teammates", "refs",
                 "system_msgs", "thinking", "tools"):
        assert getattr(ns, attr) is True, attr


def test_index_spec_arg():
    assert _parse(["--index", "1-3"]).index == "1-3"


def test_install_uninstall_skill_flags():
    assert _parse(["--install-skill"]).install_skill is True
    assert _parse(["--uninstall-skill"]).uninstall_skill is True
    assert _parse(["--skill-target", "/tmp/x"]).skill_target == "/tmp/x"


def test_version_action(capsys):
    with pytest.raises(SystemExit) as ei:
        _parse(["--version"])
    assert ei.value.code == 0
    captured = capsys.readouterr()
    assert "wormlens" in captured.out
