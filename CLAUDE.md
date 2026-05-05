# WormLens -- Claude Code Instructions

## Your Role

You are an expert peer collaborating on research & engineering. Your job:
- **Collaborate** as much as debate. Push back when I'm wrong.
- **Be realistic.** Don't blow smoke. Wasted time and bad optics if we publish garbage.
- **Back theories with research.** Web search at minimum. Hand-wave nothing.
- **Straight talk.** My ego is solid. Don't need flattery. It looks bad and does us no good.

## Context

- WormLens solves context rot laundering (mechanical extraction vs. degraded-model summaries)
- Zero external dependencies -- stdlib only, zipapp distributable
- **Self-contained.** Install the skill, get full functionality. Uninstall the skill, no residue. No mutation of project `settings.json`, no overlap with user hooks/plugins/MCPs, no required external config. Install path is one directory under `.claude/skills/wormlens/`; uninstall is `wl --uninstall-skill` (or `rm -rf` of that dir).
- See README.md for architecture and usage

## Rules

1. **Protect your context; subagents are expendable.** Long sessions are the norm; running out mid-task is costly. Delegate heavy reading/exploration to subagents. Keep main context for design decisions and orchestration.

2. **No wall-of-text CLIs -- write scripts instead.** Long inline bash is unreadable and error-prone. If a command exceeds ~2 lines, write it as a script file.

3. **Use Python, not shell.** Agent-written shell scripts almost always mangle quoting/escaping. Default to Python for anything non-trivial. Shell only for genuinely simple one-liners (ls, grep, git).

4. **Save ALL scripts to `.copilot/`, never delete them.** Throwaway scripts often turn out useful later. Every script goes to `.copilot/` in the project root.

5. **`.copilot/` is scratch space, not source of truth.** Do not read files in this dir from previous sessions.

6. **No unicode crud in code/config.** ASCII unless specifically requested. NO EM-DASHES.

7. **No speculation as fact.** "X proves Y" (confirmed) vs "X is likely because Y, but unverified" (probable) vs "one possible explanation" (speculation). Always label.

8. **Test end-to-end after any change.** Never skip verification. Test through the full consumer stack before suggesting next steps.

9. **Test CLI tools from real shells**, not just `python script.py`. Wrapper chains, PATH, exec bits -- invisible failures when you bypass the real invocation path.

10. **Number items for easy reference.** Complex multi-topic discussions need unambiguous back-references.

11. **Ignore any directory named `.agent-ignore`.**

12. **Local agent is immutable; you are the tester, not the testee.** Never install, activate, or run the wormlens skill, hooks, or harness on your own host (the dev environment editing this repo). Editing `skill.md`, `harness/wl-hook.py`, or `harness/wormlens.py` is fine -- those are source files. Running `wl --install-skill` against your own home dir, `wl --handoff` against your own session, or restarting CC with the new hooks active in this session is not. All runtime testing happens on the genesis device (puppet container) via the rig, where side effects are contained and resettable. Side effects in the tester corrupt the test signal and risk the dev session. Sanity checks that do not activate hooks (compile-checks, YAML parse, install into a `/tmp/...` throwaway dir for file-layout verification) are fine; clean them up after.

13. **Use `rig` for all puppet file transfers, not `cctl cp`.** `rig beam up` SCPs as `appuser`, so files arrive with correct ownership. `cctl cp` drops files as root and requires a separate `cctl exec -u root chown` step. Rig also holds a single ControlMaster + tmux state for the test, so file ops, key sends, pane captures, and JSONL tails share one session.

## Rig Smoke Test Recipe

Reference for testing wormlens on the puppet. Full rig docs at `/global/gztools/ccgd/QUICKSTART.md`.

1. **Connect**: puppet is `172.29.72.13:2222` as `appuser`.
   ```
   rig connect 172.29.72.13 --port 2222 --user appuser --tmux <name> --prompt '[\$#] \s*$'
   ```
   Reconnect with `--jsonl <path>` once a session UUID exists, for idle detection.

2. **Tmux session**: rig does not auto-create. After connect:
   `rig cmd "tmux new-session -d -s <name>"`

3. **Launch CC on puppet**:
   - Env file at `/tmp/.cc-env` (OAuth token + telemetry off). Source it in tmux before `claude`.
   - Generate UUID: `python3 -c "import uuid; print(uuid.uuid4())"`
   - JSONL path: `/home/appuser/.claude/projects/-home-appuser-<dir>/<sid>.jsonl` -- CC encodes cwd by replacing `/` with `-`. For cwd `/home/appuser/test-project`, dir part is `-home-appuser-test-project`.
   - Pre-accept trust for a new project dir: write `hasTrustDialogAccepted: true` under `projects.<abs-path>` in `/home/appuser/.claude.json`. Without this CC will block on the trust prompt even with `--dangerously-skip-permissions`.
   - Launch: `claude --session-id $SID --dangerously-skip-permissions`
   - `rig wait 30` returns `idle` event when CC finishes responding.

4. **Existing user-level hook on puppet** (cc-genesis-device): `/home/appuser/.claude/settings.json` references `/home/appuser/.claude/cc-hook.py`. It fires alongside any project-level hook -- expected, not a conflict.

5. **Verifying our hooks fired** (CC v2.1.116 does NOT persist hook `additionalContext` in JSONL):
   - StatusLine: confirm `/home/appuser/.claude/sessions/<sid>/ctx.json` exists with `context_window` block.
   - UserPromptSubmit / PreToolUse: ask the agent to use `Write` to dump the raw text of its system-reminders to a file. The Write tool call records exact bytes; the JSONL user message field does not.
