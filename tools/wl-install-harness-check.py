#!/usr/bin/env python3
"""Fleet smoke harness for the installed `wl` command.

This intentionally does not require pytest. It validates the real command path
used by crew agents:

1. `wl --doctor --no-color` runs and imports providers.
2. `wl --install-skill --skill-target TMP_PROJECT` installs the skill and hooks.
3. Installed files and `.claude/settings.json` contain the managed wormlens hook
   entries.
4. `wl --uninstall-skill` removes only the managed install.
5. `wl launch --help` reaches the harness parser.

It does not start Claude Code or touch the caller's real ~/.claude tree.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_WL = Path('/global/crew/scripts/wl')
DEFAULT_SOURCE = Path('/global/gztools/wormlens')
HOOK_MARKER = 'wormlens/wl-hook.py'


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault('NO_COLOR', '1')
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def fail(msg: str, proc: subprocess.CompletedProcess[str] | None = None) -> int:
    print(f'[FAIL] {msg}', file=sys.stderr)
    if proc is not None:
        print(f'  rc={proc.returncode}', file=sys.stderr)
        if proc.stdout:
            print('  stdout:', proc.stdout[-2000:], file=sys.stderr)
        if proc.stderr:
            print('  stderr:', proc.stderr[-2000:], file=sys.stderr)
    return 1


def ok(msg: str) -> None:
    print(f'[OK] {msg}')


def assert_hook_entry(settings: dict, key: str) -> bool:
    if key == 'statusLine':
        sl = settings.get('statusLine')
        return isinstance(sl, dict) and HOOK_MARKER in str(sl.get('command', ''))
    hooks = settings.get('hooks')
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(key)
    if not isinstance(entries, list):
        return False
    return any(HOOK_MARKER in json.dumps(entry, sort_keys=True) for entry in entries)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--wl', default=str(DEFAULT_WL), help='wl executable to test')
    ap.add_argument('--source-tree', default=str(DEFAULT_SOURCE), help='expected live source tree')
    ap.add_argument('--keep-temp', action='store_true', help='do not delete temp project')
    args = ap.parse_args(argv)

    wl = Path(args.wl)
    source_tree = Path(args.source_tree)
    if not wl.is_file() or not os.access(wl, os.X_OK):
        return fail(f'wl executable missing/not executable: {wl}')
    if not source_tree.is_dir():
        return fail(f'expected source tree missing: {source_tree}')

    wrapper_text = wl.read_text(errors='replace') if wl.is_file() else ''
    expected_refs = {str(source_tree), str(source_tree.parent)}
    if not any(ref in wrapper_text for ref in expected_refs):
        return fail(f'{wl} does not reference expected source tree or parent: {source_tree}')
    ok(f'{wl} references live source tree path via {source_tree.parent}')

    proc = run([str(wl), '--doctor', '--no-color'])
    if proc.returncode != 0:
        return fail('wl --doctor exited non-zero', proc)
    if '[OK] Provider import:' not in proc.stdout:
        return fail('wl --doctor did not report provider imports', proc)
    ok('wl --doctor runs and imports providers')

    proc = run([str(wl), 'launch', '--help'])
    if proc.returncode != 0:
        return fail('wl launch --help exited non-zero', proc)
    if '--ctx-limit' not in proc.stdout or '--hard-kill' not in proc.stdout:
        return fail('wl launch --help did not reach harness parser', proc)
    ok('wl launch subcommand reaches harness parser')

    tmp_root = Path(tempfile.mkdtemp(prefix='wl-install-harness-'))
    try:
        project = tmp_root / 'project'
        project.mkdir()
        (project / '.git').mkdir()

        proc = run([str(wl), '--install-skill', '--skill-target', str(project)], cwd=project)
        if proc.returncode != 0:
            return fail('wl --install-skill failed in temp project', proc)

        skill = project / '.claude' / 'skills' / 'wormlens' / 'SKILL.md'
        hook = project / '.claude' / 'skills' / 'wormlens' / 'wl-hook.py'
        settings_path = project / '.claude' / 'settings.json'
        for path in (skill, hook, settings_path):
            if not path.is_file():
                return fail(f'install missing expected file: {path}')
        if not os.access(hook, os.X_OK):
            return fail(f'installed hook is not executable: {hook}')

        settings = json.loads(settings_path.read_text())
        for key in ('statusLine', 'UserPromptSubmit', 'PreToolUse'):
            if not assert_hook_entry(settings, key):
                return fail(f'settings.json missing managed wormlens entry: {key}')
        ok('skill install creates hook, skill, and managed settings entries')

        proc = run([str(wl), '--uninstall-skill', '--skill-target', str(project)], cwd=project)
        if proc.returncode != 0:
            return fail('wl --uninstall-skill failed in temp project', proc)
        if skill.exists() or hook.exists():
            return fail('uninstall left managed skill files behind')
        if settings_path.exists():
            settings_after = json.loads(settings_path.read_text())
            if HOOK_MARKER in json.dumps(settings_after, sort_keys=True):
                return fail('uninstall left wormlens hook markers in settings.json')
        ok('skill uninstall removes managed install cleanly')

        ok('wormlens install harness check passed')
        return 0
    finally:
        if args.keep_temp:
            print(f'[INFO] kept temp dir: {tmp_root}')
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
