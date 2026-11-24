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
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRendererCategory,
    QgsSymbol,
    QgsVectorLayer,
    QgsVectorLayerJoinInfo,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QMetaType
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

# 2b. The same file loaded twice ("Duplicate Layer"): both come back, each
#     with its own name, style and position -- after a swap and a rollback.
dup = os.path.join(d, "dup.geojson")
write_points(dup, 1)
dgroup = root.addGroup("dup-grp")
spacer1 = QgsVectorLayer("Point?crs=EPSG:25832", "spacer1", "memory")
spacer2 = QgsVectorLayer("Point?crs=EPSG:25832", "spacer2", "memory")
copy_a = QgsVectorLayer(dup, "Copy A", "ogr")
copy_b = QgsVectorLayer(dup, "Copy B", "ogr")
for lyr in (spacer1, copy_a, spacer2, copy_b):
    project.addMapLayer(lyr, False)
    dgroup.addLayer(lyr)
copy_a.renderer().symbol().setColor(QColor("#0000ff"))
copy_b.renderer().symbol().setColor(QColor("#ffff00"))
expected_order = ["spacer1", "Copy A", "spacer2", "Copy B"]


def dup_state():
    order = [c.name() for c in dgroup.children()]
    colours = {n: project.mapLayersByName(n)[0].renderer().symbol().color().name()
               for n in ("Copy A", "Copy B") if project.mapLayersByName(n)}
    counts = {n: project.mapLayersByName(n)[0].featureCount() for n in ("Copy A", "Copy B")
              if project.mapLayersByName(n)}
    return order, colours, counts


outs = output_handling.OutputSet()
tmp = outs.add_file(dup, layer_name="Dup")
write_points(tmp, 3)
outs.commit()
order, colours, counts = dup_state()
check(f"duplicates: both back in their places after a swap {order}", order == expected_order)
check("duplicates: each keeps its own style", colours == {"Copy A": "#0000ff", "Copy B": "#ffff00"})
check("duplicates: both show the new contents", counts == {"Copy A": 3, "Copy B": 3})

handle = open(dup, "rb")  # rollback case
outs = output_handling.OutputSet()
tmp = outs.add_file(dup, layer_name="Dup")
write_points(tmp, 7)
try:
    outs.commit()
except output_handling.OutputCommitError:
    pass
handle.close()
order, colours, counts = dup_state()
check(f"duplicates: both back in their places after a rollback {order}", order == expected_order)
check("duplicates: old contents and styles after the rollback",
      counts == {"Copy A": 3, "Copy B": 3} and colours == {"Copy A": "#0000ff", "Copy B": "#ffff00"})

# 2c. The layer object itself survives (not removed + reloaded): same ID, so
#     visibility, a categorized style, and a join from another layer all keep
#     working -- after a swap and after a rollback.
kept = os.path.join(d, "kept.geojson")
kfields = QgsFields()
kfields.append(QgsField("id", QMetaType.Type.QString))
kfields.append(QgsField("Thermonet", QMetaType.Type.QString))


def write_kept(path, n, tag):
    feats = []
    for i in range(n):
        f = QgsFeature(kfields)
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(500000 + i, 6200000)))
        f["id"] = f"K{i}"
        f["Thermonet"] = tag
        feats.append(f)
    output_handling.write_geojson(path, kfields, QgsWkbTypes.Point, CRS, feats)


write_kept(kept, 2, "No")
klayer = QgsVectorLayer(kept, "Kept", "ogr")
project.addMapLayer(klayer)
kept_id = klayer.id()
root.findLayer(kept_id).setItemVisibilityChecked(False)
cats = [QgsRendererCategory(v, QgsSymbol.defaultSymbol(klayer.geometryType()), v) for v in ("Yes", "No")]
klayer.setRenderer(QgsCategorizedSymbolRenderer("Thermonet", cats))
table = QgsVectorLayer("None?field=id:string", "table", "memory")
tf = QgsFeature(table.fields())
tf["id"] = "K0"
table.dataProvider().addFeatures([tf])
project.addMapLayer(table)
join = QgsVectorLayerJoinInfo()
join.setJoinLayer(klayer)
join.setJoinFieldName("id")
join.setTargetFieldName("id")
join.setPrefix("j_")
join.setUsingMemoryCache(False)
table.addJoin(join)


def kept_state():
    lyr = project.mapLayer(kept_id)
    renderer = lyr.renderer() if lyr else None
    return (
        lyr is not None,
        root.findLayer(kept_id).itemVisibilityChecked() if lyr else None,
        isinstance(renderer, QgsCategorizedSymbolRenderer) and renderer.classAttribute() == "Thermonet",
        next(table.getFeatures())["j_Thermonet"],
        lyr.featureCount() if lyr else None,
    )


outs = output_handling.OutputSet()
tmp = outs.add_file(kept, layer_name="Kept")
write_kept(tmp, 3, "Yes")
_ = list(klayer.getFeatures())  # pooled read
outs.commit()
same, visible, categorized, joined, count = kept_state()
check("kept layer: same layer ID after a swap", same)
check("kept layer: still unticked after a swap", visible is False)
check("kept layer: categorized style intact", categorized)
check(f"kept layer: join shows the new values ({joined!r}), new contents ({count})", joined == "Yes" and count == 3)

handle = open(kept, "rb")  # rollback case
outs = output_handling.OutputSet()
tmp = outs.add_file(kept, layer_name="Kept")
write_kept(tmp, 5, "Rolled")
try:
    outs.commit()
except output_handling.OutputCommitError:
    pass
handle.close()
same, visible, categorized, joined, count = kept_state()
check("kept layer: same layer ID, unticked, style intact after a rollback", same and visible is False and categorized)
check(f"kept layer: old contents after the rollback ({joined!r}, {count})", joined == "Yes" and count == 3)

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

# 7. drop() in a folder of its own leaves no empty .qthermonet-tmp behind.
other_dir = os.path.join(d, "elsewhere")
os.makedirs(other_dir)
outs = output_handling.OutputSet()
outs.add_file(os.path.join(d, "kept_out.geojson"))
dropped_tmp = outs.add_file(os.path.join(other_dir, "dropped.geojson"))
write_points(dropped_tmp, 1)
outs.drop(os.path.join(other_dir, "dropped.geojson"))
check("drop: its temp folder is removed when empty",
      not os.path.exists(os.path.join(other_dir, ".qthermonet-tmp")))
outs.discard()

# 8. A folder output that fails halfway: the message names exactly the files
#    still waiting -- not the folder, not the files that did arrive.
fail_folder = os.path.join(d, "modelica-fail")
os.makedirs(fail_folder)
with open(os.path.join(fail_folder, "b.json"), "w") as f:
    f.write("old")
blocker = open(os.path.join(fail_folder, "b.json"), "rb")  # "open in another program"
outs = output_handling.OutputSet()
tmp_folder = outs.add_folder(fail_folder)
for name in ("a.json", "b.json"):
    with open(os.path.join(tmp_folder, name), "w") as f:
        f.write("new")
try:
    outs.commit()
    check("folder failure: commit raised", False)
except output_handling.OutputCommitError as exc:
    message = str(exc)
    not_updated_line = next(line for line in message.splitlines() if line.startswith("Not updated"))
    check("folder failure: names the waiting file b.json as not updated",
          os.path.join(fail_folder, "b.json") in not_updated_line)
    check("folder failure: doesn't list the folder itself or the moved a.json as not updated",
          not not_updated_line.rstrip().endswith(fail_folder)
          and os.path.join(fail_folder, "a.json") not in not_updated_line)
    check("folder failure: lists a.json as updated", os.path.join(fail_folder, "a.json") in message)
blocker.close()

project.clear()
shutil.rmtree(d, ignore_errors=True)
_harness.finish()
