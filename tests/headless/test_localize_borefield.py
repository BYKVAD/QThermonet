"""QThermonet's QGIS-based borefield localization must match pythermonet's.

`_localize_borefield_coordinates_qgis` (full_dimensioning_algorithm.py)
mirrors pythermonet's pyproj-based `localize_borefield_coordinates`, because
pyproj crashes QGIS on its 2nd call from a Processing background thread.
This checks the two give the same result, and that the QGIS one survives
repeated background-thread runs.
"""

from __future__ import annotations

import time

import _harness
import numpy as np
from _harness import check
from qgis.core import QgsApplication, QgsTask
from qgis.PyQt.QtCore import QCoreApplication

from pythermonet.components.vhe_field import BorefieldCoordinatesInput
from pythermonet.components.vhe_field import localize_borefield_coordinates as pythermonet_version
from QThermonet.processing.full_dimensioning_algorithm import (
    _localize_borefield_coordinates_qgis as qgis_version,
)

projected = BorefieldCoordinatesInput(
    ids=["BH0", "BH1", "BH2"],
    x=np.array([556987.02, 557002.02, 556987.02]), y=np.array([6200998.37, 6200998.37, 6200983.37]),
    z=None, crs="EPSG:25832",
)
geographic = BorefieldCoordinatesInput(
    ids=["BH0", "BH1", "BH2"],
    x=np.array([9.9128, 9.9131, 9.9128]), y=np.array([55.9509, 55.9509, 55.9508]),
    z=None, crs="EPSG:4326",
)
with_z = BorefieldCoordinatesInput(
    ids=["BH0", "BH1"], x=np.array([556987.02, 557002.02]), y=np.array([6200998.37, 6200998.37]),
    z=np.array([-1.0, -2.0]), crs="EPSG:25832",
)

# 1. Same result as pythermonet (pythermonet on the main thread, where pyproj is fine).
for label, bf, tol in (("projected EPSG:25832", projected, 1e-9),
                       ("geographic EPSG:4326", geographic, 1e-6),
                       ("projected with z", with_z, 1e-9)):
    q, p = np.array(qgis_version(bf)), np.array(pythermonet_version(bf))
    check(f"{label}: same as pythermonet (max diff {np.abs(q - p).max():.2e} m)",
          q.shape == p.shape and np.abs(q - p).max() <= tol)

# 2. Eight runs on QGIS background threads (pyproj crashes on the 2nd).
done = []


def run(task, n):
    done.append((n, qgis_version(projected)[1]))
    return True


tasks = []  # keep Python references -- otherwise a task is garbage-collected before it runs
for n in range(1, 9):
    tasks.append(QgsTask.fromFunction(f"localize {n}", run, n))
    QgsApplication.taskManager().addTask(tasks[-1])
    deadline = time.time() + 10
    while len(done) < n and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.05)
    time.sleep(0.5)  # let the pool thread retire, so the next task may get a new one
check(f"8 background-thread runs without crash ({len(done)} done)", len(done) == 8)

_harness.finish()
