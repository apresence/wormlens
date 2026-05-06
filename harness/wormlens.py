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
import errno
import fcntl
import json
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
import tty
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
    # Use \r\n so log lines render correctly when stdin is in raw mode
    # (during the pty bridge in wait_for_exit_or_handoff).
    try:
        sys.stderr.write(line + "\r\n")
        sys.stderr.flush()
    except OSError:
        pass
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


def _copy_winsize(src_fd: int, dst_fd: int):
    """Copy terminal window size from src_fd to dst_fd."""
    try:
        s = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, s)
    except OSError:
        pass


def launch_claude(session_id: str):
    """Launch claude in interactive mode under a fresh pty.

    Returns (proc, master_fd). The boot prompt is NOT passed positionally --
    in CC v2.1.116 a positional prompt forces --print/sdk-cli (one-shot)
    mode, which exits in seconds and never fires StatusLine. The harness
    delivers the boot prompt by writing to the master end of the pty after
    CC's interactive UI is ready (signalled by ctx.json appearing).
    """
    master_fd, slave_fd = pty.openpty()
    if sys.stdin.isatty():
        _copy_winsize(sys.stdin.fileno(), slave_fd)

    cmd = [
        "claude",
        "--session-id", session_id,
        "--dangerously-skip-permissions",
    ]
    log(f"Launching (interactive pty): claude --session-id {session_id[:8]}...")
    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    # non-blocking master so drain reads do not stall on EOF
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return proc, master_fd


def _flatten_boot_prompt(text: str) -> bytes:
    """Collapse multi-line boot prompt to a single line.

    CC's TUI treats multi-line input as fragile (depends on Shift+Enter
    handling). The boot text is bullet-style, so collapsing newlines to
    spaces keeps it readable. The trailing submit (CR) is sent separately
    by the caller after a small delay -- if text and CR arrive in the same
    write, Ink-based TUIs (like CC's) treat the whole thing as a paste and
    do NOT fire an Enter key event.
    """
    flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat.encode("utf-8", "replace")


def wait_for_exit_or_handoff(
    proc: subprocess.Popen,
    master_fd: int,
    session_id: str,
    boot_prompt: str,
    ctx_limit_pct: int,
    hard_kill_pct: int,
    poll_interval: float = 2.0,
    grace_period: float = 60.0,
    boot_max_wait: float = 8.0,
    fast_exit_secs: float = 10.0,
) -> str:
    """Bridge stdio to CC's pty and supervise the session.

    Auto-injects boot_prompt onto the master fd once ctx.json shows up
    (CC's TUI is ready), or after boot_max_wait seconds elapse with no
    ctx.json (fallback so misconfig does not silently swallow the boot).

    Returns: 'exit', 'handoff', 'forced', 'hard_kill', or 'fast_exit'.
    'fast_exit' = CC died quickly with rc=0 and never wrote ctx.json
    (likely launched in --print/sdk-cli mode by mistake).
    """
    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old_attrs = None
    if stdin_fd is not None:
        try:
            old_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        except (termios.error, OSError):
            old_attrs = None

    def _on_winch(signum, frame):
        if stdin_fd is not None:
            _copy_winsize(stdin_fd, master_fd)

    prev_winch = signal.signal(signal.SIGWINCH, _on_winch)

    ctx_path = CLAUDE_DIR / "sessions" / session_id / "ctx.json"
    boot_sent = not bool(boot_prompt)
    boot_submit_at: float | None = None  # when to send the trailing CR
    submit_delay = 0.5
    ever_saw_ctx = False
    urgent_since: float | None = None
    start = time.time()
    last_ctx_poll = 0.0

    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                # drain whatever output remains on master_fd
                while True:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    try:
                        os.write(sys.stdout.fileno(), data)
                    except OSError:
                        break
                elapsed = time.time() - start
                log(f"Claude exited (rc={rc}, elapsed={elapsed:.1f}s)")
                if (rc == 0 and elapsed < fast_exit_secs and not ever_saw_ctx):
                    return "fast_exit"
                return "exit"

            if HANDOFF_MARKER.exists():
                log("Handoff signal from agent")
                return "handoff"

            read_fds = [master_fd]
            if stdin_fd is not None:
                read_fds.append(stdin_fd)
            try:
                r, _, _ = select.select(read_fds, [], [], 0.1)
            except (OSError, select.error) as e:
                if getattr(e, "errno", None) == errno.EINTR:
                    continue
                r = []

            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                    if data:
                        os.write(sys.stdout.fileno(), data)
                except OSError:
                    pass

            if stdin_fd is not None and stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 4096)
                    if data:
                        os.write(master_fd, data)
                except OSError:
                    pass

            now = time.time()
            if now - last_ctx_poll < poll_interval:
                continue
            last_ctx_poll = now

            if not ever_saw_ctx and ctx_path.exists():
                ever_saw_ctx = True

            if not boot_sent:
                if ever_saw_ctx:
                    log("CC interactive UI ready -- injecting boot prompt")
                    boot_sent = True
                elif (now - start) >= boot_max_wait:
                    log(f"ctx.json not seen after {boot_max_wait:.0f}s -- "
                        f"injecting boot prompt anyway")
                    boot_sent = True
                if boot_sent:
                    try:
                        os.write(master_fd, _flatten_boot_prompt(boot_prompt))
                        # Schedule the Enter as a separate write so Ink
                        # treats it as a key event instead of part of a paste.
                        boot_submit_at = now + submit_delay
                    except OSError as e:
                        log(f"WARN: failed to inject boot prompt: {e}")
                        boot_submit_at = None

            if boot_submit_at is not None and now >= boot_submit_at:
                try:
                    os.write(master_fd, b"\r")
                    log("Boot prompt submitted")
                except OSError as e:
                    log(f"WARN: failed to submit boot prompt: {e}")
                boot_submit_at = None

            pct = get_ctx_pct(session_id)
            if pct is not None:
                if pct >= hard_kill_pct:
                    log(f"Context at {pct}% >= {hard_kill_pct}% -- HARD KILL")
                    return "hard_kill"
                if pct >= ctx_limit_pct:
                    if urgent_since is None:
                        urgent_since = now
                        log(f"Context at {pct}% >= {ctx_limit_pct}% -- "
                            f"hook will inject URGENT, grace period "
                            f"{grace_period}s")
                    elif now - urgent_since > grace_period:
                        log(f"Grace period expired ({grace_period}s), "
                            f"forcing handoff")
                        return "forced"
                else:
                    urgent_since = None
    finally:
        try:
            signal.signal(signal.SIGWINCH, prev_winch)
        except (ValueError, OSError):
            pass
        if stdin_fd is not None and old_attrs is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
            except (termios.error, OSError):
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass


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


def main(argv=None):
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
    args = parser.parse_args(argv)

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
        proc, master_fd = launch_claude(session_id)

        reason = wait_for_exit_or_handoff(
            proc, master_fd, session_id, boot,
            args.ctx_limit, args.hard_kill,
            args.poll_interval, args.grace,
        )

        if reason in ("handoff", "forced", "hard_kill"):
            kill_claude(proc)

        record_session(session_id, session_num, reason)
        prev_session_id = session_id

        if reason == "fast_exit":
            sys.stderr.write(
                "\r\nERROR: CC exited immediately with rc=0 and never wrote "
                "ctx.json. This usually means we accidentally launched in "
                "--print/sdk-cli mode (positional prompt argument). "
                "Aborting outer loop.\r\n"
            )
            log("Fast-exit detected. Aborting outer loop.")
            return 2

        if reason == "exit":
            log("User exited. Stopping outer loop.")
            break

        if reason == "hard_kill":
            log("HARD KILL -- agent did not comply. Restarting anyway.")

        log(f"Restarting in 3s (reason: {reason})...")
        time.sleep(3)

    log("wormlens outer loop stopped")
    return 0


if __name__ == "__main__":
    main()
