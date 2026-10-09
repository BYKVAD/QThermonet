"""Network cropping rules on a hand-made network (Pipe Topology's `_crop_network`).

Design: ``claude/handoffs/2026-10-09-network-cropping-design.md``. Main pipes
without heat pumps on them or downstream are removed; pipe ends past the
last connection are trimmed; a removed branch's junction doesn't keep a tail
alive; pipes not connected to the source are left as is.
"""

from __future__ import annotations

import _harness
from _harness import check
from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsPointXY, QgsProject

from QThermonet import utils
from QThermonet.processing import pipe_topology_algorithm as pt

X0, Y0 = 500000, 6200000


def line(*points):
    return QgsGeometry.fromPolylineXY([QgsPointXY(X0 + x, Y0 + y) for x, y in points])


pipes = [
    (1, line((0, 0), (130, 0)), 0),         # main; last building at x=80 -> 50 m tail
    (2, line((50, 0), (50, 40)), 1),        # branch; building at y=30 -> 10 m tail
    (3, line((70, 0), (70, -20)), 2),       # dead branch -> removed (and no keep point at x=70)
    (4, line((300, 300), (320, 300)), -1),  # not connected to the source -> left as is
]
service_pipes = [line((80, 8), (80, 0)), line((58, 30), (50, 30)), line((310, 308), (310, 300))]
distance_area = utils.create_distance_area(
    QgsCoordinateReferenceSystem("EPSG:25832"), QgsProject.instance().transformContext()
)

kept, report = pt._crop_network(pipes, service_pipes, QgsPointXY(X0, Y0), distance_area)
actions = sorted(action for action, _, _ in report)
check("dead branch removed", 3 not in kept)
check(f"main trimmed to the last building ({kept[1].length():.2f} m)", abs(kept[1].length() - 80) < 0.01)
check(f"branch trimmed to its building ({kept[2].length():.2f} m)", abs(kept[2].length() - 30) < 0.01)
check("main still starts at the source", kept[1].asPolyline()[0].distance(QgsPointXY(X0, Y0)) < 0.01)
check("unconnected pipe left as is", abs(kept[4].length() - 20) < 0.01)
check(f"report: one removal, two trims, one left as is {actions}", actions == [
    "Not connected to the source, left as is",
    "Removed a main pipe without heat pumps",
    "Trimmed the end of a main pipe",
    "Trimmed the end of a main pipe",
])

kept, _ = pt._crop_network(pipes[:3], [], QgsPointXY(X0, Y0), distance_area)
check("no buildings at all -> nothing kept", not kept)

_harness.finish()
