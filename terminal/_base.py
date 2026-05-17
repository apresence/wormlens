"""Abstract terminal control surface for wormlens.

Backends declare capabilities via a Flag enum; consumers check the flag
before calling a method. The base class provides NotImplementedError
defaults for every method, so a backend only implements what it supports.

Backends so far:
  - tmux (terminal/tmux.py) -- full caps

Designed to grow without refactor: screen, mosh (SEND_KEYS-only), and
pywin32.SendKeys (SEND_KEYS-only) all fit the same protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Flag, auto


class Capabilities(Flag):
    """Operations a TerminalControl backend may support.

    Consumers should `cap & Capabilities.X` before calling X. Methods
    that aren't supported raise NotImplementedError, so the check is
    advisory but recommended for clean fallback paths.
    """
    NONE          = 0
    SEND_KEYS     = auto()  # write keystrokes / text to terminal
    SCREEN_SCRAPE = auto()  # capture visible pane content
    SCROLLBACK    = auto()  # capture history beyond visible area
    RESIZE        = auto()  # change terminal dimensions
    SPAWN         = auto()  # create a new session
    KILL          = auto()  # destroy a session
    LIST          = auto()  # enumerate active sessions
    ATTACH        = auto()  # return command to attach interactively


@dataclass
class Session:
    """Lightweight handle to a controlled terminal session.

    Names are backend-scoped; uniqueness within the backend.
    """
    name: str
    pid: int | None = None
    cwd: str | None = None
    cols: int | None = None
    rows: int | None = None
    metadata: dict = field(default_factory=dict)


class TerminalControl:
    """Abstract terminal control. Backends subclass + override.

    Declares `.capabilities` as a Capabilities flag. Default for every
    method is NotImplementedError so a partial impl (e.g. SEND_KEYS only)
    Just Works without re-implementing every method.

    All methods are instance methods (no class-level state assumptions);
    backends may hold connections / clients in __init__.
    """

    capabilities: Capabilities = Capabilities.NONE

    def list_sessions(self) -> list[Session]:
        """Return all controlled sessions visible to this backend."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support LIST"
        )

    def send_keys(
        self,
        name: str,
        keys: str | list[str],
        *,
        enter: bool = True,
        literal: bool = False,
    ) -> None:
        """Send keystrokes / text to the named session.

        Args:
            name: session name.
            keys: single string or list of tokens. Backends may interpret
                special tokens (e.g. tmux: 'Enter', 'C-c').
            enter: append Enter after the input when True (default).
            literal: send keys uninterpreted, no special-key translation.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support SEND_KEYS"
        )

    def capture(self, name: str, *, scrollback: int = 0) -> str:
        """Capture pane contents as text.

        Args:
            name: session name.
            scrollback: lines of history to include before the visible
                area. 0 = visible only. Backend may cap at available
                history depth.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support SCREEN_SCRAPE"
        )

    def spawn(
        self,
        name: str,
        command: str | list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> Session:
        """Create a new session.

        Args:
            name: session name; must be unique within the backend.
            command: initial command to run. None = login shell.
            cwd: working directory for the spawned process.
            env: env overrides (merged on top of backend defaults).
            cols/rows: initial terminal dimensions.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support SPAWN"
        )

    def kill(self, name: str) -> None:
        """Destroy the named session. Idempotent: missing session is no-op."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support KILL"
        )

    def resize(self, name: str, cols: int, rows: int) -> None:
        """Resize the named session's terminal."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support RESIZE"
        )

    def attach_command(self, name: str) -> list[str]:
        """Return argv to attach to the session interactively.

        Caller execs it themselves -- attaching from a library would
        replace the calling process.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support ATTACH"
        )

    def supports(self, *caps: Capabilities) -> bool:
        """Convenience: True if all `caps` are present in self.capabilities."""
        required = Capabilities.NONE
        for c in caps:
            required |= c
        return (self.capabilities & required) == required
