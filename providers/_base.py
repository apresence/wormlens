"""Abstract base class for provider backends."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

from ..models import ChatSession, FilterOpts


_EXTRACT_OPEN_RE = re.compile(r'<wormlens-extract[^>]*>\s*\n?', re.DOTALL)
_EXTRACT_COMMENT_RE = re.compile(
    r'<!--\s*This is episodic memory[^>]*-->\s*\n?',
)
_EXTRACT_CLOSE_RE = re.compile(r'\n?</wormlens-extract>\s*')


def session_id_matches(actual: str | None, selector: str | None) -> bool:
    """True when `selector` identifies the session `actual`.

    A None/empty selector matches everything. Otherwise an exact match wins;
    failing that, `selector` is treated as a shortened id and prefix-matched
    against the full id -- so a 6-hex prefix selects the full UUID, git-style.
    """
    if not selector:
        return True
    if not actual:
        return False
    return actual == selector or actual.startswith(selector)


def strip_extract_bookends(text: str) -> str:
    """Remove wormlens-extract wrapper tags while preserving inner content.

    Strips the opening tag (with any attributes), the episodic-memory
    comment line, and the closing tag. The actual extract content between
    them is kept intact.
    """
    text = _EXTRACT_OPEN_RE.sub('', text)
    text = _EXTRACT_COMMENT_RE.sub('', text)
    text = _EXTRACT_CLOSE_RE.sub('', text)
    return text


class Provider(ABC):
    """Base class for chat history extraction backends.

    Each backend implements discovery (finding session files on disk),
    parsing (converting source-specific JSONL into ChatSession objects),
    and detection (identifying whether a file belongs to this provider).
    """

    provider_id: str = ""
    provider_label: str = ""

    @abstractmethod
    def discover_sessions(self, **kwargs) -> list[Path]:
        """Find session files on disk using platform-specific paths."""

    @abstractmethod
    def parse_file(
        self,
        path: Path,
        opts: FilterOpts,
        session_id_filter: str | None = None,
        since_last_compact: bool = False,
    ) -> list[ChatSession]:
        """Parse a file into one or more ChatSession objects."""

    @abstractmethod
    def list_sessions_metadata(self, **kwargs) -> list[dict]:
        """Return metadata dicts for all discoverable sessions.

        Providers may accept optional kwargs (e.g. `paths=[...]`) to
        enumerate sessions inside a caller-supplied file or directory
        rather than the provider's own discovery roots. Providers that
        don't support this can ignore kwargs.
        """

    def discovery_roots(self) -> list[Path]:
        """Return directories this provider scans for sessions.

        Empty list means this provider is file-only (no auto-discovery;
        the caller supplies a file path directly). Used by `wl --list-sources`.
        """
        return []

    def parse_line(self, raw_line: str, opts: FilterOpts, state: dict) -> list:
        """Parse one raw line from a JSONL transcript into ChatMessage objects.

        Streaming entry point used by wormlens.follow. The default implementation
        raises NotImplementedError -- providers that handle line-oriented JSONL
        (Claude Code, Codex, VS Code Copilot) override this. `state` carries
        cross-record bookkeeping (provider-specific shape).

        Return empty list for ignored or unparseable lines.
        """
        raise NotImplementedError(
            f"{self.provider_id} provider does not support line streaming"
        )

    @classmethod
    @abstractmethod
    def detect(cls, path: Path) -> bool:
        """Return True if path looks like a file this backend handles."""

    def get_skill_path(self) -> Path | None:
        return None

    def get_hook_config(self) -> dict | None:
        return None

    def get_harness_config(self) -> dict | None:
        return None
