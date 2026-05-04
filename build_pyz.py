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


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        pkg_dest = tmp / "wormlens"
        shutil.copytree(
            PACKAGE_DIR, pkg_dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build_pyz.py"),
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
