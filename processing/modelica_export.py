# -*- coding: utf-8 -*-
"""Building and writing the Modelica-export files for a BHE project.

Four files, written into one destination folder: ``heatpumps.json``,
``pipes.json``, ``boreholes.json``, and a trimmed passthrough of the
project's settings file. See
``claude/handoffs/2026-09-23-modelica-export-overview.md`` and the
``/grilling`` session that followed it for the design this implements.

Scope: BHE only -- deliberate, see the design discussion for why. Main-pipe
topology can have multiple, tree-shaped branches (a main pipe teeing into
another main pipe); it does not support a main pipe re-merging with another
downstream of a shared heat pump (a true loop/mesh), which the algorithm below
doesn't attempt to detect.
"""

from __future__ import annotations

import json
from pathlib import Path

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingException,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

# Matches the proximity tolerance pipe_topology_algorithm.py already uses for
# near-overlapping features ("Dist = 0.1 # distance in meter, to ensure
# robustness for near-overlapping features").
_CONNECTION_NODE_SNAP_TOLERANCE_M = 0.1  # m


def _fine_segments_from_fractions(
    tie_ins: list[tuple[str, float]],
    section_name: str,
    length_authoritative: float,
    merge_tolerance_m: float = _CONNECTION_NODE_SNAP_TOLERANCE_M,
) -> tuple[list[dict], dict[str, str]]:
    """Split one main-pipe section into fine sub-segments at each tie-in point.

    Pure function -- no geometry objects, just already-computed fractional
    positions along the line. Kept pure so the ordering/node-assignment logic
    can be validated independently of QGIS (see the design session's scratch
    validation against ``08-kattrup-straight-road-simple-case`` and
    ``09-test-jane``).

    Near-coincident positions -- including a tie-in landing exactly on one of
    the section's own endpoints -- are clustered into one shared node instead
    of producing a zero-length pipe segment. Confirmed necessary against real
    data: two heat pumps tee-ing off the exact same point, and a heat pump
    tying in exactly where the section meets the borefield connection.

    Parameters
    ----------
    tie_ins : list of (str, float)
        ``(key, fraction_along_line)`` pairs, any order. `key` is normally a
        heat pump ID, but may be any string identifying what ties in here
        (e.g. a marker for another main-pipe section joining at this point).
        Fraction is in [0, 1], as returned by ``QgsGeometry.lineLocatePoint``
        divided by the line's own (possibly CRS-distorted) length -- the
        ratio is scale-invariant even though the absolute distances aren't
        real meters in a distorting CRS such as Web Mercator.
    section_name : str
        The coarse ``Section`` name this line belongs to (e.g.
        ``"Pipe_branch_1"``), used as the prefix for synthetic node IDs.
    length_authoritative : float
        The section's real-world length [m], e.g. from the topology file's
        own ``Trace_(m)`` (computed with ellipsoidal measurement) -- used to
        scale fractions into real lengths.
    merge_tolerance_m : float
        Positions within this many real-world meters of each other (or of
        either endpoint) share one node instead of getting their own.

    Returns
    -------
    tuple of (list of dict, dict of str to str)
        ``segments``: ordered list of ``{"start_node", "end_node", "length"}``
        dicts covering the whole line. ``node_by_key``: maps each tie-in key
        to the node it connects to.

    """
    positions = sorted(
        [(frac, key) for key, frac in tie_ins] + [(0.0, None), (1.0, None)],
        key=lambda t: t[0],
    )

    clusters: list[list] = []
    for frac, key in positions:
        if clusters and (frac - clusters[-1][0]) * length_authoritative <= merge_tolerance_m:
            clusters[-1][1].append(key)
        else:
            clusters.append([frac, [key]])
    clusters[0][0] = 0.0
    clusters[-1][0] = 1.0

    node_ids = [f"{section_name}_n{i}" for i in range(len(clusters))]
    node_by_key: dict[str, str] = {}
    for node_id, (_frac, keys) in zip(node_ids, clusters):
        for key in keys:
            if key is not None:
                node_by_key[key] = node_id

    segments = []
    for i in range(len(clusters) - 1):
        segments.append({
            "start_node": node_ids[i],
            "end_node": node_ids[i + 1],
            "length": (clusters[i + 1][0] - clusters[i][0]) * length_authoritative,
        })
    return segments, node_by_key


def _single_line_geometry(geometry: QgsGeometry) -> QgsGeometry:
    """A plain single-part LineString geometry, given a LineString or a
    MultiLineString(one part) -- `lineLocatePoint`/`length` need an actual
    line geometry, not a multi-part wrapper around one. Preserves every
    vertex of the line (bends included), not just its endpoints.
    """
    if geometry.isMultipart():
        parts = geometry.asMultiPolyline()
        return QgsGeometry.fromPolylineXY(parts[0])
    return geometry


def _tie_in_fraction(main_geometry: QgsGeometry, point: QgsPointXY) -> float:
    """Fractional distance of `point` along `main_geometry`, in [0, 1]."""
    line = _single_line_geometry(main_geometry)
    distance_along = line.lineLocatePoint(QgsGeometry.fromPointXY(point))
    return distance_along / line.length()


def _line_endpoints(geometry: QgsGeometry) -> tuple[QgsPointXY, QgsPointXY]:
    """First and last vertex of a LineString/MultiLineString(one part)."""
    points = _single_line_geometry(geometry).asPolyline()
    return points[0], points[-1]


def _nearest_section_within_tolerance(
    point: QgsPointXY,
    geometries: dict[str, QgsGeometry],
    tolerance_m: float = _CONNECTION_NODE_SNAP_TOLERANCE_M,
) -> str | None:
    """Name of whichever `geometries` entry `point` is closest to, within
    `tolerance_m` -- `None` if nothing is close enough.
    """
    best_name: str | None = None
    best_distance: float | None = None
    point_geometry = QgsGeometry.fromPointXY(point)
    for name, geometry in geometries.items():
        distance = geometry.distance(point_geometry)
        if best_distance is None or distance < best_distance:
            best_name, best_distance = name, distance
    if best_distance is not None and best_distance <= tolerance_m:
        return best_name
    return None


def build_heatpumps_export(hp_list: list) -> list[dict]:
    """Build the ``heatpumps.json`` content from pythermonet's heat pump list.

    Delivered (building-side) loads and COP values, deliberately not the
    pre-computed ground-extracted loads -- see the design session for why
    (Modelica's own heat pump component is meant to compute that split
    dynamically from COP, not receive it pre-baked in).

    Parameters
    ----------
    hp_list : list of pythermonet.components.heat_pump.HeatPump
        Heat pumps as read by ``pythermonet.input.read_heat_pumps_tsv``.

    Returns
    -------
    list of dict
        One entry per heat pump.

    """
    heatpumps = []
    for hp in hp_list:
        heatpumps.append({
            "id": f"HP_{hp.id_}",
            "load_annual_heating": float(hp.load_annual_heating),      # W
            "load_winter_heating": float(hp.load_winter_heating),      # W
            "load_peak_heating": float(hp.load_peak_heating),          # W
            "scop_annual": float(hp.scop_annual),
            "scop_winter": float(hp.scop_winter),
            "cop_peak": float(hp.cop_peak),
            "load_annual_cooling": float(hp.load_annual_cooling),      # W
            "load_summer_cooling": float(hp.load_summer_cooling),      # W
            "load_peak_cooling": float(hp.load_peak_cooling),          # W
            "eer": float(hp.eer),
        })
    return heatpumps


_MAIN_JUNCTION_KEY_PREFIX = "__main_junction__"


def build_pipes_export(
    topology_geojson_path: str,
    hydraulic,
    brine,
    borefield_connection_point: QgsPointXY | None,
    topology_crs_authid: str,
    feedback,
) -> list[dict]:
    """Build the ``pipes.json`` content: fine sub-segments, node-based.

    Splits each main-pipe ``Section`` into fine sub-segments at every
    service-pipe tie-in point along it, and at every point another main-pipe
    section joins it (geometry-only post-processing on top of pythermonet's
    already-computed, coarse per-``Section`` diameter/flow -- no new
    pythermonet dimensioning work). Service-pipe sections are already the
    finest unit and pass through as a single segment each.

    A service pipe is attributed to whichever main-pipe section its tie-in
    point actually touches geometrically -- *not* by ``HP_ID_vector``
    membership, which is cumulative up the hierarchy (a trunk section's
    vector lists every heat pump behind it, including ones served through a
    side branch that never physically touches the trunk's own line).

    Parameters
    ----------
    topology_geojson_path : str
        Path to ``pipe_topology.geojson`` (the geometry-carrying sibling of
        the ``.dat`` pythermonet actually dimensions from).
    hydraulic : pythermonet.dimensioning.hydraulic_result.HydraulicResult
        The mode-independent hydraulic dimensioning result -- supplies
        diameter/flow per ``Section``, positionally aligned to
        ``hydraulic.network.names_trace``.
    brine : pythermonet.core.heat_carrier.HeatCarrier
        Used to convert volumetric to mass flow rate.
    borefield_connection_point : QgsPointXY or None
        The BHE field's connection-node coordinate (the borehole flagged
        ``is_connection_node`` in Source Placement's ``borefield.geojson``),
        already reprojected into the topology layer's CRS. `None` skips
        anchoring the network to the borefield.
    topology_crs_authid : str
        CRS authid of the topology layer, only used for the feedback message
        if the connection-node snap fails.
    feedback : QgsProcessingFeedback
        For a non-fatal warning if the borefield connection node doesn't
        snap to any main-pipe section's end.

    Returns
    -------
    list of dict
        One entry per fine pipe sub-segment.

    Raises
    ------
    QgsProcessingException
        If the topology GeoJSON can't be loaded, or a service pipe's tie-in
        point doesn't lie on any main-pipe section within tolerance.

    """
    layer = QgsVectorLayer(topology_geojson_path, "pipe_topology", "ogr")
    if not layer.isValid():
        raise QgsProcessingException(f"Could not load topology GeoJSON: {topology_geojson_path}")

    features_by_section = {f["Section"]: f for f in layer.getFeatures()}

    names_trace = list(hydraulic.network.names_trace)
    index_by_section = {name: i for i, name in enumerate(names_trace)}

    main_sections = [n for n in names_trace if n.startswith("Pipe_branch_")]
    service_sections = [n for n in names_trace if n.startswith("Service_pipe_")]

    def _mass_flow_peak(i: int) -> float:
        return float(hydraulic.volume_flow_rates_peak_heating[i]) * float(brine.density)

    main_geometries = {
        name: _single_line_geometry(features_by_section[name].geometry())
        for name in main_sections
    }

    # Attribute each service pipe to the main-pipe section its tie-in point
    # actually touches.
    tie_ins_by_main_section: dict[str, list[tuple[str, float]]] = {name: [] for name in main_sections}
    for name in service_sections:
        feature = features_by_section[name]
        hp_id = feature["HP_ID_vector"].strip()
        _, tie_in_point = _line_endpoints(feature.geometry())
        target = _nearest_section_within_tolerance(tie_in_point, main_geometries)
        if target is None:
            raise QgsProcessingException(
                f"Service pipe '{name}' doesn't tie into any main-pipe "
                f"section within {_CONNECTION_NODE_SNAP_TOLERANCE_M} m."
            )
        tie_ins_by_main_section[target].append(
            (hp_id, _tie_in_fraction(main_geometries[target], tie_in_point))
        )

    # Detect main-pipe-to-main-pipe junctions: a section is a "child" of
    # whichever other section's line one of its own endpoints touches.
    # Assumes a tree-shaped network (each section has at most one parent);
    # doesn't detect or reject a loop/mesh.
    parent_of: dict[str, tuple[str, QgsPointXY]] = {}
    for name in main_sections:
        start_pt, end_pt = _line_endpoints(features_by_section[name].geometry())
        for other in main_sections:
            if other == name:
                continue
            for pt in (start_pt, end_pt):
                if main_geometries[other].distance(QgsGeometry.fromPointXY(pt)) <= _CONNECTION_NODE_SNAP_TOLERANCE_M:
                    parent_of[name] = (other, pt)
                    break
            if name in parent_of:
                break

    for child, (parent, touching_pt) in parent_of.items():
        tie_ins_by_main_section[parent].append((
            f"{_MAIN_JUNCTION_KEY_PREFIX}{child}",
            _tie_in_fraction(main_geometries[parent], touching_pt),
        ))

    per_section_segments: dict[str, list[dict]] = {}
    hp_tie_in_node: dict[str, str] = {}
    junction_node_for_child: dict[str, str] = {}
    for name in main_sections:
        length_authoritative = float(features_by_section[name]["Trace_(m)"])
        segments, node_by_key = _fine_segments_from_fractions(
            tie_ins_by_main_section[name], name, length_authoritative
        )
        per_section_segments[name] = segments
        for key, node in node_by_key.items():
            if key.startswith(_MAIN_JUNCTION_KEY_PREFIX):
                junction_node_for_child[key[len(_MAIN_JUNCTION_KEY_PREFIX):]] = node
            else:
                hp_tie_in_node[key] = node

    # Rename map applied globally at the end, rather than mutating
    # segments[0]/segments[-1] directly -- a tie-in (a heat pump, or another
    # branch's junction point) can coincide exactly with a section's own
    # endpoint (confirmed against real data: a heat pump tying in exactly at
    # the borefield connection point). Direct mutation would rename the
    # segment's node but leave hp_tie_in_node pointing at the old, now
    # nowhere-referenced node id -- a silently disconnected graph.
    rename: dict[str, str] = {}

    def _resolve(node_id: str) -> str:
        seen = set()
        while node_id in rename and node_id not in seen:
            seen.add(node_id)
            node_id = rename[node_id]
        return node_id

    for child, (parent, touching_pt) in parent_of.items():
        start_pt, _end_pt = _line_endpoints(features_by_section[child].geometry())
        child_own_node = (
            per_section_segments[child][0]["start_node"]
            if touching_pt == start_pt
            else per_section_segments[child][-1]["end_node"]
        )
        rename[child_own_node] = junction_node_for_child[child]

    if borefield_connection_point is not None:
        anchored = False
        for name in main_sections:
            start_pt, end_pt = _line_endpoints(features_by_section[name].geometry())
            segments = per_section_segments[name]
            if start_pt.distance(borefield_connection_point) <= _CONNECTION_NODE_SNAP_TOLERANCE_M:
                rename[segments[0]["start_node"]] = "borefield_connection"
                anchored = True
                break
            if end_pt.distance(borefield_connection_point) <= _CONNECTION_NODE_SNAP_TOLERANCE_M:
                rename[segments[-1]["end_node"]] = "borefield_connection"
                anchored = True
                break
        if not anchored:
            feedback.reportError(
                f"Borefield connection node (CRS {topology_crs_authid}) doesn't "
                f"snap to any main-pipe section's end within "
                f"{_CONNECTION_NODE_SNAP_TOLERANCE_M} m -- the pipe network and "
                "the borefield are not connected in the exported node graph."
            )

    for segments in per_section_segments.values():
        for segment in segments:
            segment["start_node"] = _resolve(segment["start_node"])
            segment["end_node"] = _resolve(segment["end_node"])
    hp_tie_in_node = {key: _resolve(node) for key, node in hp_tie_in_node.items()}

    pipes: list[dict] = []
    for name in main_sections:
        i = index_by_section[name]
        diameter_outer = float(hydraulic.diameters_outer[i])
        diameter_inner = float(hydraulic.diameters_inner[i])
        flow_rate_peak = _mass_flow_peak(i)
        for idx, segment in enumerate(per_section_segments[name]):
            pipes.append({
                "id": f"{name}_seg{idx}",
                "pipe_type": "main",
                "start_node": segment["start_node"],
                "end_node": segment["end_node"],
                "length": segment["length"],
                "diameter_outer": diameter_outer,
                "diameter_inner": diameter_inner,
                "flow_rate_peak": flow_rate_peak,
            })

    for name in service_sections:
        feature = features_by_section[name]
        i = index_by_section[name]
        hp_id = feature["HP_ID_vector"].strip()
        pipes.append({
            "id": name,
            "pipe_type": "connection",
            "start_node": hp_tie_in_node.get(hp_id, f"{name}_main_end"),
            "end_node": f"HP_{hp_id}",
            "length": float(feature["Trace_(m)"]),
            "diameter_outer": float(hydraulic.diameters_outer[i]),
            "diameter_inner": float(hydraulic.diameters_inner[i]),
            "flow_rate_peak": _mass_flow_peak(i),
        })

    return pipes


def build_boreholes_export(borefield_input, coordinates: list[list[float]], result) -> dict:
    """Build the ``boreholes.json`` content.

    Parameters
    ----------
    borefield_input : pythermonet.components.vhe_field.BorefieldCoordinatesInput
        Supplies each borehole's ``id``.
    coordinates : list of list of float
        Localized ``[x, y]`` coordinates, one per borehole, from
        ``pythermonet.components.localize_borefield_coordinates`` --
        positionally aligned to ``borefield_input.ids``.
    result : pythermonet.dimensioning.BHE.bhe_workflow.BHEWorkflowResult
        The BHE sizing result -- supplies the field-wide computed values.

    Returns
    -------
    dict
        ``{"boreholes": [...], "length", "thermal_resistance_heating",
        "thermal_resistance_cooling", "governing_mode",
        "mass_flow_rate_peak_heating", "mass_flow_rate_peak_cooling"}``.

    """
    boreholes = [
        {"id": bh_id, "x": float(xy[0]), "y": float(xy[1])}
        for bh_id, xy in zip(borefield_input.ids, coordinates)
    ]
    sizing = result.sizing
    return {
        "boreholes": boreholes,
        "length": float(sizing.length_element),                                    # m
        "thermal_resistance_heating": float(sizing.thermal_resistance_heating),     # K.m/W
        "thermal_resistance_cooling": (
            float(sizing.thermal_resistance_cooling)
            if sizing.thermal_resistance_cooling is not None else None
        ),                                                                          # K.m/W
        "governing_mode": sizing.governing_mode,
        "mass_flow_rate_peak_heating": float(result.mass_flow_rate_peak_bhe_heating),  # kg/s
        "mass_flow_rate_peak_cooling": (
            float(result.mass_flow_rate_peak_bhe_cooling)
            if result.mass_flow_rate_peak_bhe_cooling is not None else None
        ),                                                                          # kg/s
    }


def build_trenches_export(trenches_geojson_path: str, result) -> dict:
    """Build the ``trenches.json`` content for an HHE project.

    Reads ``trenches.geojson`` directly -- no pythermonet bridge, unlike
    BHE's borefield coordinates -- because pythermonet's HHE sizing never
    consumes per-trench positions at all (a lumped/aggregate field model,
    only `n_loops` and soil properties matter to it; see the design
    session). Coordinates are localized (relative to the connection-node
    trench's start, not real-world absolute) by hand here, matching the
    same "relative distances only" principle
    `localize_borefield_coordinates` applies for BHE.

    Parameters
    ----------
    trenches_geojson_path : str
        Path to Source Placement's ``trenches.geojson``.
    result : pythermonet.dimensioning.HHE.hhe_workflow.HHEWorkflowResult
        The HHE sizing result -- supplies the field-wide computed values.

    Returns
    -------
    dict
        ``{"trenches": [...], "length", "thermal_resistance_heating",
        "thermal_resistance_cooling", "governing_mode",
        "mass_flow_rate_peak_heating", "mass_flow_rate_peak_cooling"}``.

    Raises
    ------
    QgsProcessingException
        If the GeoJSON can't be loaded, or no trench is flagged
        ``is_connection_node``.

    """
    layer = QgsVectorLayer(trenches_geojson_path, "trenches", "ogr")
    if not layer.isValid():
        raise QgsProcessingException(f"Could not load trenches GeoJSON: {trenches_geojson_path}")

    features = list(layer.getFeatures())
    connection_feature = next((f for f in features if f["is_connection_node"]), None)
    if connection_feature is None:
        raise QgsProcessingException(
            f"No trench in {trenches_geojson_path} is flagged 'is_connection_node'."
        )
    origin, _ = _line_endpoints(connection_feature.geometry())

    trenches = []
    for feature in features:
        start, end = _line_endpoints(feature.geometry())
        trenches.append({
            "id": feature["id"],
            "start_x": float(start.x() - origin.x()),   # m, relative to connection node
            "start_y": float(start.y() - origin.y()),   # m, relative to connection node
            "end_x": float(end.x() - origin.x()),        # m, relative to connection node
            "end_y": float(end.y() - origin.y()),         # m, relative to connection node
        })

    sizing = result.sizing
    return {
        "trenches": trenches,
        "length": float(sizing.length_element),                                    # m
        "thermal_resistance_heating": float(sizing.thermal_resistance_heating),     # K.m/W
        "thermal_resistance_cooling": (
            float(sizing.thermal_resistance_cooling)
            if sizing.thermal_resistance_cooling is not None else None
        ),                                                                          # K.m/W
        "governing_mode": sizing.governing_mode,
        "mass_flow_rate_peak_heating": float(result.mass_flow_rate_peak_hhe_heating),  # kg/s
        "mass_flow_rate_peak_cooling": (
            float(result.mass_flow_rate_peak_hhe_cooling)
            if result.mass_flow_rate_peak_hhe_cooling is not None else None
        ),                                                                          # kg/s
    }


def build_settings_passthrough(settings_path: str | Path) -> dict:
    """Read the project's settings file, dropping ``qthermonet_``-prefixed sections.

    Deliberately the *full* remaining settings content, not a curated subset
    -- see the design session for why (avoids iterating field-by-field; the
    user will tell Alessandro directly what to use, and trim later once he's
    tested things).

    Parameters
    ----------
    settings_path : str or Path
        Path to the project's settings JSON file.

    Returns
    -------
    dict
        The raw settings content, minus any top-level key starting with
        ``qthermonet_`` (QThermonet's own reserved sections, meaningless
        outside QThermonet -- see ``utils.write_settings_json``).

    """
    raw = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("qthermonet_")}


def connection_point_in_topology_crs(
    source_geojson_path: str, topology_crs_authid: str
) -> QgsPointXY | None:
    """Find the flagged connection-node feature, reprojected into the topology CRS.

    Works for both a BHE ``borefield.geojson`` (point features) and an HHE
    ``trenches.geojson`` (line features, connection node = trench 0's
    ``connection_node_end`` -- taken as its start, since that's the only
    value Source Placement's generator ever writes) -- both use the same
    ``is_connection_node`` flag.

    Parameters
    ----------
    source_geojson_path : str
        Path to Source Placement's ``borefield.geojson`` or ``trenches.geojson``.
    topology_crs_authid : str
        CRS authid (e.g. ``"EPSG:3857"``) to reproject into.

    Returns
    -------
    QgsPointXY or None
        `None` if no feature is flagged ``is_connection_node``.

    """
    layer = QgsVectorLayer(source_geojson_path, "connection_source", "ogr")
    if not layer.isValid():
        raise QgsProcessingException(f"Could not load GeoJSON: {source_geojson_path}")

    connection_feature = next(
        (f for f in layer.getFeatures() if f["is_connection_node"]), None
    )
    if connection_feature is None:
        return None

    geometry = connection_feature.geometry()
    point = geometry.asPoint() if geometry.type() == QgsWkbTypes.PointGeometry else _line_endpoints(geometry)[0]
    target_crs = QgsCoordinateReferenceSystem(topology_crs_authid)
    transform = QgsCoordinateTransform(layer.crs(), target_crs, QgsProject.instance())
    return transform.transform(point)


def write_modelica_export(
    output_dir: str | Path,
    heatpumps: list[dict],
    pipes: list[dict],
    settings_passthrough: dict,
    boreholes: dict | None = None,
    trenches: dict | None = None,
) -> None:
    """Write the Modelica-export files into `output_dir`.

    Exactly one of `boreholes` (BHE) or `trenches` (HHE) must be given --
    which one determines both the ground-field file's name
    (``boreholes.json``/``trenches.json``) and the settings passthrough's
    (``settings_bhe.json``/``settings_hhe.json``, matching the source
    settings file's own naming convention).

    Parameters
    ----------
    output_dir : str or Path
        Destination folder -- created if it doesn't exist.
    heatpumps : list of dict
        From `build_heatpumps_export`.
    pipes : list of dict
        From `build_pipes_export`.
    settings_passthrough : dict
        From `build_settings_passthrough`.
    boreholes : dict or None
        From `build_boreholes_export`, for a BHE project.
    trenches : dict or None
        From `build_trenches_export`, for an HHE project.

    Raises
    ------
    ValueError
        If neither or both of `boreholes`/`trenches` are given.

    """
    if (boreholes is None) == (trenches is None):
        raise ValueError("Exactly one of `boreholes` or `trenches` must be given.")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "heatpumps.json").write_bytes(
        (json.dumps(heatpumps, indent=2) + "\n").encode("utf-8")
    )
    (out / "pipes.json").write_bytes(
        (json.dumps(pipes, indent=2) + "\n").encode("utf-8")
    )
    if boreholes is not None:
        (out / "boreholes.json").write_bytes(
            (json.dumps(boreholes, indent=2) + "\n").encode("utf-8")
        )
        settings_name = "settings_bhe.json"
    else:
        (out / "trenches.json").write_bytes(
            (json.dumps(trenches, indent=2) + "\n").encode("utf-8")
        )
        settings_name = "settings_hhe.json"
    (out / settings_name).write_bytes(
        (json.dumps(settings_passthrough, indent=2) + "\n").encode("utf-8")
    )
