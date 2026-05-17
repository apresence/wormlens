"""wormlens.terminal -- pluggable terminal-control backends.

Backends declare capabilities via a Flag enum; consumers query before
calling. The base class lives in _base.py; individual backends are
imported lazily (no hard dep on libtmux unless TmuxControl is touched).
"""

from ._base import Capabilities, Session, TerminalControl

__all__ = ["Capabilities", "Session", "TerminalControl"]
