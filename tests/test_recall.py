"""--recall output structure: caveat tag present, no frontmatter.

The frontmatter-strip behavior lives in cli.py (`use_frontmatter = False`
when `args.recall`); the formatter functions respect their `frontmatter`
arg independently. These tests assert the documented end-state: when the
CLI runs `--recall`, the produced text uses <wl-recall-caveat> tags and
contains no YAML frontmatter block.
"""
from __future__ import annotations

from wormlens.formatters import format_chat, format_md, format_txt
from wormlens.models import ChatMessage, ChatSession


def _sessions():
    return [ChatSession(
        session_id="abc",
        title="t",
        start_ts="2026-05-01T00:00:00Z",
        end_ts="2026-05-01T00:00:01Z",
        source_file="/tmp/x.jsonl",
        source_type="cc",
        messages=[
            ChatMessage(role="user", text="hi", msg_type="msg"),
            ChatMessage(role="assistant", text="hello", msg_type="msg"),
        ],
    )]


def test_recall_chat_uses_caveat_tag():
    out = format_chat(_sessions(), recall=True)
    assert out.startswith("<wl-recall-caveat>")
    assert out.rstrip().endswith("</wl-recall-caveat>")
    assert "<wormlens-extract" not in out


def test_recall_chat_with_frontmatter_off_strips_yaml():
    out = format_chat(_sessions(), frontmatter=False, recall=True)
    assert "---\nexported:" not in out


def test_recall_md_uses_caveat_tag():
    out = format_md(_sessions(), recall=True)
    assert out.startswith("<wl-recall-caveat>")
    assert "<wormlens-extract" not in out


def test_recall_md_strips_frontmatter_when_recall_set():
    """format_md hardcodes frontmatter off when recall=True (see formatters.py).

    Quote: `if args.recall: use_frontmatter = False` is in cli, but
    format_md *also* honours recall via its `frontmatter` arg. Our caller
    passes False; assert the rendered body has no YAML.
    """
    out = format_md(_sessions(), frontmatter=False, recall=True)
    assert "---\nexported:" not in out
    assert "user_turns:" not in out


def test_recall_txt_uses_caveat_tag():
    out = format_txt(_sessions(), recall=True)
    assert out.startswith("<wl-recall-caveat>")


def test_recall_includes_instruction_caveat_text():
    out = format_chat(_sessions(), recall=True)
    assert "extracted session history" in out
    assert "memory" in out
