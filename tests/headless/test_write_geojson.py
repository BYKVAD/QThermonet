"""write_geojson: the file keeps its CRS, for any authority code, on any thread.

Background: GeoJSONs were written without a "crs" member in some GUI
sessions (QGIS then reads them as WGS84 -> invisible layers); the helper
writes via a memory layer + writeAsVectorFormatV3 and checks the file's own
"crs" member afterwards.
"""

from __future__ import annotations

import os
import shutil
import time

import _harness
from _harness import check
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsTask,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QMetaType

from QThermonet import output_handling as oh

d = _harness.temp_dir()
fields = QgsFields()
fields.append(QgsField("id", QMetaType.Type.QString))
fields.append(QgsField("is_connection_node", QMetaType.Type.Bool))


def lines(x, y, n=3):
    out = []
    for i in range(n):
        f = QgsFeature(fields)
        f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x + i, y), QgsPointXY(x + i, y + 1)]))
        f["id"] = f"TR{i}"
        f["is_connection_node"] = i == 0
        out.append(f)
    return out


# 1. Round trip for several CRSs and authorities.
cases = {
    "EPSG:25832": (557015.0, 6201021.0),
    "EPSG:4326": (9.92, 55.95),
    "EPSG:3857": (1104000.0, 7548800.0),
    "EPSG:2154": (650000.0, 6860000.0),
    "EPSG:5972": (557015.0, 6201021.0),
    "ESRI:102001": (100000.0, 100000.0),
}
for authid, (x, y) in cases.items():
    crs = QgsCoordinateReferenceSystem(authid)
    path = os.path.join(d, authid.replace(":", "_") + ".geojson")
    try:
        oh.write_geojson(path, fields, QgsWkbTypes.LineString, crs, lines(x, y))
        layer = QgsVectorLayer(path, "c", "ogr")
        first = next(layer.getFeatures())
        ok = (layer.crs().authid() == authid and layer.featureCount() == 3 and first["id"] == "TR0"
              and abs(first.geometry().asPolyline()[0].x() - x) < 1e-6)
        check(f"{authid}: CRS, features, attributes and coordinates kept", ok)
        del layer
    except OSError as exc:
        check(f"{authid}: written ({exc})", False)

# 2. Refusals.
for label, crs in (
    ("invalid CRS", QgsCoordinateReferenceSystem()),
    ("custom CRS without a code",
     QgsCoordinateReferenceSystem.fromProj("+proj=tmerc +lon_0=10.5 +k=0.9996 +x_0=500000 +ellps=GRS80 +units=m")),
):
    try:
        oh.write_geojson(os.path.join(d, "bad.geojson"), fields, QgsWkbTypes.LineString, crs, [])
        check(f"{label} refused", False)
    except OSError:
        check(f"{label} refused", True)

# 2b. Features the memory layer rejects are reported, not silently dropped.
int_fields = QgsFields()
int_fields.append(QgsField("n", QMetaType.Type.Int))
bad_value = QgsFeature(int_fields)
bad_value.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(0, 0), QgsPointXY(1, 1)]))
bad_value["n"] = "abc"  # text in an Int field
wrong_geom = QgsFeature(int_fields)
wrong_geom.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))  # point into a line layer
wrong_geom["n"] = 1
for label, feature in (("text in a number field", bad_value), ("point into a line layer", wrong_geom)):
    path = os.path.join(d, "rejected.geojson")
    try:
        oh.write_geojson(path, int_fields, QgsWkbTypes.LineString,
                         QgsCoordinateReferenceSystem("EPSG:25832"), [feature])
        check(f"rejected feature ({label}) reported", False)
    except OSError as exc:
        check(f"rejected feature ({label}) reported: {exc}", "0 of 1" in str(exc) and not os.path.exists(path))

# 3. Point geometry.
pt = QgsFeature(QgsFields())
pt.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(557015, 6201021)))
p = os.path.join(d, "point.geojson")
oh.write_geojson(p, QgsFields(), QgsWkbTypes.Point, QgsCoordinateReferenceSystem("EPSG:25832"), [pt])
check("point geometry written", QgsVectorLayer(p, "p", "ogr").featureCount() == 1)

# 4. Missing "crs" member is detected.
nocrs = os.path.join(d, "nocrs.geojson")
with open(nocrs, "w") as f:
    f.write('{"type":"FeatureCollection","features":[]}')
check("missing crs member detected", oh._geojson_crs_authid(nocrs) is None)

# 5. On a QGIS background thread (like Pipe Topology's run).
bg = {}


def bg_write(task):
    try:
        oh.write_geojson(os.path.join(d, "background.geojson"), fields, QgsWkbTypes.LineString,
                         QgsCoordinateReferenceSystem("EPSG:25832"), lines(557015, 6201021, 1))
        bg["ok"] = True
    except Exception as exc:  # noqa: BLE001
        bg["error"] = repr(exc)
    return True


task = QgsTask.fromFunction("bg write", bg_write)  # keep a reference, or it's collected before running
QgsApplication.taskManager().addTask(task)
deadline = time.time() + 10
while not bg and time.time() < deadline:
    QCoreApplication.processEvents()
    time.sleep(0.05)
check(f"background-thread write {bg}", bg.get("ok") is True)
check("background file reads back as EPSG:25832",
      QgsVectorLayer(os.path.join(d, "background.geojson"), "b", "ogr").crs().authid() == "EPSG:25832")

shutil.rmtree(d, ignore_errors=True)
_harness.finish()
