"""Shared setup for QThermonet's headless tests.

Starts a headless QGIS, makes the plugin importable as ``QThermonet`` (from
this repo, whatever it is checked out as -- the folder must be named
``QThermonet``) plus QGIS's own ``processing`` plugin, and provides a tiny
``check()``/``finish()`` reporting helper. Run tests with QGIS's Python
(``python-qgis.bat``) -- see ``README.md``.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
from pathlib import Path

faulthandler.enable()

from qgis.core import QgsApplication  # noqa: E402

REPO_DIR = Path(__file__).resolve().parents[2]
if REPO_DIR.name != "QThermonet":
    raise SystemExit(
        f"The repo folder must be named 'QThermonet' to import the plugin package (got {REPO_DIR.name!r})."
    )

app = QgsApplication([], True)
app.initQgis()
sys.path.insert(0, str(REPO_DIR.parent))
sys.path.insert(0, os.path.join(QgsApplication.prefixPath(), "python", "plugins"))

_results: list[bool] = []


def check(label: str, ok: bool) -> None:
    """Record and print one test assertion."""
    _results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + label, flush=True)


def temp_dir() -> str:
    """A fresh temporary folder for one test's files."""
    return tempfile.mkdtemp(prefix="qthermonet_test_")


def finish() -> None:
    """Print the summary, shut QGIS down, and exit non-zero if anything failed."""
    passed, total = sum(_results), len(_results)
    print(f"\n{passed}/{total} passed", flush=True)
    app.exitQgis()
    sys.exit(0 if total and passed == total else 1)
