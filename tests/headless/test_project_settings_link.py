"""The QGIS project remembers its active settings file -- also across a restart.

`utils` stores the active settings file in the project file
(`QgsProject.writeEntry`) and reactivates it when the project is opened, so
the tools' saved paths are the defaults again after a QGIS restart. A new
project, and a "Save As" copy, start without a link. A restart is simulated
by opening the saved project in a fresh QGIS process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import _harness
from _harness import check
from qgis.core import QgsProject

from pythermonet.resources import SETTINGS_TEMPLATE_BHE_PATH
from QThermonet import utils

ROLE = "load_dat_file"


def reopen_in_fresh_qgis(project_path: str) -> dict:
    """Open `project_path` in a new QGIS process (= after a restart); report what's active."""
    out = subprocess.run(
        [sys.executable, __file__, "--reopen", project_path],
        cwd=os.path.dirname(__file__), capture_output=True, text=True,
    )
    for line in out.stdout.splitlines():
        if line.startswith("REOPEN "):
            return json.loads(line[len("REOPEN "):])
    return {"error": out.stderr.strip().splitlines()[-1:] or "no output"}


if len(sys.argv) > 2 and sys.argv[1] == "--reopen":
    # Child process: a fresh QGIS that only opens the project.
    utils.connect_project_signals()
    QgsProject.instance().read(sys.argv[2])
    print("REOPEN " + json.dumps({
        "settings_path": utils.get_current_settings_path(),
        "cached": utils.get_cached_path(ROLE),
    }), flush=True)
    utils.disconnect_project_signals()
    QgsProject.instance().clear()
    _harness.app.exitQgis()
    sys.exit(0)

d = _harness.temp_dir()
settings = os.path.join(d, "settings_bhe.json")
shutil.copyfile(SETTINGS_TEMPLATE_BHE_PATH, settings)
a, b = os.path.join(d, "a.qgz"), os.path.join(d, "b.qgz")
heat_loads = os.path.join(d, "heat_loads.dat")
project = QgsProject.instance()
utils.connect_project_signals()

# 1. New (unsaved) project: open a settings file, save a path.
utils.load_existing_settings_path(settings)
utils.set_cached_path(ROLE, heat_loads)
entry, ok = project.readEntry("QThermonet", "settings_path")
check("new project: link written into the project", ok and entry == settings)
check("new project: path saved in the settings file", utils.get_cached_path(ROLE) == heat_loads)

# 2. First save keeps the session.
project.write(a)
check("first save: settings file still active", utils.get_current_settings_path() == settings)
check("first save: saved path still the default", utils.get_cached_path(ROLE) == heat_loads)

# 3. Save As: the copy starts without a link.
project.write(b)
entry_b, ok_b = project.readEntry("QThermonet", "settings_path")
check("save as: link removed from the copy", not ok_b or not entry_b)
check("save as: no settings file active in the copy", utils.get_current_settings_path() is None)
check("save as: no saved-path default in the copy", utils.get_cached_path(ROLE) is None)

# 4. "Restart": a fresh QGIS opens each project.
after_a = reopen_in_fresh_qgis(a)
check(f"restart + open a.qgz: settings file reactivated ({after_a})",
      after_a.get("settings_path") == settings and after_a.get("cached") == heat_loads)
after_b = reopen_in_fresh_qgis(b)
check(f"restart + open b.qgz (Save As copy): nothing active ({after_b})",
      after_b.get("settings_path") is None and after_b.get("cached") is None)

# 5. "New project" starts empty; reopening a.qgz in this session works too.
project.clear()
check("new project: nothing active", utils.get_current_settings_path() is None)
project.read(a)
check("open a.qgz in the same session: settings file active", utils.get_current_settings_path() == settings)

# 6. Linked settings file gone: opening the project just activates nothing.
project.clear()
os.remove(settings)
project.read(a)
check("linked settings file deleted: nothing active, no error", utils.get_current_settings_path() is None)

utils.disconnect_project_signals()
project.clear()
shutil.rmtree(d, ignore_errors=True)
_harness.finish()
