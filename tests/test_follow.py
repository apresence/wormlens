"""Tests for wormlens.follow + per-provider parse_line.

Skips integration tests if watchdog isn't installed.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

from wormlens.models import ChatMessage, FilterOpts
from wormlens.providers.claude_code import ClaudeCodeProvider
from wormlens.providers.codex import CodexProvider


# --- parse_line: claude_code ------------------------------------------------


class TestParseLineCC(unittest.TestCase):
    def setUp(self):
        self.p = ClaudeCodeProvider()
        self.opts = FilterOpts()
        self.state = {}

    def test_user_message(self):
        rec = {
            "type": "user", "sessionId": "s1",
            "timestamp": "2026-05-17T00:00:00Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
        out = self.p.parse_line(json.dumps(rec), self.opts, self.state)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].role, "user")
        self.assertEqual(out[0].text, "hi")
        self.assertEqual(out[0].session_id, "s1")

    def test_assistant_message(self):
        rec = {
            "type": "assistant", "sessionId": "s1",
            "timestamp": "2026-05-17T00:00:01Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
        }
        out = self.p.parse_line(json.dumps(rec), self.opts, self.state)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].role, "assistant")

    def test_empty_line(self):
        self.assertEqual(self.p.parse_line("", self.opts, self.state), [])
        self.assertEqual(self.p.parse_line("   \n  ", self.opts, self.state), [])

    def test_unparseable_line(self):
        self.assertEqual(self.p.parse_line("not json", self.opts, self.state), [])
        self.assertEqual(self.p.parse_line("{broken", self.opts, self.state), [])

    def test_unknown_record_type(self):
        rec = {"type": "summary", "summary": "x"}
        self.assertEqual(self.p.parse_line(json.dumps(rec), self.opts, self.state), [])

    def test_bash_state_tracking(self):
        # tool_use Bash -> tool_result with matching id should both emit when --bash
        opts = FilterOpts(bash=True)
        state = {}
        tu = {
            "type": "assistant", "sessionId": "s1", "timestamp": "t",
            "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": "tu_1", "name": "Bash",
                "input": {"command": "ls"},
            }]},
        }
        out1 = self.p.parse_line(json.dumps(tu), opts, state)
        self.assertTrue(any(m.msg_type == "bash" for m in out1))
        self.assertIn("tu_1", state.get("bash_ids", set()))

        tr = {
            "type": "user", "sessionId": "s1", "timestamp": "t",
            "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "tu_1",
                "content": "file1\nfile2\n",
            }]},
        }
        out2 = self.p.parse_line(json.dumps(tr), opts, state)
        self.assertTrue(any(m.msg_type == "bash" for m in out2))


# --- parse_line: codex ------------------------------------------------------


class TestParseLineCodex(unittest.TestCase):
    def setUp(self):
        self.p = CodexProvider()
        self.opts = FilterOpts()
        self.state = {}

    def test_session_meta_populates_state(self):
        rec = {
            "type": "session_meta", "timestamp": "2026-05-17T00:00:00Z",
            "payload": {
                "id": "cdx-x", "cwd": "/tmp", "cli_version": "0.1",
                "model_provider": "openai", "originator": "cli",
            },
        }
        out = self.p.parse_line(json.dumps(rec), self.opts, self.state)
        self.assertEqual(out, [])
        self.assertEqual(self.state["session_id"], "cdx-x")
        self.assertEqual(self.state["cwd"], "/tmp")

    def test_message_uses_state_session_id(self):
        meta = {
            "type": "session_meta", "timestamp": "t0",
            "payload": {"id": "cdx-1"},
        }
        self.p.parse_line(json.dumps(meta), self.opts, self.state)
        msg = {
            "type": "response_item", "timestamp": "t1",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        }
        out = self.p.parse_line(json.dumps(msg), self.opts, self.state)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "hello")
        self.assertEqual(out[0].session_id, "cdx-1")

    def test_ignored_record_types(self):
        for rtype in ("turn_context", "event_msg"):
            rec = {"type": rtype, "timestamp": "t", "payload": {}}
            self.assertEqual(
                self.p.parse_line(json.dumps(rec), self.opts, self.state), []
            )

    def test_garbage(self):
        self.assertEqual(self.p.parse_line("not json", self.opts, self.state), [])


# --- integration: follow ---------------------------------------------------


def _watchdog_available():
    try:
        import watchdog  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_watchdog_available(), "watchdog not installed")
class TestFollow(unittest.TestCase):
    def _write(self, path, rec):
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()

    def _run_follow(self, paths, on_record, *, opts=None, run_time=1.5,
                    appender=None):
        from wormlens.follow import follow

        stop = threading.Event()

        def stopper():
            time.sleep(run_time)
            stop.set()

        threads = [threading.Thread(target=stopper)]
        if appender:
            threads.append(threading.Thread(target=appender))
        for t in threads:
            t.start()
        follow(paths, on_record, opts=opts, stop=stop)
        for t in threads:
            t.join()

    def test_streams_appended_records_cc(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        # Seed a detectable CC record before follow starts
        self._write(path, {
            "type": "user", "sessionId": "s0", "timestamp": "t",
            "message": {"role": "user", "content": [{"type": "text", "text": "seed"}]},
        })

        got = []
        on_rec = lambda m, p: got.append((m.role, m.text))

        def appender():
            time.sleep(0.4)
            self._write(path, {
                "type": "assistant", "sessionId": "s0", "timestamp": "t1",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "live one"},
                ]},
            })
            time.sleep(0.2)
            self._write(path, {
                "type": "user", "sessionId": "s0", "timestamp": "t2",
                "message": {"role": "user", "content": [
                    {"type": "text", "text": "live two"},
                ]},
            })

        try:
            self._run_follow([path], on_rec, run_time=1.5, appender=appender)
        finally:
            os.unlink(path)

        # Should have received exactly the two appended records (not the seed).
        self.assertEqual(
            got, [("assistant", "live one"), ("user", "live two")]
        )

    def test_partial_line_buffered(self):
        """A line written in two chunks should not parse until newline arrives."""
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self._write(path, {
            "type": "user", "sessionId": "s0", "timestamp": "t",
            "message": {"role": "user", "content": [{"type": "text", "text": "seed"}]},
        })

        got = []
        on_rec = lambda m, p: got.append((m.role, m.text))

        rec = {
            "type": "user", "sessionId": "s0", "timestamp": "t1",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "split-line"},
            ]},
        }
        raw = json.dumps(rec)
        half_a, half_b = raw[: len(raw) // 2], raw[len(raw) // 2:]

        def appender():
            time.sleep(0.4)
            with open(path, "a") as f:
                f.write(half_a)
                f.flush()
            time.sleep(0.3)
            with open(path, "a") as f:
                f.write(half_b + "\n")
                f.flush()

        try:
            self._run_follow([path], on_rec, run_time=1.6, appender=appender)
        finally:
            os.unlink(path)

        self.assertEqual(got, [("user", "split-line")])

    def test_empty_paths_errors(self):
        from wormlens.follow import follow, FollowError
        with self.assertRaises(FollowError):
            follow([], lambda m, p: None)

    def test_nonfile_path_errors(self):
        from wormlens.follow import follow, FollowError
        with self.assertRaises(FollowError):
            follow(["/does/not/exist/file.jsonl"], lambda m, p: None)


if __name__ == "__main__":
    unittest.main()
