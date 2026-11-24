"""Run every ``test_*.py`` in this folder, each in its own process, and summarize.

Separate processes, so a hard crash in one test (e.g. an access violation in
a native library) is reported as a failure instead of stopping the rest.
Run with QGIS's Python -- see ``README.md``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    """Run all tests; return 0 if every one passed."""
    tests = sorted(HERE.glob("test_*.py"))
    failed = []
    for test in tests:
        print(f"===== {test.name}", flush=True)
        completed = subprocess.run(
            [sys.executable, str(test)], cwd=HERE, capture_output=True, text=True
        )
        lines = [line for line in completed.stdout.splitlines() if line.startswith(("PASS", "FAIL")) or "passed" in line]
        print("\n".join(lines) or "(no output)", flush=True)
        if completed.returncode != 0:
            failed.append(test.name)
            print(f"   exit code {completed.returncode}", flush=True)
            if completed.stderr.strip():
                print("   stderr (last lines):", *completed.stderr.strip().splitlines()[-8:], sep="\n   ")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} test files passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
