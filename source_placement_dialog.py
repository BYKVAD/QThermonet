# -*- coding: utf-8 -*-
"""Interactive source-placement window for QThermonet.

Lets a user click a connection node on an embedded map (optionally snapped
to an existing layer, e.g. the pipe topology network) and generate a
source field, exporting it as a GeoJSON layer:

- **BHE**: a rectangular grid of boreholes, exported as a GeoJSON point
  layer that ``full_dimensioning_algorithm.py`` converts to pythermonet's
  WKT/EWKT ``.dat`` format at call time.
- **HHE**: a single row of parallel trenches, exported as a GeoJSON line
  layer. Pythermonet's HHE sizing is entirely non-spatial (reads
  ``n_pipes_parallel``/``pipe_spacing``/``burial_depth``/``length_element``
  from the settings file; no coordinate input at all), so this half of the
  tool is a pure QGIS-side visualization/planning aid -- never consumed by
  ``full_dimensioning_algorithm.py``.

See ``claude/handoffs/handoff-interactive-borefield-placement.md`` (BHE) and
``claude/handoffs/2026-09-17-hhe-source-placement-backlog.md`` (HHE) for the
full confirmed designs this implements.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from pythermonet.input import load_settings

from . import utils
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsPointXY,
    QgsProject,
    QgsSnappingConfig,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import (
    QgsMapCanvas,
    QgsMapLayerComboBox,
    QgsMapTool,
    QgsMapToolPan,
    QgsRubberBand,
    QgsVertexMarker,
)
from qgis.PyQt.QtCore import QMetaType, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

#: Default rectangular-grid parameters shown when the dialog opens.
_DEFAULT_N_ROWS = 3
_DEFAULT_N_COLS = 1
_DEFAULT_SPACING = 15.0  # m
_DEFAULT_ROTATION = 0.0  # degrees, CCW from "rows west->east, columns north->south"

#: The reserved settings-file key prefix `load_settings()` (pythermonet) skips
#: entirely -- see `pythermonet/src/pythermonet/input/load_settings.py`.
_QTHERMONET_HHE_KEY = "qthermonet_hhe_settings"

_RUBBER_BAND_COLOR = QColor("#0067c0")
_SNAP_TOLERANCE_PX = 12


@dataclass(frozen=True)
class _GridParameters:
    """One rectangular-grid layout, as entered in the parameter panel.

    Attributes
    ----------
    n_rows : int
        Number of boreholes along the row axis (west->east at rotation 0).
    n_cols : int
        Number of boreholes along the column axis (north->south at
        rotation 0).
    spacing_row : float
        Distance between adjacent boreholes along the row axis. # m
    spacing_col : float
        Distance between adjacent boreholes along the column axis. # m
    rotation : float
        Counter-clockwise rotation of the whole grid from its default
        orientation (rows west->east, columns north->south). # degrees

    """

    n_rows: int
    n_cols: int
    spacing_row: float
    spacing_col: float
    rotation: float


def _generate_grid_points(origin: QgsPointXY, grid: _GridParameters) -> list[QgsPointXY]:
    """Generate a rectangular grid of points anchored at (row 0, col 0).

    At `grid.rotation` == 0, the row axis runs west->east and the column
    axis runs north->south; positive rotation is counter-clockwise. The
    first point returned is always (row 0, col 0) -- the connection-node
    anchor, per the confirmed design (see module docstring).

    Parameters
    ----------
    origin : QgsPointXY
        The connection node -- world coordinates of the (row 0, col 0)
        corner, in the map canvas's CRS.
    grid : _GridParameters
        Row/column counts, independent row/column spacing, and rotation.

    Returns
    -------
    list of QgsPointXY
        `grid.n_rows * grid.n_cols` points, row-major, first entry ==
        `origin`.

    """
    theta = math.radians(grid.rotation)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    points = []
    for row in range(grid.n_rows):
        for col in range(grid.n_cols):
            local_x = row * grid.spacing_row
            local_y = -col * grid.spacing_col
            x = origin.x() + local_x * cos_t - local_y * sin_t
            y = origin.y() + local_x * sin_t + local_y * cos_t
            points.append(QgsPointXY(x, y))
    return points


@dataclass(frozen=True)
class _TrenchParameters:
    """One HHE trench-field layout.

    Attributes
    ----------
    n_pipes_parallel : int
        Number of parallel trenches -- settings-owned, read-only in the UI.
    pipe_spacing : float
        Distance between adjacent trenches. # m, settings-owned
    length_element : float
        Length of each trench. # m, user-editable, seeded from settings
    rotation : float
        Counter-clockwise rotation from the default orientation (trenches
        west->east, offset axis north->south). # degrees
    mirror_left : bool
        `False` (default): trenches extend east of the offset axis (before
        rotation). `True`: extend west instead -- a mirror reflection, not
        reproducible by `rotation` alone.

    """

    n_pipes_parallel: int
    pipe_spacing: float
    length_element: float
    rotation: float
    mirror_left: bool


def _generate_trench_lines(
    origin: QgsPointXY, trenches: _TrenchParameters
) -> list[tuple[QgsPointXY, QgsPointXY]]:
    """Generate a single row of parallel trench lines anchored at trench 0.

    Trench 0's start point is always `origin` -- the connection node. Reuses
    the same rotation convention as `_generate_grid_points`.

    Parameters
    ----------
    origin : QgsPointXY
        The connection node, in the map canvas's CRS.
    trenches : _TrenchParameters
        Trench count/spacing/length (settings-owned) plus rotation/handedness
        (dialog-owned).

    Returns
    -------
    list of (QgsPointXY, QgsPointXY)
        `trenches.n_pipes_parallel` (start, end) pairs, trench 0's start ==
        `origin`.

    """
    theta = math.radians(trenches.rotation)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    sign = -1.0 if trenches.mirror_left else 1.0

    lines = []
    for i in range(trenches.n_pipes_parallel):
        offset_y = -i * trenches.pipe_spacing
        start = QgsPointXY(
            origin.x() + (-offset_y) * sin_t,
            origin.y() + offset_y * cos_t,
        )
        local_x1 = sign * trenches.length_element
        end = QgsPointXY(
            origin.x() + local_x1 * cos_t - offset_y * sin_t,
            origin.y() + local_x1 * sin_t + offset_y * cos_t,
        )
        lines.append((start, end))
    return lines


def _derive_connection_node_and_rotation(
    flagged_start: QgsPointXY, flagged_end: QgsPointXY, mirror_left: bool
) -> tuple[QgsPointXY, float]:
    """Recover the connection node and rotation from an existing flagged trench.

    Connection node and rotation are never separately persisted (see the
    confirmed design) -- they're re-derived from a trench layer's own current
    geometry, which stays correct even if a user hand-edits the layer, unlike
    a separately-stored snapshot that could go stale. `mirror_left` alone
    needs to be stored, since a single trench's direction vector can't
    distinguish rotation from handedness when `n_pipes_parallel == 1`.

    Parameters
    ----------
    flagged_start : QgsPointXY
        The connection-node end of the flagged trench (per its
        `connection_node_end` attribute) -- this *is* the connection node.
    flagged_end : QgsPointXY
        The flagged trench's other end.
    mirror_left : bool
        The stored handedness, needed to disambiguate rotation.

    Returns
    -------
    tuple of (QgsPointXY, float)
        `(connection_node, rotation_degrees)`.

    """
    dx = flagged_end.x() - flagged_start.x()
    dy = flagged_end.y() - flagged_start.y()
    angle = math.degrees(math.atan2(dy, dx))
    rotation = (angle - 180.0) if mirror_left else angle
    return flagged_start, rotation


def _write_trench_geojson(
    path: str, lines: list[tuple[QgsPointXY, QgsPointXY]], crs: QgsCoordinateReferenceSystem
) -> None:
    """Write a list of trench lines as a GeoJSON, trench 0 flagged as the connection node.

    Parameters
    ----------
    path : str
        Output `.geojson` path.
    lines : list of (QgsPointXY, QgsPointXY)
        Trench (start, end) pairs, as from `_generate_trench_lines`.
    crs : QgsCoordinateReferenceSystem
        CRS the coordinates are already in.

    Raises
    ------
    OSError
        If the GeoJSON file can't be created.

    """
    fields = QgsFields()
    fields.append(QgsField("id", QMetaType.Type.QString))
    fields.append(QgsField("is_connection_node", QMetaType.Type.Bool))
    fields.append(QgsField("connection_node_end", QMetaType.Type.QString))

    writer = QgsVectorFileWriter(path, "UTF-8", fields, QgsWkbTypes.LineString, crs, "GeoJSON")
    if writer.hasError() != QgsVectorFileWriter.NoError:
        raise OSError(f"Could not write GeoJSON file: {writer.errorMessage()}")

    for index, (start, end) in enumerate(lines):
        feature = QgsFeature(fields)
        feature.setGeometry(QgsGeometry.fromPolylineXY([start, end]))
        feature["id"] = f"TR{index}"
        feature["is_connection_node"] = index == 0
        feature["connection_node_end"] = "start"
        writer.addFeature(feature)
    del writer


def write_settings_json(path: str | Path, raw: dict) -> None:
    """Validate and atomically write a settings file's raw JSON.

    The one place in QThermonet that actually writes a settings file to
    disk: `SettingsEditorDialog._on_save`, Source Placement's HHE export,
    and Full Dimensioning's computed-length write-back all go through this
    (directly, or via :func:`update_settings_fields`). Writes `raw` to a
    temp file, validates it with `pythermonet.input.load_settings` (the
    real file is never touched if invalid), then atomically replaces
    `path`. Deliberately never goes through `pythermonet.output.save_settings`
    (which reconstructs the file purely from typed domain objects and would
    silently drop anything -- like QThermonet's reserved `qthermonet_`-
    prefixed sections -- not represented in them); writing raw JSON directly
    naturally carries forward everything this call doesn't explicitly touch.

    Parameters
    ----------
    path : str or Path
        Settings file path.
    raw : dict
        The complete settings JSON to write.

    Raises
    ------
    ValueError
        If `raw` doesn't validate against pythermonet's schema. The real
        file is left untouched.

    """
    p = Path(path)
    temp_path = p.with_suffix(p.suffix + ".tmp")
    temp_path.write_bytes((json.dumps(raw, indent=2) + "\n").encode("utf-8"))
    try:
        load_settings(temp_path)
    except ValueError:
        os.remove(temp_path)
        raise
    os.replace(temp_path, p)


def update_settings_fields(path: str | Path, updates: dict[str, dict[str, object]]) -> None:
    """Update specific field values in a settings file, leaving everything else untouched.

    Parameters
    ----------
    path : str or Path
        Settings file path.
    updates : dict of str to dict of str to object
        `{role: {field_name: new_value}}` -- only these fields change; every
        other role/field, and any `qthermonet_`-prefixed section, is
        carried forward unchanged.

    Raises
    ------
    ValueError
        Propagated from :func:`write_settings_json` if the result doesn't
        validate.

    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    for role, fields in updates.items():
        for field_name, value in fields.items():
            raw[role]["values"][field_name]["value"] = value
    write_settings_json(path, raw)


def _normalize_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def refresh_hhe_trench_layer(settings_path: str) -> None:
    """Regenerate an HHE trench GeoJSON to match a just-changed settings file.

    Called only from two known trigger points -- `SettingsEditorDialog._on_save`
    and the HHE branch of `full_dimensioning_algorithm.py` after a run --
    never from a filesystem watcher. No-op if `settings_path` has no
    `qthermonet_hhe_settings` section, or if the layer it names isn't
    currently loaded in the project: this deliberately does not maintain a
    registry of unopened files (see the confirmed design in
    `claude/handoffs/2026-09-17-hhe-source-placement-backlog.md`).

    Parameters
    ----------
    settings_path : str
        Path to the settings file that was just saved, or just used for an
        HHE dimensioning run.

    """
    try:
        with open(settings_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return

    section = raw.get(_QTHERMONET_HHE_KEY)
    if not section or not section.get("geojson_path"):
        return
    geojson_path = section["geojson_path"]
    mirror_left = bool(section.get("mirror_left", False))

    target_layer = None
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and _normalize_path(layer.source()) == _normalize_path(
            geojson_path
        ):
            target_layer = layer
            break
    if target_layer is None:
        return

    flagged_start = flagged_end = None
    for feature in target_layer.getFeatures():
        if feature["is_connection_node"]:
            polyline = feature.geometry().asPolyline()
            if feature["connection_node_end"] == "start":
                flagged_start, flagged_end = polyline[0], polyline[-1]
            else:
                flagged_start, flagged_end = polyline[-1], polyline[0]
            break
    if flagged_start is None:
        return

    connection_node, rotation = _derive_connection_node_and_rotation(
        flagged_start, flagged_end, mirror_left
    )

    try:
        settings = load_settings(settings_path)
        hhe_params = settings["hhe_field_parameters"]
    except (FileNotFoundError, ValueError, KeyError):
        return

    trenches = _TrenchParameters(
        n_pipes_parallel=hhe_params.n_pipes_parallel,
        pipe_spacing=hhe_params.pipe_spacing,
        length_element=hhe_params.length_element,
        rotation=rotation,
        mirror_left=mirror_left,
    )
    lines = _generate_trench_lines(connection_node, trenches)

    # Rewriting the file at the OS level (as `_write_trench_geojson` does for
    # a fresh export) fails here on Windows: the loaded layer holds the file
    # open, so `QgsVectorFileWriter` can't delete-then-recreate it
    # ("Permission denied"). Updating the already-loaded layer's own data
    # provider in place (truncate + re-add) goes through QGIS's own OGR
    # write path instead, which handles this correctly.
    provider = target_layer.dataProvider()
    provider.truncate()
    new_features = []
    for index, (start, end) in enumerate(lines):
        feature = QgsFeature(target_layer.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY([start, end]))
        feature["id"] = f"TR{index}"
        feature["is_connection_node"] = index == 0
        feature["connection_node_end"] = "start"
        new_features.append(feature)
    provider.addFeatures(new_features)
    target_layer.updateExtents()
    target_layer.triggerRepaint()


class _ConnectionNodeTool(QgsMapTool):
    """Map tool for picking the connection node, with optional snapping.

    Emits `pointPicked` with the picked point on click. When snapping is
    enabled and a snap layer is set, the click snaps onto that layer's
    nearest vertex/segment (shown live via a circular marker while the
    mouse moves); otherwise the raw map-coordinate click is used as-is.
    """

    pointPicked = pyqtSignal(QgsPointXY)

    def __init__(self, canvas: QgsMapCanvas) -> None:
        super().__init__(canvas)
        self._snap_layer: QgsVectorLayer | None = None
        self._snap_enabled: bool = True

        self._marker = QgsVertexMarker(canvas)
        self._marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
        self._marker.setColor(_RUBBER_BAND_COLOR)
        self._marker.setPenWidth(2)
        self._marker.setIconSize(12)
        self._marker.hide()

        self._apply_snap_config()

    def set_snap_layer(self, layer: QgsVectorLayer | None) -> None:
        """Set which layer's vertices/segments subsequent clicks snap to.

        Parameters
        ----------
        layer : QgsVectorLayer | None
            Layer to snap to, or `None` if no layer is selected.

        """
        self._snap_layer = layer
        self._apply_snap_config()

    def set_snap_enabled(self, enabled: bool) -> None:
        """Toggle snapping on/off without losing the selected snap layer.

        Parameters
        ----------
        enabled : bool
            Whether clicks should snap to `self._snap_layer` at all.

        """
        self._snap_enabled = enabled
        self._apply_snap_config()

    def _apply_snap_config(self) -> None:
        config = QgsSnappingConfig()
        if self._snap_enabled and self._snap_layer is not None:
            config.setEnabled(True)
            config.setMode(QgsSnappingConfig.SnappingMode.AdvancedConfiguration)
            settings = QgsSnappingConfig.IndividualLayerSettings(
                True,
                Qgis.SnappingType.Vertex | Qgis.SnappingType.Segment,
                _SNAP_TOLERANCE_PX,
                Qgis.MapToolUnit.Pixels,
                0,
                0,
            )
            config.setIndividualLayerSettings(self._snap_layer, settings)
        else:
            config.setEnabled(False)
        self.canvas().snappingUtils().setConfig(config)

    def canvasMoveEvent(self, event) -> None:  # noqa: D102 (Qt override, not new public API)
        match = self.canvas().snappingUtils().snapToMap(event.pos())
        if match.isValid():
            self._marker.setCenter(match.point())
            self._marker.show()
        else:
            self._marker.hide()

    def canvasReleaseEvent(self, event) -> None:  # noqa: D102 (Qt override, not new public API)
        match = self.canvas().snappingUtils().snapToMap(event.pos())
        point = match.point() if match.isValid() else event.mapPoint()
        self.pointPicked.emit(point)

    def deactivate(self) -> None:  # noqa: D102 (Qt override, not new public API)
        self._marker.hide()
        super().deactivate()


class SourcePlacementDialog(QDialog):
    """Window for interactively placing a project's BHE/HHE source field.

    Embeds a `QgsMapCanvas`: the user optionally picks a layer to snap to
    (e.g. the pipe topology network), clicks a connection node, then either:

    - **BHE**: a rectangular grid of boreholes is generated and live-previewed
      from row/column count, independent row/column spacing, and rotation.
      Export writes a GeoJSON point layer (`id` + `is_connection_node`).
    - **HHE**: a single row of parallel trenches is generated from a settings
      file's `n_pipes_parallel`/`pipe_spacing` (read-only) plus an editable
      `length_element` and dialog-owned rotation/handedness. Export writes a
      GeoJSON line layer (`id` + `is_connection_node` + `connection_node_end`)
      and writes the (possibly adjusted) length back into the settings file.

    Source type (BHE/HHE) is a manual toggle.

    Parameters
    ----------
    iface : QgisInterface
        The running QGIS interface, used to seed the embedded canvas with
        the current project layers/extent/CRS for context.
    parent : QWidget | None
        Parent widget, typically the QGIS main window.

    """

    def __init__(self, iface, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("QThermonet — Source Placement")
        self.resize(1000, 700)

        self._iface = iface
        self._mode: str = "BHE"
        self._connection_point: QgsPointXY | None = None
        self._points: list[QgsPointXY] = []
        self._trench_lines: list[tuple[QgsPointXY, QgsPointXY]] = []
        self._base_layers: list[QgsVectorLayer] = []
        self._hhe_settings_path: str | None = None

        self._build_ui()
        self._sync_canvas_to_project()
        self._set_mode("BHE")

    # -- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        outer.addWidget(self._build_mode_row())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_canvas())
        splitter.addWidget(self._build_side_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, stretch=1)

        self._status_label = QLabel("Click “Set Connection Node”, then click the map.")
        self._status_label.setStyleSheet("color: #6b6b6b; font-size: 11px;")
        outer.addWidget(self._status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _build_mode_row(self) -> QWidget:
        row = QHBoxLayout()
        row.addWidget(QLabel("Source type:"))
        self._bhe_radio = QRadioButton("BHE")
        self._bhe_radio.setChecked(True)
        self._bhe_radio.toggled.connect(lambda checked: checked and self._set_mode("BHE"))
        row.addWidget(self._bhe_radio)
        self._hhe_radio = QRadioButton("HHE")
        self._hhe_radio.toggled.connect(lambda checked: checked and self._set_mode("HHE"))
        row.addWidget(self._hhe_radio)
        row.addStretch(1)

        container = QWidget()
        container.setLayout(row)
        return container

    def _build_canvas(self) -> QgsMapCanvas:
        self._canvas = QgsMapCanvas()
        self._canvas.setCanvasColor(Qt.GlobalColor.white)

        self._rubber_band = QgsRubberBand(self._canvas, QgsWkbTypes.PointGeometry)
        self._rubber_band.setColor(_RUBBER_BAND_COLOR)
        self._rubber_band.setIconSize(12)

        self._rubber_band_hhe = QgsRubberBand(self._canvas, QgsWkbTypes.LineGeometry)
        self._rubber_band_hhe.setColor(_RUBBER_BAND_COLOR)
        self._rubber_band_hhe.setWidth(2)

        self._pan_tool = QgsMapToolPan(self._canvas)
        self._point_tool = _ConnectionNodeTool(self._canvas)
        self._point_tool.pointPicked.connect(self._on_point_picked)
        self._canvas.setMapTool(self._pan_tool)

        return self._canvas

    def _build_side_panel(self) -> QWidget:
        panel = QVBoxLayout()

        snap_row = QHBoxLayout()
        self._snap_enabled_check = QCheckBox("Snap to:")
        self._snap_enabled_check.setChecked(True)
        self._snap_enabled_check.toggled.connect(self._on_snap_enabled_toggled)
        snap_row.addWidget(self._snap_enabled_check)
        self._snap_layer_combo = QgsMapLayerComboBox()
        self._snap_layer_combo.setFilters(
            QgsMapLayerProxyModel.Filter.PointLayer | QgsMapLayerProxyModel.Filter.LineLayer
        )
        self._snap_layer_combo.setAllowEmptyLayer(True)
        self._snap_layer_combo.layerChanged.connect(self._on_snap_layer_changed)
        snap_row.addWidget(self._snap_layer_combo)
        panel.addLayout(snap_row)

        pick_button = QPushButton("Set Connection Node")
        pick_button.setCheckable(True)
        pick_button.toggled.connect(self._on_pick_node_toggled)
        self._pick_button = pick_button
        panel.addWidget(pick_button)

        self._bhe_panel = self._build_bhe_panel()
        panel.addWidget(self._bhe_panel)

        self._hhe_panel = self._build_hhe_panel()
        panel.addWidget(self._hhe_panel)

        panel.addStretch(1)

        self._export_button = QPushButton("Export Borefield…")
        self._export_button.clicked.connect(self._on_export)
        panel.addWidget(self._export_button)

        container = QWidget()
        container.setLayout(panel)

        # Sync the tool to the combo's initial state (usually "no layer").
        self._on_snap_layer_changed(self._snap_layer_combo.currentLayer())

        return container

    def _build_bhe_panel(self) -> QGroupBox:
        box = QGroupBox("Borefield grid")
        grid = QVBoxLayout()

        self._n_rows_spin = self._add_spin_row(grid, "Rows:", QSpinBox, 1, 500, _DEFAULT_N_ROWS)
        self._n_cols_spin = self._add_spin_row(grid, "Columns:", QSpinBox, 1, 500, _DEFAULT_N_COLS)
        self._spacing_row_spin = self._add_spin_row(
            grid, "Row spacing (m):", QDoubleSpinBox, 0.1, 1000.0, _DEFAULT_SPACING
        )
        self._spacing_col_spin = self._add_spin_row(
            grid, "Column spacing (m):", QDoubleSpinBox, 0.1, 1000.0, _DEFAULT_SPACING
        )
        self._rotation_spin = self._add_spin_row(
            grid, "Rotation, CCW (°):", QDoubleSpinBox, -360.0, 360.0, _DEFAULT_ROTATION
        )

        box.setLayout(grid)
        return box

    def _build_hhe_panel(self) -> QGroupBox:
        box = QGroupBox("Trench field")
        layout = QVBoxLayout()

        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("Settings file:"))
        self._hhe_settings_display = QLineEdit()
        self._hhe_settings_display.setReadOnly(True)
        settings_row.addWidget(self._hhe_settings_display)
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._on_browse_hhe_settings)
        settings_row.addWidget(browse_button)
        layout.addLayout(settings_row)

        self._hhe_n_pipes_label = self._add_display_row(layout, "N parallel pipes:")
        self._hhe_pipe_spacing_label = self._add_display_row(layout, "Pipe spacing (m):")
        self._hhe_burial_depth_label = self._add_display_row(layout, "Burial depth (m):")

        self._length_element_spin = self._add_spin_row(
            layout, "Trench length (m):", QDoubleSpinBox, 0.1, 10000.0, 100.0
        )
        self._length_element_spin.setEnabled(False)

        self._hhe_rotation_spin = self._add_spin_row(
            layout, "Rotation, CCW (°):", QDoubleSpinBox, -360.0, 360.0, _DEFAULT_ROTATION
        )

        self._mirror_left_check = QCheckBox("Draw trenches to the left")
        self._mirror_left_check.toggled.connect(self._regenerate_preview)
        layout.addWidget(self._mirror_left_check)

        box.setLayout(layout)
        return box

    def _add_display_row(self, layout: QVBoxLayout, label_text: str) -> QLabel:
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        value_label = QLabel("—")
        row.addWidget(value_label)
        row.addStretch(1)
        layout.addLayout(row)
        return value_label

    def _add_spin_row(self, layout: QVBoxLayout, label_text: str, spin_cls, minimum, maximum, default):
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        spin = spin_cls()
        spin.setRange(minimum, maximum)
        spin.setValue(default)
        spin.valueChanged.connect(self._regenerate_preview)
        row.addWidget(spin)
        layout.addLayout(row)
        return spin

    # -- Canvas / project sync --------------------------------------------

    def _sync_canvas_to_project(self) -> None:
        project = QgsProject.instance()
        self._canvas.setDestinationCrs(project.crs())

        main_canvas = self._iface.mapCanvas() if self._iface is not None else None
        if main_canvas is not None:
            # `main_canvas.layers()` reflects the layer panel's actual order
            # and visibility; `project.mapLayers().values()` is an arbitrary
            # (layer-ID insertion) order that can put an opaque layer (e.g.
            # an AOI polygon) on top of everything else, hiding the rest.
            self._base_layers = list(main_canvas.layers())
            self._canvas.setExtent(main_canvas.extent())
        else:
            self._base_layers = list(project.mapLayers().values())
        self._canvas.setLayers(self._base_layers)
        self._canvas.refresh()

    # -- Mode handling -----------------------------------------------------

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._bhe_panel.setVisible(mode == "BHE")
        self._hhe_panel.setVisible(mode == "HHE")
        self._export_button.setText("Export Borefield…" if mode == "BHE" else "Export Trenches…")
        self._regenerate_preview()

    # -- HHE settings-file link ---------------------------------------------

    def _on_browse_hhe_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open HHE Settings File", "", "Settings JSON (*.json)"
        )
        if not path:
            return

        try:
            settings = load_settings(path)
            hhe_params = settings["hhe_field_parameters"]
        except (FileNotFoundError, ValueError) as exc:
            QMessageBox.critical(self, "Can't open settings file", str(exc))
            return
        except KeyError:
            QMessageBox.critical(
                self,
                "Not an HHE settings file",
                "This settings file has no 'hhe_field_parameters' role.",
            )
            return

        self._hhe_settings_path = path
        self._hhe_settings_display.setText(path)
        self._refresh_hhe_display(hhe_params)
        self._length_element_spin.setValue(hhe_params.length_element)
        self._length_element_spin.setEnabled(True)

        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            section = raw.get(_QTHERMONET_HHE_KEY, {})
        except (OSError, json.JSONDecodeError):
            section = {}
        self._mirror_left_check.setChecked(bool(section.get("mirror_left", False)))

        self._regenerate_preview()

    def _refresh_hhe_display(self, hhe_params) -> None:
        self._hhe_n_pipes_label.setText(str(hhe_params.n_pipes_parallel))
        self._hhe_pipe_spacing_label.setText(f"{hhe_params.pipe_spacing:g}")
        self._hhe_burial_depth_label.setText(f"{hhe_params.burial_depth:g}")

    def _current_hhe_field_parameters(self):
        """Read `hhe_field_parameters` fresh from the selected settings file.

        Returns
        -------
        object | None
            The `HHEFieldParameters` domain object, or `None` if no settings
            file is selected or it can no longer be read/validated.

        """
        if self._hhe_settings_path is None:
            return None
        try:
            settings = load_settings(self._hhe_settings_path)
            return settings["hhe_field_parameters"]
        except (FileNotFoundError, ValueError, KeyError):
            return None

    # -- Snapping ------------------------------------------------------------

    def _on_snap_layer_changed(self, layer: QgsVectorLayer | None) -> None:
        self._point_tool.set_snap_layer(layer)
        self._ensure_layer_visible(layer)

    def _on_snap_enabled_toggled(self, checked: bool) -> None:
        self._point_tool.set_snap_enabled(checked)

    def _ensure_layer_visible(self, layer: QgsVectorLayer | None) -> None:
        """Show exactly `layer` plus the layers visible when the dialog opened.

        Snapping to a layer the user can't see defeats the point of showing
        it at all, so a newly selected snap layer is drawn on top -- but this
        recomputes from :attr:`_base_layers` every time (rather than adding
        onto whatever the canvas currently shows), so switching the selection
        replaces the previously added layer instead of stacking on top of it,
        and picking "no snapping" (`None`) reverts to exactly the original
        view.

        Parameters
        ----------
        layer : QgsVectorLayer | None
            Newly selected snap-to layer, or `None`.

        """
        if layer is not None and layer not in self._base_layers:
            self._canvas.setLayers([layer, *self._base_layers])
        else:
            self._canvas.setLayers(self._base_layers)
        self._canvas.refresh()

    # -- Connection node / preview -----------------------------------------

    def _on_pick_node_toggled(self, checked: bool) -> None:
        self._canvas.setMapTool(self._point_tool if checked else self._pan_tool)

    def _on_point_picked(self, point: QgsPointXY) -> None:
        self._connection_point = point
        self._pick_button.setChecked(False)
        self._regenerate_preview()

    def _regenerate_preview(self) -> None:
        self._rubber_band.reset(QgsWkbTypes.PointGeometry)
        self._rubber_band_hhe.reset(QgsWkbTypes.LineGeometry)
        self._points = []
        self._trench_lines = []

        if self._connection_point is None:
            self._set_status("Click “Set Connection Node”, then click the map.")
            return

        if self._mode == "BHE":
            self._regenerate_preview_bhe()
        else:
            self._regenerate_preview_hhe()

    def _regenerate_preview_bhe(self) -> None:
        grid = _GridParameters(
            n_rows=self._n_rows_spin.value(),
            n_cols=self._n_cols_spin.value(),
            spacing_row=self._spacing_row_spin.value(),
            spacing_col=self._spacing_col_spin.value(),
            rotation=self._rotation_spin.value(),
        )
        self._points = _generate_grid_points(self._connection_point, grid)
        for point in self._points:
            self._rubber_band.addPoint(point, True)
        self._canvas.refresh()
        self._set_status(f"{len(self._points)} borehole(s) previewed.")

    def _regenerate_preview_hhe(self) -> None:
        hhe_params = self._current_hhe_field_parameters()
        if hhe_params is None:
            self._set_status("Browse to an HHE settings file to preview trenches.")
            return
        self._refresh_hhe_display(hhe_params)

        trenches = _TrenchParameters(
            n_pipes_parallel=hhe_params.n_pipes_parallel,
            pipe_spacing=hhe_params.pipe_spacing,
            length_element=self._length_element_spin.value(),
            rotation=self._hhe_rotation_spin.value(),
            mirror_left=self._mirror_left_check.isChecked(),
        )
        self._trench_lines = _generate_trench_lines(self._connection_point, trenches)
        for start, end in self._trench_lines:
            self._rubber_band_hhe.addGeometry(
                QgsGeometry.fromPolylineXY([start, end]), None
            )
        self._canvas.refresh()
        self._set_status(f"{len(self._trench_lines)} trench(es) previewed.")

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    # -- Export --------------------------------------------------------------

    def _on_export(self) -> None:
        if self._mode == "BHE":
            self._export_bhe()
        else:
            self._export_hhe()

    def _export_bhe(self) -> None:
        if not self._points:
            QMessageBox.warning(
                self, "Nothing to export", "Set a connection node first."
            )
            return

        utils.warn_if_project_unsaved(self)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Borefield",
            os.path.join(utils.default_save_directory(), "borefield.geojson"),
            "GeoJSON (*.geojson)",
        )
        if not path:
            return

        fields = QgsFields()
        fields.append(QgsField("id", QMetaType.Type.QString))
        fields.append(QgsField("is_connection_node", QMetaType.Type.Bool))

        crs: QgsCoordinateReferenceSystem = self._canvas.mapSettings().destinationCrs()
        writer = QgsVectorFileWriter(
            path, "UTF-8", fields, QgsWkbTypes.Point, crs, "GeoJSON"
        )
        if writer.hasError() != QgsVectorFileWriter.NoError:
            QMessageBox.critical(
                self, "Export failed", f"Could not create GeoJSON file: {writer.errorMessage()}"
            )
            return

        for index, point in enumerate(self._points):
            feature = QgsFeature(fields)
            feature.setGeometry(QgsGeometry.fromPointXY(point))
            feature["id"] = f"BH{index}"
            feature["is_connection_node"] = index == 0
            writer.addFeature(feature)
        del writer

        layer = QgsVectorLayer(path, "Borefield", "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        self._set_status(f"Exported {len(self._points)} borehole(s) to {path}.")

    def _export_hhe(self) -> None:
        if not self._trench_lines:
            QMessageBox.warning(
                self, "Nothing to export", "Set a connection node first."
            )
            return
        if self._hhe_settings_path is None:
            QMessageBox.warning(
                self, "No settings file", "Browse to an HHE settings file first."
            )
            return

        utils.warn_if_project_unsaved(self)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Trenches",
            os.path.join(utils.default_save_directory(), "trenches.geojson"),
            "GeoJSON (*.geojson)",
        )
        if not path:
            return

        try:
            settings = load_settings(self._hhe_settings_path)
            if "hhe_field_parameters" not in settings:
                raise KeyError("hhe_field_parameters")
        except (FileNotFoundError, ValueError, KeyError) as exc:
            QMessageBox.critical(self, "Can't read settings file", str(exc))
            return

        crs: QgsCoordinateReferenceSystem = self._canvas.mapSettings().destinationCrs()
        try:
            _write_trench_geojson(path, self._trench_lines, crs)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return

        try:
            with open(self._hhe_settings_path, encoding="utf-8") as f:
                raw = json.load(f)
            raw["hhe_field_parameters"]["values"]["length_element"]["value"] = (
                self._length_element_spin.value()
            )
            raw[_QTHERMONET_HHE_KEY] = {
                "geojson_path": path,
                "mirror_left": self._mirror_left_check.isChecked(),
            }
            write_settings_json(self._hhe_settings_path, raw)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Can't save settings file", str(exc))
            return

        layer = QgsVectorLayer(path, "HHE Trenches", "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        self._set_status(
            f"Exported {len(self._trench_lines)} trench(es) to {path}, updated settings file."
        )
