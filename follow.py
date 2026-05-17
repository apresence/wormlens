"""Live tail of agent transcript JSONLs.

Streams new records from one or more transcript files as they are appended.
Drives the per-provider `parse_line` shim added to each provider for this
purpose. Used by `wl -f` and intended as the streaming primitive for
downstream consumers (spad-mcp, memav, cortex).

Optional dependency: `watchdog` (cross-platform file-event backend).
Install via the `[follow]` extra:

    pip install wormlens[follow]

Design adapted from spad-mcp's core/watcher.py (Christopher M. Mapes,
apresence on GitHub, MIT) -- watchdog-backed reimplementation, not a copy.
"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path
from typing import Callable, Iterable

from .models import ChatMessage, FilterOpts
from .providers import PROVIDERS, detect_provider


# --- public types -----------------------------------------------------------

OnRecord = Callable[[ChatMessage, str], None]
"""Callback invoked per parsed ChatMessage. Args: (message, source_path)."""


class FollowError(RuntimeError):
    pass


# --- watchdog soft import ---------------------------------------------------

def _require_watchdog():
    try:
        from watchdog.observers import Observer  # noqa: F401
        from watchdog.events import FileSystemEventHandler  # noqa: F401
    except ImportError as e:
        raise FollowError(
            "wormlens.follow requires the `watchdog` package. "
            "Install with: pip install 'wormlens[follow]'"
        ) from e


# --- per-file tail state ----------------------------------------------------


class _Tail:
    """Per-file cursor + leftover-bytes buffer + provider parse state."""

    def __init__(self, path: str, provider, opts: FilterOpts, on_record: OnRecord):
        self.path = path
        self.provider = provider
        self.opts = opts
        self.on_record = on_record
        self.pos: int = 0
        self._buf: str = ""
        # provider-specific state dict (e.g. bash_ids for CC, session meta for codex)
        self.state: dict = {}
        self._attach()

    def _attach(self):
        """Seek to EOF so we don't replay history on initial bind."""
        try:
            self.pos = os.path.getsize(self.path)
        except OSError:
            self.pos = 0

    def process(self):
        """Read new bytes since last position; parse complete lines."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return

        # Truncation: file shrunk -> reset
        if size < self.pos:
            self.pos = 0
            self._buf = ""

        if size == self.pos:
            return

        try:
            with open(self.path, "r", errors="replace") as fh:
                fh.seek(self.pos)
                chunk = fh.read(size - self.pos)
                self.pos = fh.tell()
        except OSError:
            return

        # Line-oriented: hold back trailing partial line
        raw = self._buf + chunk
        lines = raw.split("\n")
        self._buf = lines[-1]
        complete = lines[:-1]

        for line in complete:
            if not line.strip():
                continue
            try:
                msgs = self.provider.parse_line(line, self.opts, self.state)
            except Exception:
                # never let one bad line kill the stream
                continue
            for m in msgs:
                if not m.source_file:
                    m.source_file = self.path
                try:
                    self.on_record(m, self.path)
                except Exception:
                    # caller's callback raised -- swallow so the stream survives
                    continue

    def reset_after_recreate(self):
        """Called when the file was deleted/moved and reappeared."""
        self.pos = 0
        self._buf = ""
        self.state = {}


# --- watchdog handler -------------------------------------------------------


def _build_handler(tails: dict, stop: threading.Event):
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if event.is_directory:
                return
            t = tails.get(os.path.realpath(event.src_path))
            if t is not None:
                t.process()

        def on_created(self, event):
            if event.is_directory:
                return
            t = tails.get(os.path.realpath(event.src_path))
            if t is not None:
                # file recreated; rebind from start (not EOF -- caller chose
                # to follow this path, new content from byte 0 is wanted)
                t.reset_after_recreate()
                t.process()

        def on_moved(self, event):
            if event.is_directory:
                return
            t = tails.get(os.path.realpath(event.src_path))
            if t is not None:
                t.reset_after_recreate()

    return _Handler()


# --- public API -------------------------------------------------------------


def follow(
    paths: Iterable[str],
    on_record: OnRecord,
    *,
    opts: FilterOpts | None = None,
    source: str | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Block until SIGINT (or `stop` event), streaming new records.

    Args:
        paths: transcript files to follow. Each is bound to a provider via
            auto-detection unless `source` is given.
        on_record: callback invoked per parsed ChatMessage.
        opts: filter options (defaults to FilterOpts() -- only msg, no tools).
        source: provider id to force; if None, auto-detect each file.
        stop: optional threading.Event for programmatic stop.
    """
    _require_watchdog()
    from watchdog.observers import Observer

    if opts is None:
        opts = FilterOpts()
    if stop is None:
        stop = threading.Event()

    path_list = [os.path.realpath(p) for p in paths]
    if not path_list:
        raise FollowError("follow() requires at least one path")

    tails: dict[str, _Tail] = {}
    for p in path_list:
        if not os.path.isfile(p):
            raise FollowError(f"not a file: {p}")
        if source:
            cls = PROVIDERS.get(source)
            if cls is None:
                raise FollowError(f"unknown provider: {source}")
        else:
            cls = detect_provider(Path(p))
            if cls is None:
                raise FollowError(f"could not detect provider for: {p}")
        provider = cls()
        # Sanity-check the provider supports streaming
        try:
            provider.parse_line("", opts, {})
        except NotImplementedError as e:
            raise FollowError(str(e))
        except Exception:
            pass  # parse_line tolerated other errors fine
        tails[p] = _Tail(p, provider, opts, on_record)

    # Watch each unique parent dir
    observer = Observer()
    handler = _build_handler(tails, stop)
    watched_dirs = set()
    for p in path_list:
        d = os.path.dirname(p) or "."
        if d not in watched_dirs:
            observer.schedule(handler, d, recursive=False)
            watched_dirs.add(d)

    # SIGINT handler -> set stop event
    def _on_sigint(signum, frame):
        stop.set()

    prev_handler = signal.signal(signal.SIGINT, _on_sigint)

    observer.start()
    try:
        # Initial drain: catch anything that landed after _attach() saw size
        for t in tails.values():
            t.process()
        # Block until stop
        while not stop.is_set():
            stop.wait(timeout=0.5)
            # Periodic poll as backup (watchdog can miss events on some FS)
            for t in tails.values():
                t.process()
    finally:
        observer.stop()
        observer.join(timeout=2.0)
        signal.signal(signal.SIGINT, prev_handler)
