"""refresh_hhe_trench_layer rewrites the trench file instead of editing it in place.

The old in-place truncate + add could leave old and new trenches both in the
file (deletes failed on a stale OGR handle, the add still ran). Builds a
settings file from pythermonet's HHE template and a trench file holding the
trenches twice, then checks the refresh leaves exactly `n_pipes_parallel`
trenches, keeps the layer's style, and cleans up.
"""

from __future__ import annotations

import json
import os
import shutil

import _harness
from _harness import check
from qgis.core import QgsCoordinateReferenceSystem, QgsPointXY, QgsProject, QgsVectorLayer
from qgis.PyQt.QtGui import QColor

from pythermonet.input import load_settings
from pythermonet.resources import SETTINGS_TEMPLATE_HHE_PATH
from QThermonet import source_placement_dialog as sp

d = _harness.temp_dir()
settings_path = os.path.join(d, "settings_hhe.json")
trench_path = os.path.join(d, "trenches.geojson")
shutil.copyfile(SETTINGS_TEMPLATE_HHE_PATH, settings_path)

with open(settings_path, encoding="utf-8") as f:
    raw = json.load(f)
raw["qthermonet_hhe_settings"] = {"geojson_path": trench_path, "mirror_left": False}
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(raw, f, indent=2)

hhe = load_settings(settings_path)["hhe_field_parameters"]
n_expected = hhe.n_pipes_parallel
params = sp._TrenchParameters(
    n_pipes_parallel=n_expected, pipe_spacing=hhe.pipe_spacing, length_element=hhe.length_element,
    rotation=25.0, mirror_left=False,
)
lines = sp._generate_trench_lines(QgsPointXY(557688.37, 6201083.83), params)
sp._write_trench_geojson(trench_path, lines + lines, QgsCoordinateReferenceSystem("EPSG:25832"))  # doubled

layer = QgsVectorLayer(trench_path, "HHE trenches", "ogr")
project = QgsProject.instance()
project.addMapLayer(layer)
check(f"setup: trench file starts doubled ({layer.featureCount()} = 2 x {n_expected})",
      layer.featureCount() == 2 * n_expected)
layer.renderer().symbol().setColor(QColor("#00ff00"))
_ = list(layer.getFeatures())  # pooled read, like the canvas drawing it

sp.refresh_hhe_trench_layer(settings_path)

new = project.mapLayersByName("HHE trenches")
n_after = QgsVectorLayer(trench_path, "check", "ogr").featureCount()
check("exactly one 'HHE trenches' layer afterwards", len(new) == 1)
check(f"file rewritten with n_pipes_parallel trenches ({n_after}), not appended", n_after == n_expected)
check("layer shows the new count", bool(new) and new[0].featureCount() == n_after)
check("layer still in EPSG:25832", bool(new) and new[0].crs().authid() == "EPSG:25832")
check("style kept", bool(new) and new[0].renderer().symbol().color().name() == "#00ff00")
check("no temp folder left", not os.path.exists(os.path.join(d, ".qthermonet-tmp")))

project.clear()
shutil.rmtree(d, ignore_errors=True)
_harness.finish()
