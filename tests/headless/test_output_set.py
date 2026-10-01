"""OutputSet: temp files, swap over a loaded layer, style/name/position kept, locks, discard.

Covers the Windows file-lock fix: a loaded layer holds its GeoJSON open, so
outputs are written to ``.qthermonet-tmp/`` and swapped in after removing
the layer -- including the case where QGIS's OGR connection pool still holds
the file after a feature read (fixed by flushing deferred deletions).
"""

from __future__ import annotations

import os
import shutil

import _harness
from _harness import check
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor

from QThermonet import output_handling, utils

CRS = QgsCoordinateReferenceSystem("EPSG:25832")


def write_points(path, n):
    """Write `n` points (via write_geojson, the plugin's own writer)."""
    features = []
    for i in range(n):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(500000 + i, 6200000)))
        features.append(f)
    output_handling.write_geojson(path, QgsFields(), QgsWkbTypes.Point, CRS, features)


def red(layer):
    layer.renderer().symbol().setColor(QColor("red"))


d = _harness.temp_dir()
real = os.path.join(d, "out.geojson")
project = QgsProject.instance()

# 1. First export: nothing loaded -> layer loaded with default style, path cached.
outs = output_handling.OutputSet()
tmp = outs.add_file(real, layer_name="Out", cache_roles=["test_role"], style_default=red)
check("temp path is in .qthermonet-tmp next to output", os.path.dirname(tmp) == os.path.join(d, ".qthermonet-tmp"))
write_points(tmp, 2)
outs.commit()
layers = project.mapLayersByName("Out")
check("first commit: file exists", os.path.isfile(real))
check("first commit: layer loaded", len(layers) == 1 and layers[0].featureCount() == 2)
check("first commit: default style applied", layers[0].renderer().symbol().color().name() == "#ff0000")
check("first commit: temp folder removed", not os.path.exists(os.path.join(d, ".qthermonet-tmp")))
check("first commit: path cached", utils.get_cached_path("test_role") == real)

# 2. Re-export over a loaded, renamed, restyled layer in a group at index 1,
#    right after a pooled feature read (like the canvas drawing it).
layer = layers[0]
layer.setName("My renamed layer")
layer.renderer().symbol().setColor(QColor("#00ff00"))
root = project.layerTreeRoot()
group = root.addGroup("grp")
other = QgsVectorLayer("Point?crs=EPSG:25832", "other", "memory")
project.addMapLayer(other, False)
group.addLayer(other)
node = root.findLayer(layer.id())
group.insertChildNode(1, node.clone())
node.parent().removeChildNode(node)

outs = output_handling.OutputSet()
tmp = outs.add_file(real, layer_name="Out", style_default=red)
write_points(tmp, 5)
_ = list(layer.getFeatures())  # pooled read -- the GUI lock case
outs.commit()
new = project.mapLayersByName("My renamed layer")
check("re-export: one layer on the file, old one replaced", len(new) == 1 and not project.mapLayersByName("Out"))
check("re-export: new contents (5 features)", bool(new) and new[0].featureCount() == 5)
check("re-export: copied style kept", bool(new) and new[0].renderer().symbol().color().name() == "#00ff00")
check("re-export: same group and position", [c.name() for c in group.children()] == ["other", "My renamed layer"])

# 3. Locked by "another program": commit fails cleanly, old layer back.
handle = open(real, "rb")
outs = output_handling.OutputSet()
tmp = outs.add_file(real, layer_name="Out", style_default=red)
write_points(tmp, 9)
try:
    outs.commit()
    check("locked: commit raised", False)
except output_handling.OutputCommitError:
    check("locked: commit raised OutputCommitError", True)
handle.close()
back = project.mapLayersByName("My renamed layer")
check("locked: old layer reloaded with old contents", len(back) == 1 and back[0].featureCount() == 5)
check("locked: old style kept", bool(back) and back[0].renderer().symbol().color().name() == "#00ff00")
check("locked: temp cleaned up", not os.path.exists(tmp))

# 4. Discard: temps removed, real file untouched.
outs = output_handling.OutputSet()
tmp = outs.add_file(real, layer_name="Out")
write_points(tmp, 1)
outs.discard()
check("discard: temp and temp folder removed", not os.path.exists(os.path.dirname(tmp)))
check("discard: real file untouched", QgsVectorLayer(real, "x", "ogr").featureCount() == 5)

# 5. Step mode: path unchanged, commit/discard are no-ops.
check("step mode: add_file returns the real path", output_handling.OutputSet(step_mode=True).add_file(real) == real)

# 6. Folder output.
real_folder = os.path.join(d, "modelica-inputs")
outs = output_handling.OutputSet()
tmp_folder = outs.add_folder(real_folder)
for name in ("a.json", "b.json"):
    with open(os.path.join(tmp_folder, name), "w") as f:
        f.write("{}")
outs.commit()
check("folder: files moved in", sorted(os.listdir(real_folder)) == ["a.json", "b.json"])
check("folder: temp removed", not os.path.exists(os.path.join(d, ".qthermonet-tmp")))

project.clear()
shutil.rmtree(d, ignore_errors=True)
_harness.finish()
