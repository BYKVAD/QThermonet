"""Build Pipe Network: full run, re-run in place, partial commit on a failed step, cancel.

Runs the real three-step chain on a small generated network: a main road
with one branch, two buildings, and a source point on the road's end. A
failing step commits only the steps before it; a step 1 failure or a cancel
changes no files.
"""

from __future__ import annotations

import os

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
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProject,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QMetaType

from processing.core.Processing import Processing
from QThermonet import output_handling, utils
from QThermonet.processing.build_pipe_network_algorithm import BuildPipeNetworkAlgorithm
from QThermonet.processing.provider import QThermonetProvider

import processing

Processing.initialize()
provider = QThermonetProvider()  # keep a reference, or its Python algorithms are collected
QgsApplication.processingRegistry().addProvider(provider)

CRS = QgsCoordinateReferenceSystem("EPSG:25832")
X0, Y0 = 500000, 6200000


def p(x, y):
    return QgsPointXY(X0 + x, Y0 + y)


def write_lines(path, lines):
    features = []
    for start, end in lines:
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPolylineXY([p(*start), p(*end)]))
        features.append(f)
    output_handling.write_geojson(path, QgsFields(), QgsWkbTypes.LineString, CRS, features)


def write_source(path):
    fields = QgsFields()
    fields.append(QgsField("is_connection_node", QMetaType.Type.Int))
    f = QgsFeature(fields)
    f.setGeometry(QgsGeometry.fromPointXY(p(0, 0)))
    f["is_connection_node"] = 1
    output_handling.write_geojson(path, fields, QgsWkbTypes.Point, CRS, [f])


def write_buildings(path, thermonet, with_ids=True):
    fields = QgsFields()
    fields.append(QgsField("Thermonet", QMetaType.Type.QString))
    if with_ids:
        fields.append(QgsField("id_lokalId", QMetaType.Type.QString))
    features = []
    for hp_id, (cx, cy) in (("hp1", (80, 10)), ("hp2", (60, 30))):
        f = QgsFeature(fields)
        ring = [p(cx - 2, cy - 2), p(cx + 2, cy - 2), p(cx + 2, cy + 2), p(cx - 2, cy + 2)]
        f.setGeometry(QgsGeometry.fromPolygonXY([ring]))
        f["Thermonet"] = thermonet
        if with_ids:
            f["id_lokalId"] = hp_id
        features.append(f)
    output_handling.write_geojson(path, fields, QgsWkbTypes.Polygon, CRS, features)


class Collect(QgsProcessingFeedback):
    """Keeps every reported error, so the failure summary can be checked."""

    def __init__(self):
        super().__init__()
        self.errors = []

    def reportError(self, error, fatalError=False):
        self.errors.append(error)


d = _harness.temp_dir()
inputs = {name: os.path.join(d, "inputs", name) for name in (
    "roads.geojson", "roads_stub.geojson", "source.geojson", "buildings.geojson", "buildings_no.geojson",
    "buildings_raw.geojson",
)}
os.makedirs(os.path.join(d, "inputs"))
write_lines(inputs["roads.geojson"], [((0, 0), (100, 0)), ((50, 0), (50, 40))])
# A branch with no building on it -> Pipe Topology's "no heat pumps" error.
write_lines(inputs["roads_stub.geojson"], [((0, 0), (100, 0)), ((50, 0), (50, 40)), ((70, 0), (70, -20))])
write_source(inputs["source.geojson"])
write_buildings(inputs["buildings.geojson"], "Yes")
write_buildings(inputs["buildings_no.geojson"], "No")
# A buildings layer without heat pump IDs.
write_buildings(inputs["buildings_raw.geojson"], "Yes", with_ids=False)

outputs = {
    "MAINS_OUTPUT": os.path.join(d, "main_pipe_hierarchy.geojson"),
    "SERVICE_PIPES_OUTPUT": os.path.join(d, "service_pipes.geojson"),
    "TOPOLOGY_OUTPUT": os.path.join(d, "pipe_topology.geojson"),
    "TOPOLOGY_DAT_OUTPUT": os.path.join(d, "pipe_topology.dat"),
}
LAYER_NAMES = ("Main pipe hierarchy", "Service pipes", "Pipe topology")


def params(roads="roads.geojson", buildings="buildings.geojson"):
    return {
        "PIPES_LAYER": inputs[roads],
        "SOURCE_LAYER": inputs["source.geojson"],
        "BUILDINGS_LAYER": inputs[buildings],
        **outputs,
    }


def snapshot():
    """Current bytes of every output (None if missing)."""
    result = {}
    for name, path in outputs.items():
        if os.path.isfile(path):
            with open(path, "rb") as f:
                result[name] = f.read()
        else:
            result[name] = None
    return result


def no_temp_left():
    return not os.path.exists(os.path.join(d, ".qthermonet-tmp"))


def layer_ids():
    project = QgsProject.instance()
    return {name: [layer.id() for layer in project.mapLayersByName(name)] for name in LAYER_NAMES}


# 1. Full run.
fb = Collect()
processing.run("QThermonet:build_pipe_network", params(), feedback=fb)
check(f"full run: no errors {fb.errors}", not fb.errors)
check("full run: all four files written", all(os.path.isfile(path) for path in outputs.values()))
check("full run: three layers loaded once each", all(len(ids) == 1 for ids in layer_ids().values()))
check("full run: no temp folder left", no_temp_left())
check("full run: cache roles point at the real files",
      utils.get_cached_path("mains_file") == outputs["MAINS_OUTPUT"]
      and utils.get_cached_path("service_pipes_file") == outputs["SERVICE_PIPES_OUTPUT"]
      and utils.get_cached_path("topology_geojson_file") == outputs["TOPOLOGY_OUTPUT"]
      and utils.get_cached_path("topology_dat_file") == outputs["TOPOLOGY_DAT_OUTPUT"])
with open(outputs["TOPOLOGY_DAT_OUTPUT"], encoding="utf-8") as f:
    dat = f.read()
check("full run: topology names both heat pumps", "hp1" in dat and "hp2" in dat)

# 2. Re-run over the loaded layers: same layer objects, no duplicates.
ids_before = layer_ids()
fb = Collect()
processing.run("QThermonet:build_pipe_network", params(), feedback=fb)
check(f"re-run: no errors {fb.errors}", not fb.errors)
check("re-run: same layers kept (IDs unchanged, no new layers)", layer_ids() == ids_before)
check("re-run: no temp folder left", no_temp_left())

# 3. Step 3 fails (stub pipe): mains + service pipes updated, topology untouched.
before = snapshot()
fb = Collect()
processing.run("QThermonet:build_pipe_network", params(roads="roads_stub.geojson"), feedback=fb)
after = snapshot()
summary = "\n".join(fb.errors)
check("step 3 failure: reported with step name", "Step 3 of 3 (Pipe Topology) failed" in summary)
check("step 3 failure: names the child's reason", "no heat pumps connected" in summary)
check("step 3 failure: summary lists updated and not updated files",
      "Updated:" in summary and "Not updated (left as they were):" in summary
      and outputs["TOPOLOGY_DAT_OUTPUT"] in summary.split("Not updated")[1])
check("step 3 failure: main pipe hierarchy updated", after["MAINS_OUTPUT"] != before["MAINS_OUTPUT"])
check("step 3 failure: service pipes updated", after["SERVICE_PIPES_OUTPUT"] != before["SERVICE_PIPES_OUTPUT"])
check("step 3 failure: topology files untouched",
      after["TOPOLOGY_OUTPUT"] == before["TOPOLOGY_OUTPUT"]
      and after["TOPOLOGY_DAT_OUTPUT"] == before["TOPOLOGY_DAT_OUTPUT"])
check("step 3 failure: layers kept in place", layer_ids() == ids_before)
check("step 3 failure: no temp folder left", no_temp_left())

# 4. Step 2 fails (no Thermonet buildings): only the hierarchy updated.
before = snapshot()
fb = Collect()
processing.run("QThermonet:build_pipe_network", params(buildings="buildings_no.geojson"), feedback=fb)
after = snapshot()
summary = "\n".join(fb.errors)
check("step 2 failure: reported with step name", "Step 2 of 3 (Shortest Service Pipes) failed" in summary)
check("step 2 failure: suggests re-running Build Pipe Network, not a single step",
      "run Build Pipe Network again" in summary and "on its own" not in summary
      and "on their own" not in summary)
check("step 2 failure: main pipe hierarchy updated", after["MAINS_OUTPUT"] != before["MAINS_OUTPUT"])
check("step 2 failure: service pipes and topology untouched",
      all(after[name] == before[name] for name in ("SERVICE_PIPES_OUTPUT", "TOPOLOGY_OUTPUT", "TOPOLOGY_DAT_OUTPUT")))
check("step 2 failure: no temp folder left", no_temp_left())

# 5. Step 1 fails: the run fails, nothing changes. Simulated with a missing
#    child algorithm -- the parent's handling is what's under test.
before = snapshot()
original_steps = BuildPipeNetworkAlgorithm.STEPS
BuildPipeNetworkAlgorithm.STEPS = (
    ("Main Pipe Hierarchy", "QThermonet:does_not_exist", original_steps[0][2]),
) + original_steps[1:]
try:
    processing.run("QThermonet:build_pipe_network", params(), feedback=Collect())
    raised = ""
except QgsProcessingException as exc:
    raised = str(exc)
finally:
    BuildPipeNetworkAlgorithm.STEPS = original_steps
check(f"step 1 failure: run fails ({raised.splitlines()[0] if raised else 'no exception'})",
      "Step 1 of 3 (Main Pipe Hierarchy) failed" in raised)
check("step 1 failure: no files changed", snapshot() == before)
check("step 1 failure: no temp folder left", no_temp_left())

# 6. Buildings layer without heat pump IDs: rejected before anything runs.
before = snapshot()
try:
    processing.run("QThermonet:build_pipe_network", params(buildings="buildings_raw.geojson"), feedback=Collect())
    raised = ""
except QgsProcessingException as exc:
    raised = str(exc)
check(f"no heat pump IDs: rejected up front ({raised.splitlines()[-1] if raised else 'no exception'})",
      "id_lokalId" in raised and "heat pump IDs" in raised)
check("no heat pump IDs: no files changed", snapshot() == before)

# 7. Cancel: nothing changes.
before = snapshot()
fb = Collect()
fb.cancel()
try:
    processing.run("QThermonet:build_pipe_network", params(), feedback=fb)
    raised = ""
except QgsProcessingException as exc:
    raised = str(exc)
check(f"cancel: run fails as cancelled ({raised.splitlines()[0] if raised else 'no exception'})",
      "Cancelled" in raised)
check("cancel: no files changed", snapshot() == before)
check("cancel: no temp folder left", no_temp_left())

_harness.finish()
