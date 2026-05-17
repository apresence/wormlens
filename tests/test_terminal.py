"""Tests for wormlens.terminal -- base + tmux backend.

Tmux integration tests skip if libtmux + tmux binary aren't both available.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import unittest

from wormlens.terminal import Capabilities, Session, TerminalControl


class TestCapabilities(unittest.TestCase):
    def test_flag_arithmetic(self):
        c = Capabilities.SEND_KEYS | Capabilities.LIST
        self.assertTrue(c & Capabilities.SEND_KEYS)
        self.assertTrue(c & Capabilities.LIST)
        self.assertFalse(c & Capabilities.KILL)

    def test_supports_single(self):
        class C(TerminalControl):
            capabilities = Capabilities.SEND_KEYS
        self.assertTrue(C().supports(Capabilities.SEND_KEYS))
        self.assertFalse(C().supports(Capabilities.KILL))

    def test_supports_multi(self):
        class C(TerminalControl):
            capabilities = Capabilities.SEND_KEYS | Capabilities.LIST
        c = C()
        self.assertTrue(c.supports(Capabilities.SEND_KEYS, Capabilities.LIST))
        self.assertFalse(c.supports(Capabilities.SEND_KEYS, Capabilities.KILL))


class TestBaseDefaults(unittest.TestCase):
    """Default methods on TerminalControl should raise NotImplementedError."""

    def test_each_method_raises_by_default(self):
        c = TerminalControl()
        with self.assertRaises(NotImplementedError):
            c.list_sessions()
        with self.assertRaises(NotImplementedError):
            c.send_keys("x", "y")
        with self.assertRaises(NotImplementedError):
            c.capture("x")
        with self.assertRaises(NotImplementedError):
            c.spawn("x")
        with self.assertRaises(NotImplementedError):
            c.kill("x")
        with self.assertRaises(NotImplementedError):
            c.resize("x", 80, 24)
        with self.assertRaises(NotImplementedError):
            c.attach_command("x")


def _tmux_available():
    if shutil.which("tmux") is None:
        return False
    try:
        import libtmux  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_tmux_available(), "tmux + libtmux not both installed")
class TestTmuxIntegration(unittest.TestCase):
    """Real tmux round-trips on a transient socket so we don't touch user sessions."""

    @classmethod
    def setUpClass(cls):
        import tempfile, os
        cls._sock_dir = tempfile.mkdtemp(prefix="wlterm-test-")
        cls._socket_path = os.path.join(cls._sock_dir, "sock")

    @classmethod
    def tearDownClass(cls):
        # Kill the test server, remove socket dir.
        subprocess.run(
            ["tmux", "-S", cls._socket_path, "kill-server"],
            stderr=subprocess.DEVNULL,
            check=False,
        )
        import shutil as _sh
        _sh.rmtree(cls._sock_dir, ignore_errors=True)

    def _control(self):
        from wormlens.terminal.tmux import TmuxControl
        return TmuxControl(socket_path=self._socket_path)

    def test_capabilities_full(self):
        c = self._control()
        for cap in (
            Capabilities.SEND_KEYS, Capabilities.SCREEN_SCRAPE,
            Capabilities.SCROLLBACK, Capabilities.RESIZE,
            Capabilities.SPAWN, Capabilities.KILL,
            Capabilities.LIST, Capabilities.ATTACH,
        ):
            self.assertTrue(c.supports(cap), f"missing cap: {cap}")

    def test_spawn_list_kill(self):
        c = self._control()
        NAME = f"wltest-spawn-{int(time.time()*1000)}"
        s = c.spawn(NAME, cwd="/tmp")
        self.assertEqual(s.name, NAME)
        self.assertIsNotNone(s.pid)
        sessions = c.list_sessions()
        self.assertIn(NAME, [x.name for x in sessions])
        c.kill(NAME)
        self.assertNotIn(NAME, [x.name for x in c.list_sessions()])

    def test_kill_is_idempotent(self):
        c = self._control()
        # killing nonexistent should not raise
        c.kill("does-not-exist-anywhere")

    def test_send_keys_visible_in_capture(self):
        c = self._control()
        NAME = f"wltest-send-{int(time.time()*1000)}"
        c.spawn(NAME, cwd="/tmp")
        try:
            time.sleep(0.3)
            sentinel = f"wormlens-tag-{NAME[-6:]}"
            c.send_keys(NAME, f"echo {sentinel}")
            time.sleep(0.5)
            cap = c.capture(NAME)
            self.assertIn(sentinel, cap)
        finally:
            c.kill(NAME)

    def test_resize_updates_dimensions(self):
        c = self._control()
        NAME = f"wltest-resize-{int(time.time()*1000)}"
        c.spawn(NAME, cwd="/tmp", cols=80, rows=24)
        try:
            c.resize(NAME, 120, 30)
            for s in c.list_sessions():
                if s.name == NAME:
                    self.assertEqual(s.cols, 120)
                    self.assertEqual(s.rows, 30)
                    return
            self.fail("session not found after resize")
        finally:
            c.kill(NAME)

    def test_capture_scrollback(self):
        c = self._control()
        NAME = f"wltest-scroll-{int(time.time()*1000)}"
        c.spawn(NAME, cwd="/tmp")
        try:
            time.sleep(0.3)
            # Push lots of lines so we have scrollback worth capturing
            for i in range(60):
                c.send_keys(NAME, f"echo line{i}")
                # send_keys hits Enter -- no need to wait between every one
            time.sleep(1.2)
            visible = c.capture(NAME)
            full = c.capture(NAME, scrollback=200)
            self.assertGreaterEqual(len(full.splitlines()), len(visible.splitlines()))
            self.assertIn("line0", full)  # earliest line should be in scrollback
        finally:
            c.kill(NAME)

    def test_attach_command_uses_socket(self):
        c = self._control()
        cmd = c.attach_command("xname")
        self.assertEqual(cmd[0], "tmux")
        self.assertIn("-S", cmd)
        self.assertIn(self._socket_path, cmd)
        self.assertIn("attach", cmd)
        self.assertIn("xname", cmd)

    def test_spawn_duplicate_errors(self):
        from wormlens.terminal.tmux import TmuxError
        c = self._control()
        NAME = f"wltest-dup-{int(time.time()*1000)}"
        c.spawn(NAME, cwd="/tmp")
        try:
            with self.assertRaises(TmuxError):
                c.spawn(NAME, cwd="/tmp")
        finally:
            c.kill(NAME)

    def test_send_keys_missing_session_errors(self):
        from wormlens.terminal.tmux import TmuxError
        c = self._control()
        with self.assertRaises(TmuxError):
            c.send_keys("ghost-session-name", "echo no")


if __name__ == "__main__":
    unittest.main()
