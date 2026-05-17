"""Tmux backend for wormlens.terminal.

Implements the full TerminalControl surface against libtmux. Lifted
in spirit from spad-mcp's adapters/tmux.py (Christopher M. Mapes /
apresence on GitHub, MIT) -- this is the primitives subset, with
the MCP-tool registration and event-orchestration layer stripped.
Those belong in the consumer (spad-mcp keeps its own orchestration
on top of this).

Optional dependency: libtmux. Install via the [tmux] extra:

    pip install 'wormlens[tmux]'
"""

from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING

from ._base import Capabilities, Session, TerminalControl

if TYPE_CHECKING:
    import libtmux  # noqa: F401


class TmuxError(RuntimeError):
    pass


def _require_libtmux():
    try:
        import libtmux  # noqa: F401
        return libtmux
    except ImportError as e:
        raise TmuxError(
            "wormlens.terminal.tmux requires the `libtmux` package. "
            "Install with: pip install 'wormlens[tmux]'"
        ) from e


def _session_pid(s) -> int | None:
    """Best-effort pid of the session's first pane."""
    try:
        pane = s.active_pane or (s.windows[0].panes[0] if s.windows else None)
        if pane is None:
            return None
        v = pane.cmd("display-message", "-p", "#{pane_pid}").stdout
        if v:
            return int(v[0])
    except Exception:
        return None
    return None


def _session_to_handle(s) -> Session:
    pid = _session_pid(s)
    cwd = None
    cols = None
    rows = None
    try:
        # session-level dimensions = the dimensions of the first window
        win = s.windows[0] if s.windows else None
        if win is not None:
            v = win.cmd("display-message", "-p", "#{window_width} #{window_height}").stdout
            if v:
                parts = v[0].split()
                if len(parts) == 2:
                    cols, rows = int(parts[0]), int(parts[1])
        pane = s.active_pane or (s.windows[0].panes[0] if s.windows else None)
        if pane is not None:
            v = pane.cmd("display-message", "-p", "#{pane_current_path}").stdout
            if v:
                cwd = v[0]
    except Exception:
        pass
    return Session(
        name=s.name,
        pid=pid,
        cwd=cwd,
        cols=cols,
        rows=rows,
        metadata={"id": getattr(s, "id", "")},
    )


class TmuxControl(TerminalControl):
    """Tmux-backed terminal control.

    Targets a single tmux server (default: the user's). For a non-default
    server, pass `socket_name` or `socket_path`.
    """

    capabilities = (
        Capabilities.SEND_KEYS
        | Capabilities.SCREEN_SCRAPE
        | Capabilities.SCROLLBACK
        | Capabilities.RESIZE
        | Capabilities.SPAWN
        | Capabilities.KILL
        | Capabilities.LIST
        | Capabilities.ATTACH
    )

    def __init__(
        self,
        *,
        socket_name: str | None = None,
        socket_path: str | None = None,
    ):
        libtmux = _require_libtmux()
        kwargs = {}
        if socket_name:
            kwargs["socket_name"] = socket_name
        if socket_path:
            kwargs["socket_path"] = socket_path
        self._socket_name = socket_name
        self._socket_path = socket_path
        self._server = libtmux.Server(**kwargs)

    # ---- LIST -------------------------------------------------------------

    def list_sessions(self) -> list[Session]:
        try:
            sessions = self._server.sessions
        except Exception as e:
            raise TmuxError(f"tmux list failed: {e}") from e
        return [_session_to_handle(s) for s in sessions]

    def _find(self, name: str):
        for s in self._server.sessions:
            if s.name == name:
                return s
        return None

    # ---- SEND_KEYS --------------------------------------------------------

    def send_keys(
        self,
        name: str,
        keys: str | list[str],
        *,
        enter: bool = True,
        literal: bool = False,
    ) -> None:
        s = self._find(name)
        if s is None:
            raise TmuxError(f"no such session: {name}")
        pane = s.active_pane or (s.windows[0].panes[0] if s.windows else None)
        if pane is None:
            raise TmuxError(f"session has no pane: {name}")
        if isinstance(keys, str):
            keys_list = [keys]
        else:
            keys_list = list(keys)
        for k in keys_list:
            if literal:
                pane.send_keys(k, enter=False, literal=True)
            else:
                pane.send_keys(k, enter=False)
        if enter:
            pane.send_keys("", enter=True, literal=True)

    # ---- SCREEN_SCRAPE / SCROLLBACK --------------------------------------

    def capture(self, name: str, *, scrollback: int = 0) -> str:
        s = self._find(name)
        if s is None:
            raise TmuxError(f"no such session: {name}")
        pane = s.active_pane or (s.windows[0].panes[0] if s.windows else None)
        if pane is None:
            raise TmuxError(f"session has no pane: {name}")
        if scrollback > 0:
            lines = pane.capture_pane(start=-scrollback, end="-")
        else:
            lines = pane.capture_pane()
        if isinstance(lines, list):
            return "\n".join(lines)
        return str(lines)

    # ---- SPAWN ------------------------------------------------------------

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
        if self._find(name) is not None:
            raise TmuxError(f"session already exists: {name}")
        if isinstance(command, list):
            cmd_str = " ".join(shlex.quote(c) for c in command)
        else:
            cmd_str = command  # may be None for login shell

        kwargs = {
            "session_name": name,
            "kill_session": False,
            "attach": False,
            "x": cols,
            "y": rows,
        }
        if cwd:
            kwargs["start_directory"] = cwd
        if cmd_str:
            kwargs["window_command"] = cmd_str

        if env:
            saved = {}
            for k, v in env.items():
                saved[k] = os.environ.get(k)
                os.environ[k] = v
            try:
                s = self._server.new_session(**kwargs)
            finally:
                for k, prev in saved.items():
                    if prev is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = prev
        else:
            s = self._server.new_session(**kwargs)
        return _session_to_handle(s)

    # ---- KILL -------------------------------------------------------------

    def kill(self, name: str) -> None:
        s = self._find(name)
        if s is None:
            return  # idempotent
        try:
            s.kill()
        except Exception as e:
            raise TmuxError(f"tmux kill failed for {name}: {e}") from e

    # ---- RESIZE -----------------------------------------------------------

    def resize(self, name: str, cols: int, rows: int) -> None:
        s = self._find(name)
        if s is None:
            raise TmuxError(f"no such session: {name}")
        win = s.windows[0] if s.windows else None
        if win is None:
            raise TmuxError(f"session has no window: {name}")
        try:
            win.cmd("resize-window", "-x", str(cols), "-y", str(rows))
        except Exception as e:
            raise TmuxError(f"tmux resize failed for {name}: {e}") from e

    # ---- ATTACH -----------------------------------------------------------

    def attach_command(self, name: str) -> list[str]:
        argv = ["tmux"]
        if self._socket_name:
            argv += ["-L", self._socket_name]
        if self._socket_path:
            argv += ["-S", self._socket_path]
        argv += ["attach", "-t", name]
        return argv
