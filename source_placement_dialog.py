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
  ``full_dimensioning_algorithm.py``. ``n_pipes_parallel``/``pipe_spacing``/
  ``length_element`` are editable here and written back on export, since
  they directly drive the preview; ``burial_depth`` stays read-only, since
  it has no effect on a 2D plan-view preview.

See ``claude/handoffs/handoff-interactive-borefield-placement.md`` (BHE) and
``claude/handoffs/2026-09-17-hhe-source-placement-backlog.md`` (HHE) for the
full confirmed designs this implements.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from pythermonet.input import load_settings
from pythermonet.resources import SETTINGS_TEMPLATE_BHE_PATH, SETTINGS_TEMPLATE_HHE_PATH

from . import output_handling, utils
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
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
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
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
#: Same idea for BHE: the last export's file and grid layout, so reopening the
#: dialog restores the placement instead of starting over.
_QTHERMONET_BHE_KEY = "qthermonet_bhe_settings"

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
        If the GeoJSON file can't be written, or loses its CRS (see
        `output_handling.write_geojson`).

    """
    fields = QgsFields()
    fields.append(QgsField("id", QMetaType.Type.QString))
    fields.append(QgsField("is_connection_node", QMetaType.Type.Bool))
    fields.append(QgsField("connection_node_end", QMetaType.Type.QString))

    features = []
    for index, (start, end) in enumerate(lines):
        feature = QgsFeature(fields)
        feature.setGeometry(QgsGeometry.fromPolylineXY([start, end]))
        feature["id"] = f"TR{index}"
        feature["is_connection_node"] = index == 0
        feature["connection_node_end"] = "start"
        features.append(feature)
    output_handling.write_geojson(path, fields, QgsWkbTypes.LineString, crs, features)


def _read_connection_geometry(
    path: str, target_crs: QgsCoordinateReferenceSystem
) -> list[QgsPointXY] | None:
    """Read the flagged connection feature from a previous Source Placement export.

    Parameters
    ----------
    path : str
        A previously exported borefield or trench GeoJSON.
    target_crs : QgsCoordinateReferenceSystem
        CRS to return the points in (the dialog's map CRS).

    Returns
    -------
    list of QgsPointXY or None
        ``[node]`` for a borefield, ``[connection_end, other_end]`` for a
        trench, or `None` if the file can't be read or has no flagged
        feature.

    """
    if not path or not os.path.isfile(path):
        return None
    layer = QgsVectorLayer(path, "previous_source", "ogr")
    if not layer.isValid() or "is_connection_node" not in layer.fields().names():
        return None
    transform = (
        QgsCoordinateTransform(layer.crs(), target_crs, QgsProject.instance())
        if layer.crs() != target_crs
        else None
    )
    has_end_field = "connection_node_end" in layer.fields().names()
    for feature in layer.getFeatures():
        if not feature["is_connection_node"]:
            continue
        geometry = QgsGeometry(feature.geometry())
        if transform is not None:
            geometry.transform(transform)
        if geometry.type() == QgsWkbTypes.PointGeometry:
            return [geometry.asPoint()]
        if geometry.type() == QgsWkbTypes.LineGeometry:
            polyline = geometry.asPolyline()
            if has_end_field and feature["connection_node_end"] == "end":
                return [polyline[-1], polyline[0]]
            return [polyline[0], polyline[-1]]
        return None
    return None


def _is_source_layer(layer) -> bool:
    """Whether `layer` is a Source Placement output (BHE borefield or HHE trenches)."""
    return isinstance(layer, QgsVectorLayer) and "is_connection_node" in layer.fields().names()


def refresh_hhe_trench_layer(settings_path: str) -> None:
    """Regenerate an HHE trench GeoJSON to match a just-changed settings file.

    Called only from two known trigger points -- `SettingsEditorDialog._on_save`
    and the HHE branch of `full_dimensioning_algorithm.py` after a run --
    never from a filesystem watcher. No-op if `settings_path` has no
    `qthermonet_hhe_settings` section, or if the layer it names isn't
    currently loaded in the project: this deliberately does not maintain a
    registry of unopened files (see the confirmed design in
    `claude/handoffs/2026-09-17-hhe-source-placement-backlog.md`).

    Must be called from the GUI thread: it swaps the file and reloads the
    layer via `output_handling.OutputSet`.

    Parameters
    ----------
    settings_path : str
        Path to the settings file that was just saved, or just used for an
        HHE dimensioning run.

    Raises
    ------
    output_handling.OutputCommitError
        If the regenerated file couldn't be written or swapped in (e.g. it's
        open in another program). The layer is then left as it was.

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
        if isinstance(layer, QgsVectorLayer) and output_handling.normalize_path(
            layer.source()
        ) == output_handling.normalize_path(geojson_path):
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

    # Rewritten as a whole file and swapped in (remove layer, replace file,
    # reload with the same name/style/place), like an export. Editing the
    # loaded layer in place (truncate + re-add) could hit a stale OGR handle:
    # every delete failed, the add still ran, and the file ended up with the
    # old and new trenches both (seen 2026-09-30).
    outputs = output_handling.OutputSet()
    temp_path = outputs.add_file(geojson_path, layer_name=target_layer.name())
    try:
        _write_trench_geojson(temp_path, lines, target_layer.crs())
    except OSError as exc:
        outputs.discard()
        raise output_handling.OutputCommitError(
            f"Could not refresh the HHE trench layer: {exc}"
        ) from exc
    outputs.commit()


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


_CARD_STYLE_UNSELECTED = (
    "QFrame#templateCard {"
    " border: 1.5px solid #c6c6c6; border-radius: 6px; background-color: #ffffff; }"
)
_CARD_STYLE_SELECTED = (
    "QFrame#templateCard {"
    " border: 2px solid #0067c0; border-radius: 6px; background-color: #eaf3fc; }"
)


def _mode_icon(mode: str) -> QPixmap:
    """Draw the small line-art glyph for a BHE/HHE template card.

    Parameters
    ----------
    mode : str
        ``"BHE"`` (three vertical boreholes below a ground-surface line) or
        ``"HHE"`` (three horizontal loops).

    Returns
    -------
    QPixmap
        A 26x26, transparent-background icon in the accent blue.
    """
    size = 26
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    accent = QColor("#0067c0")

    thick_pen = QPen(accent)
    thick_pen.setWidthF(2.0)
    thin_pen = QPen(accent)
    thin_pen.setWidthF(1.4)

    if mode == "BHE":
        painter.setPen(thin_pen)
        painter.drawLine(2, 4, 24, 4)  # ground surface
        painter.setPen(thick_pen)
        for x in (6, 13, 20):
            painter.drawLine(x, 4, x, 22)  # boreholes, hanging below the surface
    else:
        painter.setPen(thick_pen)
        for y in (8, 14, 20):
            painter.drawLine(4, y, 22, y)  # horizontal loops

    painter.end()
    return pixmap


class _ClickableFrame(QFrame):
    """A QFrame that emits `clicked` on mouse press, for card-style selection."""

    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:  # noqa: D102 (Qt override, not new public API)
        self.clicked.emit()
        super().mousePressEvent(event)


class _TemplateChoiceDialog(QDialog):
    """Small modal for New: pick a BHE or HHE starting template.

    Source Placement's mode is entirely file-driven (see
    `SourcePlacementDialog`'s own docstring) -- there is no mode toggle to
    infer a template from when creating a brand-new settings file, so this
    asks explicitly instead. Previously lived in `settings_editor_dialog.py`
    (removed when that dialog became Browse-only); moved here since this is
    now the only place a new settings file gets created.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Settings File")
        self.mode: str = "BHE"
        self.destination_path: str = ""

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose a starting template:"))

        cards_row = QHBoxLayout()
        self._bhe_radio = QRadioButton()
        self._bhe_card = self._build_card(
            self._bhe_radio, "BHE", "Starts from pythermonet's bundled BHE template."
        )
        cards_row.addWidget(self._bhe_card)

        self._hhe_radio = QRadioButton()
        self._hhe_card = self._build_card(
            self._hhe_radio, "HHE", "Starts from pythermonet's bundled HHE template."
        )
        cards_row.addWidget(self._hhe_card)
        layout.addLayout(cards_row)

        # The two radios live in different cards, so they don't share a
        # parent widget -- Qt only auto-exclusivizes radio buttons with the
        # same parent, so without this both could end up checked at once.
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._bhe_radio)
        self._mode_group.addButton(self._hhe_radio)

        self._bhe_radio.setChecked(True)
        self._bhe_card.setStyleSheet(_CARD_STYLE_SELECTED)
        self._bhe_radio.toggled.connect(lambda checked: self._restyle_card(self._bhe_card, checked))
        self._hhe_radio.toggled.connect(lambda checked: self._restyle_card(self._hhe_card, checked))
        self._bhe_card.clicked.connect(self._bhe_radio.click)
        self._hhe_card.clicked.connect(self._hhe_radio.click)

        note = QLabel(
            "The chosen template is copied as-is. QThermonet does not generate "
            "settings content itself."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a8a8a; font-size: 11px;")
        layout.addWidget(note)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Settings file:"))
        self._path_display = QLineEdit()
        self._path_display.setReadOnly(True)
        path_row.addWidget(self._path_display)
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._on_browse)
        path_row.addWidget(browse_button)
        layout.addLayout(path_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_browse(self) -> None:
        mode = "HHE" if self._hhe_radio.isChecked() else "BHE"
        default_name = f"settings_{mode.lower()}.json"
        utils.warn_if_project_unsaved(self)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save New Settings File",
            os.path.join(utils.default_save_directory(), default_name),
            "Settings JSON (*.json)",
        )
        if not path:
            return
        self.destination_path = path
        self._path_display.setText(path)
        self._ok_button.setEnabled(True)

    def _build_card(self, radio: QRadioButton, mode: str, description: str) -> _ClickableFrame:
        card = _ClickableFrame()
        card.setObjectName("templateCard")
        card.setStyleSheet(_CARD_STYLE_UNSELECTED)

        card_layout = QVBoxLayout(card)
        top_row = QHBoxLayout()
        icon_label = QLabel()
        icon_label.setPixmap(_mode_icon(mode))
        top_row.addWidget(icon_label)
        top_row.addStretch(1)
        top_row.addWidget(radio)
        card_layout.addLayout(top_row)

        title = QLabel("Borehole (BHE)" if mode == "BHE" else "Horizontal (HHE)")
        title.setStyleSheet("font-weight: 600; font-size: 13px;")
        card_layout.addWidget(title)

        desc_label = QLabel(description)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #5f5f5f; font-size: 11px;")
        card_layout.addWidget(desc_label)

        return card

    def _restyle_card(self, card: _ClickableFrame, selected: bool) -> None:
        card.setStyleSheet(_CARD_STYLE_SELECTED if selected else _CARD_STYLE_UNSELECTED)

    def accept(self) -> None:  # noqa: D102 (Qt override, not new public API)
        self.mode = "HHE" if self._hhe_radio.isChecked() else "BHE"
        super().accept()


class SourcePlacementDialog(QDialog):
    """Window for interactively placing a project's BHE/HHE source field.

    Embeds a `QgsMapCanvas`: the user optionally picks a layer to snap to
    (e.g. the pipe topology network), clicks a connection node, then either:

    - **BHE**: a rectangular grid of boreholes is generated and live-previewed
      from row/column count, independent row/column spacing, and rotation.
      Export writes a GeoJSON point layer (`id` + `is_connection_node`).
    - **HHE**: a single row of parallel trenches is generated from a settings
      file's `n_pipes_parallel`/`pipe_spacing`/`length_element`, seeded from
      the file on load but editable here (they directly drive the preview),
      plus dialog-owned rotation/handedness. `burial_depth` stays a
      read-only display -- it has no effect on this 2D preview. Export
      writes a GeoJSON line layer (`id` + `is_connection_node` +
      `connection_node_end`) and writes the (possibly adjusted)
      `n_pipes_parallel`/`pipe_spacing`/`length_element` back into the
      settings file.

    Source type (BHE/HHE) is entirely file-driven, not a manual toggle:
    Browsing to an existing settings file detects its mode
    (:func:`~QThermonet.utils.detect_mode`); creating a new one asks via
    `_TemplateChoiceDialog`, since there's no toggle to infer it from. Before
    any settings file is loaded, placement is disabled and neither panel is
    shown.

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
        self._mode: str | None = None
        self._connection_point: QgsPointXY | None = None
        self._points: list[QgsPointXY] = []
        self._trench_lines: list[tuple[QgsPointXY, QgsPointXY]] = []
        self._base_layers: list[QgsVectorLayer] = []
        self._settings_path: str | None = None

        self._build_ui()
        self._sync_canvas_to_project()
        self._select_default_snap_layer()
        self._set_mode(None)

        last_path = utils.get_current_settings_path()
        if last_path:
            self._try_autoload_settings_path(last_path)

    # -- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        outer.addWidget(self._build_settings_file_row())

        self._mode_label = QLabel("No settings file loaded.")
        self._mode_label.setStyleSheet("color: #5f5f5f; font-size: 11px;")
        outer.addWidget(self._mode_label)

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

    def _build_settings_file_row(self) -> QWidget:
        """Build the top-of-dialog "Settings file: [path] [Browse…] [New…]" row.

        This is the sole settings-file control for the whole dialog (BHE and
        HHE no longer each have their own) -- Browse auto-detects the loaded
        file's mode, New goes through `_TemplateChoiceDialog` to ask which
        template to start from, and either way `_apply_settings_path` drives
        which panel is shown from there.
        """
        row = QHBoxLayout()
        row.addWidget(QLabel("Settings file:"))
        self._settings_display = QLineEdit()
        self._settings_display.setReadOnly(True)
        row.addWidget(self._settings_display)
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._on_browse_settings)
        row.addWidget(browse_button)
        new_button = QPushButton("New…")
        new_button.clicked.connect(self._on_new_settings)
        row.addWidget(new_button)

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

        self._empty_state_label = QLabel(
            "Browse or create a settings file above to begin."
        )
        self._empty_state_label.setWordWrap(True)
        self._empty_state_label.setStyleSheet("color: #8a8a8a; font-size: 11px;")
        panel.addWidget(self._empty_state_label)

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

        self._n_pipes_parallel_spin = self._add_spin_row(
            layout, "N parallel pipes (2 × U-pipes):", QSpinBox, 2, 50, 2
        )
        self._n_pipes_parallel_spin.setSingleStep(2)
        self._n_pipes_parallel_spin.setEnabled(False)
        self._n_pipes_parallel_spin.setToolTip(
            "Each U-pipe trench counts as 2 parallel pipes (one out, one "
            "return) -- e.g. 3 U-pipe trenches = 6 parallel pipes. "
            "pythermonet floors n_pipes_parallel // 2 to get the U-pipe "
            "count, so this must stay even -- the up/down arrows only step "
            "by 2, but typing an odd value directly is still possible."
        )

        self._pipe_spacing_spin = self._add_spin_row(
            layout, "Pipe spacing (m):", QDoubleSpinBox, 0.1, 100.0, 1.0
        )
        self._pipe_spacing_spin.setEnabled(False)

        self._burial_depth_spin = self._add_spin_row(
            layout, "Burial depth (m):", QDoubleSpinBox, 0.0, 100.0, 0.0
        )
        self._burial_depth_spin.setEnabled(False)
        self._burial_depth_spin.setToolTip(
            "Read-only: burial depth has no effect on this 2D preview. "
            "Edit it in Dimensioning Settings instead."
        )

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
            layers = list(main_canvas.layers())
            self._canvas.setExtent(main_canvas.extent())
        else:
            layers = list(project.mapLayers().values())
        # A previously exported field would be drawn under the live preview
        # and never update while editing -- leave source layers out.
        self._base_layers = [layer for layer in layers if not _is_source_layer(layer)]
        self._snap_layer_combo.setExceptedLayerList(
            [layer for layer in project.mapLayers().values() if _is_source_layer(layer)]
        )
        self._canvas.setLayers(self._base_layers)
        self._canvas.refresh()

    def _select_default_snap_layer(self) -> None:
        """Pre-select the roads layer (the one Get Buildings cached) as the snap target."""
        roads_path = utils.get_cached_path("roads_file")
        if not roads_path:
            return
        target = output_handling.normalize_path(roads_path)
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and output_handling.normalize_path(
                layer.source()
            ) == target:
                self._snap_layer_combo.setLayer(layer)
                return

    # -- Mode handling -----------------------------------------------------

    def _set_mode(self, mode: str | None) -> None:
        self._mode = mode
        self._empty_state_label.setVisible(mode is None)
        self._bhe_panel.setVisible(mode == "BHE")
        self._hhe_panel.setVisible(mode == "HHE")
        self._pick_button.setEnabled(mode is not None)
        self._export_button.setEnabled(mode is not None)
        self._export_button.setText("Export Trenches…" if mode == "HHE" else "Export Borefield…")
        if mode is None:
            self._mode_label.setText("No settings file loaded.")
        else:
            self._mode_label.setText(
                f"Mode: {mode} ({'Borehole' if mode == 'BHE' else 'Horizontal'} Heat Exchanger)"
            )
        self._regenerate_preview()

    # -- Settings-file link ---------------------------------------------

    def _on_browse_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Settings File", "", "Settings JSON (*.json)"
        )
        if not path:
            return

        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Can't open settings file", str(exc))
            return

        mode = utils.detect_mode(raw)
        if mode is None:
            QMessageBox.critical(
                self,
                "Can't open settings file",
                "This file doesn't contain any role QThermonet recognizes as "
                "BHE- or HHE-specific, so its mode can't be determined.",
            )
            return

        if self._apply_settings_path(mode, path):
            utils.load_existing_settings_path(path)

    def _on_new_settings(self) -> None:
        choice = _TemplateChoiceDialog(self)
        if choice.exec() != QDialog.DialogCode.Accepted:
            return

        template_path = (
            SETTINGS_TEMPLATE_BHE_PATH if choice.mode == "BHE" else SETTINGS_TEMPLATE_HHE_PATH
        )
        try:
            shutil.copyfile(template_path, choice.destination_path)
        except OSError as exc:
            QMessageBox.critical(self, "Can't create settings file", str(exc))
            return

        if self._apply_settings_path(choice.mode, choice.destination_path):
            utils.create_new_settings_path(choice.destination_path)

    def _apply_settings_path(self, mode: str, path: str, *, silent: bool = False) -> bool:
        """Load `path` as the active settings file, switching the dialog to `mode`.

        Parameters
        ----------
        mode : str
            ``"BHE"`` or ``"HHE"`` -- which panel to switch to.
        path : str
            Settings file to load.
        silent : bool
            If True, suppress error dialogs on failure instead of showing
            them. Used for the automatic last-used-file pre-load on dialog
            open, where popping an error for a file the user didn't just
            pick themselves this time would be unexpected.

        Returns
        -------
        bool
            Whether `path` was successfully applied. On failure, no state
            was changed (and, unless `silent`, an error dialog was shown).
        """

        def _fail(title: str, message: str) -> bool:
            if not silent:
                QMessageBox.critical(self, title, message)
            return False

        if mode == "HHE":
            try:
                settings = load_settings(path)
                hhe_params = settings["hhe_field_parameters"]
            except (FileNotFoundError, ValueError) as exc:
                return _fail("Can't open settings file", str(exc))
            except KeyError:
                return _fail(
                    "Not an HHE settings file",
                    "This settings file has no 'hhe_field_parameters' role.",
                )

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
        else:
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                return _fail("Can't open settings file", str(exc))

            if utils.detect_mode(raw) != "BHE":
                return _fail(
                    "Not a BHE settings file",
                    "This settings file doesn't contain any BHE-specific role.",
                )

        self._settings_path = path
        self._settings_display.setText(path)
        self._set_mode(mode)
        self._restore_previous_placement(mode, path)
        return True

    def _restore_previous_placement(self, mode: str, path: str) -> None:
        """Set the connection node (and layout) from this settings file's previous export.

        Silent on any failure -- the dialog then just waits for a picked node,
        as before. HHE: node and rotation come from the trench file named in
        ``qthermonet_hhe_settings`` (mirroring is restored by the caller).
        BHE: node from the file in ``qthermonet_bhe_settings`` plus the stored
        grid layout; for exports made before that section existed, only the
        node, from the cached borefield file.
        """
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        crs = self._canvas.mapSettings().destinationCrs()

        if mode == "HHE":
            section = raw.get(_QTHERMONET_HHE_KEY) or {}
            points = _read_connection_geometry(section.get("geojson_path"), crs)
            if not points or len(points) != 2:
                return
            node, rotation = _derive_connection_node_and_rotation(
                points[0], points[1], self._mirror_left_check.isChecked()
            )
            self._hhe_rotation_spin.setValue((rotation + 180.0) % 360.0 - 180.0)
        else:
            section = raw.get(_QTHERMONET_BHE_KEY) or {}
            geojson_path = section.get("geojson_path") or utils.get_cached_path("borefield_file")
            points = _read_connection_geometry(geojson_path, crs)
            if not points or len(points) != 1:
                return
            node = points[0]
            for key, spin in (
                ("n_rows", self._n_rows_spin),
                ("n_cols", self._n_cols_spin),
                ("spacing_row", self._spacing_row_spin),
                ("spacing_col", self._spacing_col_spin),
                ("rotation", self._rotation_spin),
            ):
                if key in section:
                    spin.setValue(section[key])

        self._on_point_picked(node)
        self._set_status("Restored the previous placement -- pick a new node to move it.")

    def _try_autoload_settings_path(self, path: str) -> None:
        """Silently pre-load the last-used settings file, if it still applies.

        Runs once on dialog open so a file already selected in another
        QThermonet dialog this session doesn't have to be reselected here.
        Failures are swallowed rather than shown: if the file moved or
        changed shape since, the dialog just opens as if nothing were
        cached, same as its very first run.

        Parameters
        ----------
        path : str
            Settings file to try loading, typically
            :func:`~QThermonet.utils.get_current_settings_path`.
        """
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        mode = utils.detect_mode(raw)
        if mode is None:
            return

        self._apply_settings_path(mode, path, silent=True)

    def _refresh_hhe_display(self, hhe_params) -> None:
        """Seed the editable HHE fields from a just-loaded settings file.

        Only called on load (from :meth:`_apply_settings_path`), never from
        :meth:`_regenerate_preview_hhe` -- these fields are dialog-owned
        once seeded, so re-seeding on every preview regeneration would
        overwrite the user's in-progress edits with the file's stale values.
        `burial_depth` stays disabled (grayed out, matching the other
        fields' box styling rather than a plain label): it has no effect on
        the 2D preview, so it's shown but never made editable here.
        """
        self._n_pipes_parallel_spin.setValue(hhe_params.n_pipes_parallel)
        self._n_pipes_parallel_spin.setEnabled(True)
        self._pipe_spacing_spin.setValue(hhe_params.pipe_spacing)
        self._pipe_spacing_spin.setEnabled(True)
        self._burial_depth_spin.setValue(hhe_params.burial_depth)

    def _current_hhe_field_parameters(self):
        """Read `hhe_field_parameters` fresh from the selected settings file.

        Returns
        -------
        object | None
            The `HHEFieldParameters` domain object, or `None` if no settings
            file is selected or it can no longer be read/validated.

        """
        if self._settings_path is None:
            return None
        try:
            settings = load_settings(self._settings_path)
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

        if self._mode is None:
            self._set_status("Browse or create a settings file to begin.")
            return

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
        # Only used to confirm the settings file still loads/validates --
        # its n_pipes_parallel/pipe_spacing are deliberately *not* read here;
        # see `_refresh_hhe_display`'s docstring for why.
        if self._current_hhe_field_parameters() is None:
            self._set_status("Browse to an HHE settings file to preview trenches.")
            return

        trenches = _TrenchParameters(
            n_pipes_parallel=self._n_pipes_parallel_spin.value(),
            pipe_spacing=self._pipe_spacing_spin.value(),
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

        utils.maybe_prompt_project_setup(
            self, crs=self._canvas.mapSettings().destinationCrs(), point=self._points[0]
        )
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

        # Written to a temp file and swapped in, so re-exporting over a file
        # that's loaded as a layer works (the loaded layer locks it on Windows).
        outputs = output_handling.OutputSet()
        temp_path = outputs.add_file(
            path, layer_name="Borefield", cache_roles=["borefield_file", "source_file"]
        )

        crs: QgsCoordinateReferenceSystem = self._canvas.mapSettings().destinationCrs()
        features = []
        for index, point in enumerate(self._points):
            feature = QgsFeature(fields)
            feature.setGeometry(QgsGeometry.fromPointXY(point))
            feature["id"] = f"BH{index}"
            feature["is_connection_node"] = index == 0
            features.append(feature)
        try:
            output_handling.write_geojson(temp_path, fields, QgsWkbTypes.Point, crs, features)
        except OSError as exc:
            outputs.discard()
            QMessageBox.critical(self, "Export failed", str(exc))
            return

        # Record the layout next to the file, so reopening the dialog can
        # restore it (see _restore_previous_placement).
        if self._settings_path is not None:
            try:
                with open(self._settings_path, encoding="utf-8") as f:
                    raw = json.load(f)
                raw[_QTHERMONET_BHE_KEY] = {
                    "geojson_path": path,
                    "n_rows": self._n_rows_spin.value(),
                    "n_cols": self._n_cols_spin.value(),
                    "spacing_row": self._spacing_row_spin.value(),
                    "spacing_col": self._spacing_col_spin.value(),
                    "rotation": self._rotation_spin.value(),
                }
                utils.write_settings_json(self._settings_path, raw)
            except (OSError, ValueError) as exc:
                outputs.discard()
                QMessageBox.critical(self, "Can't save settings file", str(exc))
                return

        try:
            outputs.commit()
        except output_handling.OutputCommitError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._set_status(f"Exported {len(self._points)} borehole(s) to {path}, updated settings file.")

    def _export_hhe(self) -> None:
        if not self._trench_lines:
            QMessageBox.warning(
                self, "Nothing to export", "Set a connection node first."
            )
            return
        if self._settings_path is None:
            QMessageBox.warning(
                self, "No settings file", "Browse to an HHE settings file first."
            )
            return

        utils.maybe_prompt_project_setup(
            self, crs=self._canvas.mapSettings().destinationCrs(), point=self._trench_lines[0][0]
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Trenches",
            os.path.join(utils.default_save_directory(), "trenches.geojson"),
            "GeoJSON (*.geojson)",
        )
        if not path:
            return

        try:
            settings = load_settings(self._settings_path)
            if "hhe_field_parameters" not in settings:
                raise KeyError("hhe_field_parameters")
        except (FileNotFoundError, ValueError, KeyError) as exc:
            QMessageBox.critical(self, "Can't read settings file", str(exc))
            return

        # Written to a temp file and swapped in, so re-exporting over a file
        # that's loaded as a layer works (the loaded layer locks it on Windows).
        outputs = output_handling.OutputSet()
        temp_path = outputs.add_file(
            path, layer_name="HHE trenches", cache_roles=["trenches_file", "source_file"]
        )

        crs: QgsCoordinateReferenceSystem = self._canvas.mapSettings().destinationCrs()
        try:
            _write_trench_geojson(temp_path, self._trench_lines, crs)
        except OSError as exc:
            outputs.discard()
            QMessageBox.critical(self, "Export failed", str(exc))
            return

        try:
            with open(self._settings_path, encoding="utf-8") as f:
                raw = json.load(f)
            raw["hhe_field_parameters"]["values"]["length_element"]["value"] = (
                self._length_element_spin.value()
            )
            raw["hhe_field_parameters"]["values"]["n_pipes_parallel"]["value"] = (
                self._n_pipes_parallel_spin.value()
            )
            raw["hhe_field_parameters"]["values"]["pipe_spacing"]["value"] = (
                self._pipe_spacing_spin.value()
            )
            raw[_QTHERMONET_HHE_KEY] = {
                "geojson_path": path,
                "mirror_left": self._mirror_left_check.isChecked(),
            }
            utils.write_settings_json(self._settings_path, raw)
        except (OSError, ValueError) as exc:
            outputs.discard()
            QMessageBox.critical(self, "Can't save settings file", str(exc))
            return

        try:
            outputs.commit()
        except output_handling.OutputCommitError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._set_status(
            f"Exported {len(self._trench_lines)} trench(es) to {path}, updated settings file."
        )
