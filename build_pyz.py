"""Build wormlens.pyz -- single-file distributable.

Creates a zipapp archive with the package nested inside so relative
imports work correctly. Output goes to the project's .copilot/ directory.

Usage:
    python wormlens/build_pyz.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
OUTPUT = PACKAGE_DIR / ".copilot" / "wormlens.pyz"


def _clean_build_artifacts():
    """Remove stale build/ dist/ *.egg-info/ trees.

    setuptools does not auto-purge these, so a wheel/sdist built after a
    file rename or deletion can otherwise drag ghost modules from a prior
    build into the new artifact (see R4_packaging M1).
    """
    for name in ("build", "dist"):
        shutil.rmtree(PACKAGE_DIR / name, ignore_errors=True)
    for egg in PACKAGE_DIR.glob("*.egg-info"):
        shutil.rmtree(egg, ignore_errors=True)


def main():
    _clean_build_artifacts()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        pkg_dest = tmp / "wormlens"
        shutil.copytree(
            PACKAGE_DIR, pkg_dest,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "build_pyz.py",
                ".git", ".github", ".gitignore",
                ".local", ".copilot", ".pytest_cache",
                "build", "dist", "*.egg-info",
                "*.pyz",
                # Private project docs / scratch -- never ship publicly.
                "CLAUDE.md", "AGENTS.md", "TODO.md", "NOTES.md",
                "CHECKPOINT.md", "HANDOFF.md", "BENCHMARK_RESULTS.md",
                "SHIP_REPORT.md", "tests", ".agent-ignore",
                ".claude", ".vscode", ".idea",
            ),
        )

        entry = tmp / "__main__.py"
        entry.write_text("from wormlens.cli import main\nmain()\n")

        OUTPUT.parent.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [sys.executable, "-m", "zipapp", str(tmp),
             "-o", str(OUTPUT), "-p", "/usr/bin/env python3"],
            check=True,
        )

    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Built: {OUTPUT} ({size_kb:.0f}KB)")


if __name__ == "__main__":
    main()
