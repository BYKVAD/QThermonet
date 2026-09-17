# -*- coding: utf-8 -*-
"""Interactive source-placement window for QThermonet.

Lets a user click a connection node on an embedded map (optionally snapped
to an existing layer, e.g. the pipe topology network) and generate a
borefield as a rectangular grid (BHE), exporting it as a GeoJSON layer that
``full_dimensioning_algorithm.py`` converts to pythermonet's WKT/EWKT
``.dat`` format at call time.

Named generically ("Source Placement", not "Borefield Placement") because
HHE (horizontal loop) placement is planned to live behind this same dialog
later, via the manual BHE/HHE toggle. HHE geometry itself (parallel
trenches, not a point grid) is unbuilt and undesigned -- see
``claude/handoffs/2026-09-17-hhe-source-placement-backlog.md``.

See ``claude/handoffs/handoff-interactive-borefield-placement.md`` for the
full confirmed design this implements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

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
    (e.g. the pipe topology network), clicks a connection node, then a
    rectangular grid of boreholes (BHE only, in this first pass) is
    generated and live-previewed from row/column count, independent
    row/column spacing, and rotation. Export writes a GeoJSON point layer
    (`id` + `is_connection_node` attributes) and adds it to the project.

    Source type (BHE/HHE) is a manual toggle -- HHE is not yet implemented
    and shows a placeholder panel.

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
        self._base_layers: list[QgsVectorLayer] = []

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
        self._hhe_radio = QRadioButton("HHE (not yet implemented)")
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

        self._hhe_panel = QLabel(
            "HHE source placement is not yet implemented in this tool.\n"
            "See claude/handoffs/2026-09-17-hhe-source-placement-backlog.md."
        )
        self._hhe_panel.setWordWrap(True)
        self._hhe_panel.setStyleSheet("color: #6b6b6b;")
        self._hhe_panel.setVisible(False)
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
        self._export_button.setEnabled(mode == "BHE")

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
        if self._connection_point is None or self._mode != "BHE":
            self._points = []
            self._set_status("Click “Set Connection Node”, then click the map.")
            return

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

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    # -- Export --------------------------------------------------------------

    def _on_export(self) -> None:
        if not self._points:
            QMessageBox.warning(
                self, "Nothing to export", "Set a connection node first."
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Borefield", "borefield.geojson", "GeoJSON (*.geojson)"
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
