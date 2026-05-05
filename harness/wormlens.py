#!/usr/bin/env python3
"""wormlens outer loop -- infinite session continuity for Claude Code.

Manages CC lifecycle: launch, monitor for handoff signal, restart with
episodic recall. Context tracking handled by wl-hook.py (StatusLine +
UserPromptSubmit hooks). Extract happens at START of next session via
`wl --recall`, not at handoff time.

Usage:
    python3 harness_rewrite.py
    python3 harness_rewrite.py --prompt "build a redis server"
    python3 harness_rewrite.py --ctx-limit 90 --hard-kill 99
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude"
WL_STATE_DIR = CLAUDE_DIR / ".wormlens"
HANDOFF_MARKER = WL_STATE_DIR / ".handoff"
SESSION_LOG = WL_STATE_DIR / "sessions.jsonl"
LOG_FILE = WL_STATE_DIR / "harness.log"
CLAUDE_JSON = CLAUDE_DIR / ".claude.json"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_ctx_pct(session_id: str) -> float | None:
    ctx_file = CLAUDE_DIR / "sessions" / session_id / "ctx.json"
    try:
        data = json.loads(ctx_file.read_text())
        return data.get("context_window", {}).get("used_percentage")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def record_session(session_id: str, session_num: int, reason: str):
    entry = {
        "session_id": session_id,
        "session_num": session_num,
        "end_reason": reason,
        "timestamp": datetime.now().isoformat(),
    }
    try:
        with open(SESSION_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def get_last_session_id() -> str | None:
    try:
        lines = SESSION_LOG.read_text().strip().splitlines()
        if lines:
            return json.loads(lines[-1]).get("session_id")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def ensure_trust_dialog(project_dir: str):
    """Set hasTrustDialogAccepted for the project dir in .claude.json."""
    try:
        data = json.loads(CLAUDE_JSON.read_text()) if CLAUDE_JSON.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    projects = data.setdefault("projects", {})
    project = projects.setdefault(project_dir, {})

    if not project.get("hasTrustDialogAccepted"):
        project["hasTrustDialogAccepted"] = True
        tmp = CLAUDE_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(CLAUDE_JSON)
        log(f"Trust dialog accepted for {project_dir}")


def build_boot_prompt(
    session_num: int,
    prev_session_id: str | None,
    task_prompt: str | None,
) -> str:
    parts = []

    parts.append(f"wormlens session #{session_num}. Continuity is active.")

    if prev_session_id:
        parts.append(
            f"\nRecover context from your previous session by running:\n"
            f"  wl --recall --session {prev_session_id}\n"
            f"Read the output, then continue where you left off. "
            f"Do NOT ask the user to repeat anything."
        )

    parts.append(
        f"\nContext protocol:"
        f"\n- system-reminders inject context_used_pct and context_remaining_pct every turn (authoritative)"
        f"\n- time and DOW in system-reminders are authoritative -- never guess"
        f"\n- When context_remaining_pct drops below 15%, initiate handoff:"
        f"\n  1. Finish current operation cleanly"
        f"\n  2. Write a <wl-summary>session description</wl-summary> in your response"
        f"\n  3. Run: wl --handoff --session <your-session-id>"
        f"\n  4. Tell the user you are handing off"
        f"\n- If you see URGENT in system-reminders, comply immediately"
        f"\n- The outer loop restarts you with full recall via wl --recall"
        f"\n- Do NOT wait until context is critical. Hand off proactively."
    )

    if task_prompt and session_num == 1:
        parts.append(f"\nTASK: {task_prompt}")

    return "\n".join(parts)


def launch_claude(boot_prompt: str, session_id: str) -> subprocess.Popen:
    cmd = [
        "claude",
        "--session-id", session_id,
        "--dangerously-skip-permissions",
        boot_prompt,
    ]
    log(f"Launching: claude --session-id {session_id[:8]}...")
    return subprocess.Popen(cmd)


def wait_for_exit_or_handoff(
    proc: subprocess.Popen,
    session_id: str,
    ctx_limit_pct: int,
    hard_kill_pct: int,
    poll_interval: float = 2.0,
    grace_period: float = 60.0,
) -> str:
    """Wait for CC to exit, agent handoff, or forced ctx limit.

    Returns reason: 'exit', 'handoff', 'forced', or 'hard_kill'.
    """
    urgent_since: float | None = None

    while True:
        rc = proc.poll()
        if rc is not None:
            log(f"Claude exited (rc={rc})")
            return "exit"

        if HANDOFF_MARKER.exists():
            log("Handoff signal from agent")
            return "handoff"

        pct = get_ctx_pct(session_id)
        if pct is not None:
            if pct >= hard_kill_pct:
                log(f"Context at {pct}% >= {hard_kill_pct}% -- HARD KILL")
                return "hard_kill"

            if pct >= ctx_limit_pct:
                if urgent_since is None:
                    urgent_since = time.time()
                    log(f"Context at {pct}% >= {ctx_limit_pct}% -- "
                        f"hook will inject URGENT, grace period {grace_period}s")
                elif time.time() - urgent_since > grace_period:
                    log(f"Grace period expired ({grace_period}s), forcing handoff")
                    return "forced"
            else:
                urgent_since = None

        time.sleep(poll_interval)


def kill_claude(proc: subprocess.Popen):
    if proc.poll() is None:
        log("Sending SIGTERM to Claude")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log("SIGTERM timeout, sending SIGKILL")
            proc.kill()
            proc.wait()
    log("Claude stopped")


def main():
    parser = argparse.ArgumentParser(
        description="wormlens outer loop -- infinite session continuity"
    )
    parser.add_argument("--prompt", default=None, help="Initial task prompt")
    parser.add_argument(
        "--ctx-limit", type=int, default=90,
        help="Context used %% at which hook injects URGENT (default: 90)"
    )
    parser.add_argument(
        "--hard-kill", type=int, default=99,
        help="Context used %% at which to force kill (default: 99)"
    )
    parser.add_argument(
        "--grace", type=float, default=60.0,
        help="Seconds to wait after URGENT before forced handoff (default: 60)"
    )
    parser.add_argument(
        "--poll-interval", type=float, default=2.0,
        help="Poll interval in seconds (default: 2.0)"
    )
    parser.add_argument(
        "--project-dir", default=None,
        help="Project directory for trust dialog (default: cwd)"
    )
    args = parser.parse_args()

    WL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    HANDOFF_MARKER.unlink(missing_ok=True)

    project_dir = args.project_dir or os.getcwd()
    ensure_trust_dialog(project_dir)

    log("=" * 50)
    log("wormlens outer loop starting")
    log(f"ctx limit: {args.ctx_limit}% (URGENT), hard kill: {args.hard_kill}%")
    log(f"grace period: {args.grace}s")
    log("=" * 50)

    prev_session_id = get_last_session_id()
    session_num = 0

    try:
        session_num = len(SESSION_LOG.read_text().strip().splitlines())
    except (FileNotFoundError, OSError):
        pass

    while True:
        session_num += 1
        session_id = str(uuid.uuid4())
        HANDOFF_MARKER.unlink(missing_ok=True)

        log(f"--- SESSION #{session_num} ({session_id[:8]}) ---")

        boot = build_boot_prompt(session_num, prev_session_id, args.prompt)
        proc = launch_claude(boot, session_id)

        reason = wait_for_exit_or_handoff(
            proc, session_id,
            args.ctx_limit, args.hard_kill,
            args.poll_interval, args.grace,
        )

        if reason in ("handoff", "forced", "hard_kill"):
            kill_claude(proc)

        record_session(session_id, session_num, reason)
        prev_session_id = session_id

        if reason == "exit":
            log("User exited. Stopping outer loop.")
            break

        if reason == "hard_kill":
            log("HARD KILL -- agent did not comply. Restarting anyway.")

        log(f"Restarting in 3s (reason: {reason})...")
        time.sleep(3)

    log("wormlens outer loop stopped")


if __name__ == "__main__":
    main()
