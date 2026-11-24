"""Source Placement restore: node and rotation read back from a previous export.

Writes trenches/borefields the way the exports do, reads the flagged
connection feature back with `_read_connection_geometry`, and checks node,
rotation (incl. mirrored and negative), the CRS transform, and missing files.
"""

from __future__ import annotations

import os
import shutil

import _harness
from _harness import check
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QMetaType

from QThermonet import output_handling
from QThermonet import source_placement_dialog as sp

UTM = QgsCoordinateReferenceSystem("EPSG:25832")
WEB = QgsCoordinateReferenceSystem("EPSG:3857")
d = _harness.temp_dir()
node = QgsPointXY(557688.37, 6201083.83)


def close(a, b, tol):
    return abs(a - b) <= tol


for mirror, rotation in ((False, 30.0), (True, 30.0), (False, -120.0), (True, 170.0)):
    params = sp._TrenchParameters(
        n_pipes_parallel=4, pipe_spacing=1.5, length_element=150.0, rotation=rotation, mirror_left=mirror,
    )
    path = os.path.join(d, f"trenches_{mirror}_{rotation}.geojson")
    sp._write_trench_geojson(path, sp._generate_trench_lines(node, params), UTM)
    pts = sp._read_connection_geometry(path, UTM)
    got_node, got_rot = sp._derive_connection_node_and_rotation(pts[0], pts[1], mirror)
    got_rot = (got_rot + 180.0) % 360.0 - 180.0
    want_rot = (rotation + 180.0) % 360.0 - 180.0
    check(f"HHE mirror={mirror} rot={rotation}: node back",
          close(got_node.x(), node.x(), 1e-3) and close(got_node.y(), node.y(), 1e-3))
    check(f"HHE mirror={mirror} rot={rotation}: rotation back", close(got_rot, want_rot, 1e-4))

# BHE: written like _export_bhe does.
fields = QgsFields()
fields.append(QgsField("id", QMetaType.Type.QString))
fields.append(QgsField("is_connection_node", QMetaType.Type.Bool))
grid = sp._GridParameters(n_rows=3, n_cols=2, spacing_row=15, spacing_col=10, rotation=20)
features = []
for i, p in enumerate(sp._generate_grid_points(node, grid)):
    f = QgsFeature(fields)
    f.setGeometry(QgsGeometry.fromPointXY(p))
    f["id"] = f"BH{i}"
    f["is_connection_node"] = i == 0
    features.append(f)
bhe_path = os.path.join(d, "borefield.geojson")
output_handling.write_geojson(bhe_path, fields, QgsWkbTypes.Point, UTM, features)
pts = sp._read_connection_geometry(bhe_path, UTM)
check("BHE: connection borehole back",
      bool(pts) and len(pts) == 1 and close(pts[0].x(), node.x(), 1e-3) and close(pts[0].y(), node.y(), 1e-3))
pts_web = sp._read_connection_geometry(bhe_path, WEB)
check("BHE: transformed to EPSG:3857",
      bool(pts_web) and 1.0e6 < pts_web[0].x() < 1.2e6 and 7.5e6 < pts_web[0].y() < 7.6e6)

# BHE mirror: at rotation 0 the columns go below (south of) the first
# borehole by default, above (north) when mirrored; rows are unaffected.
for mirror, sign in ((False, -1), (True, 1)):
    pts = sp._generate_grid_points(node, sp._GridParameters(
        n_rows=2, n_cols=2, spacing_row=15, spacing_col=10, rotation=0, mirror_left=mirror))
    check(f"BHE mirror={mirror}: first borehole on the node", close(pts[0].x(), node.x(), 1e-6) and close(pts[0].y(), node.y(), 1e-6))
    check(f"BHE mirror={mirror}: second column {'above' if mirror else 'below'} the first borehole",
          close(pts[1].x(), node.x(), 1e-6) and close(pts[1].y(), node.y() + sign * 10, 1e-6))
    check(f"BHE mirror={mirror}: second row still east", close(pts[2].x(), node.x() + 15, 1e-6) and close(pts[2].y(), node.y(), 1e-6))

check("missing file -> None", sp._read_connection_geometry(os.path.join(d, "nope.geojson"), UTM) is None)
check("empty path -> None", sp._read_connection_geometry("", UTM) is None)

shutil.rmtree(d, ignore_errors=True)
_harness.finish()
