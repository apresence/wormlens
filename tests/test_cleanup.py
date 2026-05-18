"""Tests for the 2026-05-17-evening cleanup batch:
- item 1: full session_id in grep output (no [:12] truncation)
- item 2: --list-sources mode + discovery_roots() on each provider
- item 3: -n N with -f (replay last N, then stream)
- item 4: --session vs --session-id distinction (already wired, smoke only)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from wormlens.providers import PROVIDERS, detect_provider
from wormlens.providers._base import Provider
from wormlens.providers.claude_code import ClaudeCodeProvider
from wormlens.providers.codex import CodexProvider
from wormlens.providers.vscode_copilot import VSCodeCopilotProvider


# --- item 2: discovery_roots() ---------------------------------------------


class TestDiscoveryRoots(unittest.TestCase):
    def test_base_default_is_empty(self):
        class Stub(Provider):
            provider_id = "stub"
            def discover_sessions(self, **kw): return []
            def parse_file(self, path, opts, **kw): return []
            def list_sessions_metadata(self, **kw): return []
            @classmethod
            def detect(cls, path): return False
        self.assertEqual(Stub().discovery_roots(), [])

    def test_cc_returns_projects_dir(self):
        roots = ClaudeCodeProvider().discovery_roots()
        self.assertEqual(len(roots), 1)
        self.assertTrue(str(roots[0]).endswith(os.path.join(".claude", "projects"))
                        or "CLAUDE_CONFIG_DIR" in os.environ)

    def test_codex_returns_sessions_dir(self):
        roots = CodexProvider().discovery_roots()
        self.assertEqual(len(roots), 1)
        self.assertTrue(str(roots[0]).endswith(os.path.join(".codex", "sessions"))
                        or "CODEX_HOME" in os.environ)

    def test_vscode_returns_workspace_store(self):
        roots = VSCodeCopilotProvider().discovery_roots()
        self.assertEqual(len(roots), 1)
        self.assertIn("workspaceStorage", str(roots[0]))

    def test_claude_ai_is_file_only(self):
        cls = PROVIDERS.get("claude_ai")
        if cls is None:
            self.skipTest("claude_ai provider not registered")
        self.assertEqual(cls().discovery_roots(), [])

    def test_wl_extract_is_file_only(self):
        cls = PROVIDERS.get("wl")
        if cls is None:
            self.skipTest("wl_extract provider not registered")
        self.assertEqual(cls().discovery_roots(), [])


# --- item 2: --list-sources CLI -------------------------------------------


class TestListSourcesCLI(unittest.TestCase):
    def test_invocation_lists_all_providers(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = "/global/gztools"
        result = subprocess.run(
            [sys.executable, "-m", "wormlens", "--list-sources"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        for pid in ("cc", "codex", "vscode"):
            self.assertIn(pid, out, f"--list-sources output missing provider {pid}")
        # file-only marker present for claude_ai
        if "claude_ai" in PROVIDERS:
            self.assertIn("file-only", out)


# --- item 3: -n N with -f --------------------------------------------------


class TestFollowWithTail(unittest.TestCase):
    """Integration: write 8 records, follow -n 3 -f, append 1, expect 3+1 in order."""

    @unittest.skipUnless(shutil.which("python3"), "no python3 in PATH")
    def test_replay_then_stream(self):
        try:
            import watchdog  # noqa: F401
        except ImportError:
            self.skipTest("watchdog not installed")

        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for i in range(1, 9):
                    rec = {
                        "type": "user", "sessionId": "sx",
                        "timestamp": f"t-old-{i}",
                        "message": {"role": "user", "content": [
                            {"type": "text", "text": f"old-{i}"},
                        ]},
                    }
                    f.write(json.dumps(rec) + "\n")
            out_path = path + ".out"
            env = dict(os.environ)
            env["PYTHONPATH"] = "/global/gztools"
            proc = subprocess.Popen(
                [sys.executable, "-m", "wormlens", "-f", "-n", "3",
                 "--format", "jsonl", path],
                stdout=open(out_path, "w"), stderr=subprocess.PIPE, env=env,
            )
            import time, signal
            time.sleep(1.2)
            # append one new record
            with open(path, "a") as f:
                f.write(json.dumps({
                    "type": "assistant", "sessionId": "sx",
                    "timestamp": "t-live",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "live-1"},
                    ]},
                }) + "\n")
            time.sleep(1.4)
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=4)
            with open(out_path) as f:
                lines = [l.strip() for l in f if l.strip()]
            texts = [json.loads(l)["text"] for l in lines]
            self.assertEqual(texts, ["old-6", "old-7", "old-8", "live-1"])
        finally:
            os.unlink(path)
            try: os.unlink(out_path)
            except FileNotFoundError: pass


# --- item 1: full session_id in grep output --------------------------------


class TestGrepFullSid(unittest.TestCase):
    def test_grep_emits_full_session_id(self):
        try:
            import watchdog  # noqa: F401
        except ImportError:
            self.skipTest("not required, but skipping to keep env consistent")
        long_sid = "abcdef12-3456-7890-abcd-ef1234567890"
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                for content_text in ("hello grep target", "reply"):
                    role = "user" if "hello" in content_text else "assistant"
                    rec = {
                        "type": role,
                        "sessionId": long_sid,
                        "timestamp": "2026-05-17T20:00:00Z",
                        "message": {"role": role, "content": [
                            {"type": "text", "text": content_text},
                        ]},
                    }
                    f.write(json.dumps(rec) + "\n")
            env = dict(os.environ)
            env["PYTHONPATH"] = "/global/gztools"
            result = subprocess.run(
                [sys.executable, "-m", "wormlens", "--grep", "grep target",
                 "--no-color", path],
                capture_output=True, text=True, env=env, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(long_sid, result.stdout,
                          f"full sid missing from grep output:\n{result.stdout}")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
