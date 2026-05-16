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
