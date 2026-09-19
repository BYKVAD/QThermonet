# -*- coding: utf-8 -*-
"""Settings-editing window for QThermonet.

Lets a user create or edit a project's ``settings.json`` without leaving
QGIS. Rendering is schema-agnostic (one text box per field found in the
loaded JSON) and role-filtered (only roles from :data:`ROLES_BHE` /
:data:`ROLES_HHE` are shown, in that order). Save writes the edited JSON via
:func:`~QThermonet.source_placement_dialog.write_settings_json` -- the one
place in QThermonet that actually writes a settings file to disk, shared
with Source Placement's HHE export and Full Dimensioning's computed-length
write-back. It validates with ``pythermonet.input.load_settings`` (the real
function the full-dimensioning algorithm will call) before ever replacing
the target file -- so a failed edit never corrupts the last-known-good
settings file. After a successful save,
:func:`~QThermonet.source_placement_dialog.refresh_hhe_trench_layer` is
called too, refreshing an already-exported HHE trench layer if one is
loaded and affected.

See ``claude/handoff/handoff-settings-window-design.md`` for the full
confirmed design this implements.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from pythermonet.resources import SETTINGS_TEMPLATE_BHE_PATH, SETTINGS_TEMPLATE_HHE_PATH

from . import utils
from .source_placement_dialog import refresh_hhe_trench_layer, write_settings_json
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

#: Roles rendered for a BHE (borehole) settings file, in the order they
#: appear in pythermonet's bundled BHE template.
ROLES_BHE: tuple[str, ...] = (
    "pipe_material_dist",
    "brine",
    "soil",
    "grout",
    "pipe_material_bhe",
    "distribution_network_parameters",
    "heat_pump_peak_supply_parameters",
    "sizing_parameters",
    "brine_temperature_limits",
    "borehole",
    "pipe_segment_bhe",
    "vhe_field_parameters",
)

#: Roles rendered for an HHE (horizontal) settings file, in the order they
#: appear in pythermonet's bundled HHE template.
ROLES_HHE: tuple[str, ...] = (
    "pipe_material_dist",
    "brine",
    "soil",
    "distribution_network_parameters",
    "heat_pump_peak_supply_parameters",
    "sizing_parameters",
    "brine_temperature_limits",
    "pipe_material_hhe",
    "pipe_segment_hhe",
    "hhe_field_parameters",
)

# Roles that exist only in one template -- used to detect a loaded file's
# mode without asking the user, by checking which side's unique roles it has.
_BHE_ONLY_ROLES = frozenset(
    {"grout", "pipe_material_bhe", "borehole", "pipe_segment_bhe", "vhe_field_parameters"}
)
_HHE_ONLY_ROLES = frozenset(
    {"pipe_material_hhe", "pipe_segment_hhe", "hhe_field_parameters"}
)

# Hand-maintained, QThermonet-local display overrides for the generic
# snake_case -> Title Case label fallback: acronyms (kept upper-case) and
# abbreviations (expanded to a friendlier phrase). Cosmetic only -- purely
# how QGIS displays the name, never written back to the JSON or seen by
# pythermonet -- so an unlisted token just falls back to str.capitalize(),
# never an error.
_LABEL_OVERRIDES: dict[str, str] = {
    "sdr": "SDR",
    "bhe": "BHE",
    "hhe": "HHE",
    "vhe": "VHE",
    "dist": "Distribution Network",
}

# Cosmetic-only unit rendering. The underlying unit string is always taken
# verbatim from the file itself; this only changes how it is *displayed*.
_UNIT_DISPLAY_OVERRIDES: dict[str, str] = {
    "kg/m^3": "kg/m³",
    "W/m^2": "W/m²",
    "Pa*s": "Pa·s",
    "degC": "°C",
    "-": "–",
}

# Matches every line load_settings() can produce in its ValueError message,
# e.g.:
#   - Block 'soil' (type: Soil), field 'thermal_conductivity_shallow_heating': unit 'W/m' expected, got 'W/m/K'
#   - Block 'borehole' (type: Annulus):
#   - Block 'sizing_parameters': Unknown settings type '...'. Known types: ...
_PROBLEM_LINE_RE = re.compile(
    r"^- Block '(?P<role>[^']+)'"
    r"(?: \(type: (?P<cls>[^)]+)\))?"
    r"(?:, field '(?P<field>[^']+)')?"
    r":\s*(?P<rest>.*)$"
)

# Type-selector-scoped, not bare declarations: an unscoped setStyleSheet()
# cascades border/background down into every unstyled child widget in Qt
# (e.g. a QGroupBox's border rule would otherwise paint every QFrame field
# row inside it red too), so each is pinned to the exact widget class it's
# meant for.
#
# The "normal" variants reserve the exact same border width / radius as the
# error variants, just a different color -- applied from the moment each
# box/field is built, not only on clear. Switching a QGroupBox/QFrame between
# "no stylesheet at all" (native-drawn) and "has a stylesheet" (CSS box
# model) shifts its content by a pixel or two, which is what made the error
# outline appear to nudge "Soil" up; keeping both states on the same CSS
# box model the whole time removes that jump.
_GROUPBOX_MARGIN_TOP = 6  # px of headroom above the border the title floats in


def _groupbox_style(border_color: str, background: str, title_color: str = "#1a1a1a") -> str:
    """Build a QGroupBox stylesheet with a bold title centered on the border.

    Qt's default title placement, combined with a plain `border` rule, does
    not reliably sit the title's vertical center on the border line the way
    a native (unstyled) QGroupBox does -- this reproduces that positioning
    explicitly via `margin-top` + the `::title` subcontrol, which is the
    standard Qt Style Sheets technique for it.

    Parameters
    ----------
    border_color : str
        CSS color for the 2px border.
    background : str
        CSS color (or `"transparent"`) for the box interior.
    title_color : str
        CSS color for the bold title text.

    Returns
    -------
    str
        A complete stylesheet for `QGroupBox.setStyleSheet()`.
    """
    return (
        f"QGroupBox {{ border: 2px solid {border_color}; border-radius: 4px; "
        f"background-color: {background}; margin-top: {_GROUPBOX_MARGIN_TOP}px; }}"
        "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; "
        f"left: 10px; padding: 0 4px; font-weight: bold; color: {title_color}; }}"
    )


_NORMAL_BOX_STYLE = _groupbox_style("#d0d0d0", "transparent")
_ERROR_BORDER_STYLE_BOX = _groupbox_style("#d13438", "#fef4f4", title_color="#a4262c")

_NORMAL_FIELD_STYLE = (
    "QFrame { border: 2px solid transparent; border-radius: 4px; background-color: transparent; }"
)
_ERROR_BORDER_STYLE_FIELD = (
    "QFrame { border: 2px solid #d13438; border-radius: 4px; background-color: #fef4f4; }"
)


def _display_label(name: str) -> str:
    """Render a snake_case field or role name for display.

    Parameters
    ----------
    name : str
        Raw snake_case name, e.g. ``"thermal_conductivity_shallow_heating"``.

    Returns
    -------
    str
        Title Case text, e.g. ``"Thermal Conductivity Shallow Heating"``,
        with any token in :data:`_LABEL_OVERRIDES` rendered as an acronym
        (e.g. ``"sdr"`` -> ``"SDR"``) instead of being title-cased.
    """
    return " ".join(
        _LABEL_OVERRIDES.get(token.lower(), token.capitalize())
        for token in name.split("_")
    )


def _display_unit(unit: str) -> str:
    """Render a unit string for display, never altering its stored meaning.

    Parameters
    ----------
    unit : str
        Raw unit string as stored in the settings file, e.g. ``"kg/m^3"``.

    Returns
    -------
    str
        A cosmetically nicer form (e.g. ``"kg/m³"``) if one is known,
        otherwise `unit` unchanged.
    """
    return _UNIT_DISPLAY_OVERRIDES.get(unit, unit)


def detect_mode(settings: dict) -> str | None:
    """Detect whether a settings file/dict is BHE or HHE.

    Public API: also used by `full_dimensioning_algorithm.py` to derive the
    heat exchanger mode straight from the chosen settings file, instead of
    a separate dialog dropdown.

    Parameters
    ----------
    settings : dict
        Role name -> block, either the settings file's raw top-level JSON
        or the `dict[str, object]` `pythermonet.input.load_settings`
        returns -- only the key set (role names) is inspected, so either
        shape works.

    Returns
    -------
    str | None
        ``"BHE"`` or ``"HHE"`` if a mode-unique role is present, otherwise
        `None` if the file contains neither (mode can't be determined).
    """
    roles = set(settings)
    if roles & _BHE_ONLY_ROLES:
        return "BHE"
    if roles & _HHE_ONLY_ROLES:
        return "HHE"
    return None


def _coerce_value(text: str, original: object) -> object:
    """Parse an edited text box back into the field's original JSON type.

    load_settings() never checks a value's Python type, only its unit and
    field-name set -- so without this, retyping a number would silently
    write it back as a string. Round-trips against whatever type the field
    already had, rather than any schema pythermonet defines.

    Parameters
    ----------
    text : str
        Current text of the field's editor widget.
    original : object
        The value as originally loaded from JSON, whose type is preserved.

    Returns
    -------
    object
        `text` parsed as `bool`/`int`/`float` if `original` was one of
        those, otherwise `text` itself.

    Raises
    ------
    ValueError
        If `text` can't be parsed as the type `original` had.
    """
    if isinstance(original, bool):
        lowered = text.strip().lower()
        if lowered not in ("true", "false"):
            raise ValueError(f"expected true/false, got {text!r}")
        return lowered == "true"
    if isinstance(original, int):
        try:
            return int(text)
        except ValueError:
            as_float = float(text)
            if as_float != int(as_float):
                raise ValueError(f"expected a whole number, got {text!r}") from None
            return int(as_float)
    if isinstance(original, float):
        return float(text)
    return text


@dataclass
class _FieldRow:
    """One rendered field: its widgets plus enough state to save it back.

    Attributes
    ----------
    role : str
        Role (top-level JSON key) this field belongs to.
    field_name : str
        Field name within the role's ``"values"``.
    editor : QLineEdit
        The editable value widget.
    container : QFrame
        Wraps label + editor + unit; its border is what gets highlighted
        red on a validation error naming this field.
    original_value : object
        The value as loaded, used to preserve its type on save
        (see :func:`_coerce_value`).
    """

    role: str
    field_name: str
    editor: QLineEdit
    container: QFrame
    original_value: object


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
    """Small modal for New: pick a BHE or HHE starting template."""

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


class SettingsEditorDialog(QDialog):
    """Window for creating/editing a QThermonet project's settings file.

    Renders one editable text box per field found under each role's
    ``"values"`` in the loaded JSON, filtered to :data:`ROLES_BHE` or
    :data:`ROLES_HHE` depending on the loaded file's detected mode. Save
    validates the edited JSON with `pythermonet.input.load_settings`
    before ever touching the real file on disk.

    Parameters
    ----------
    parent : QWidget | None
        Parent widget, typically the QGIS main window.
    initial_path : str | None
        Settings file to load immediately, if any.

    Attributes
    ----------
    path : Path | None
        Currently loaded settings file, or `None` if nothing is loaded yet.
    """

    def __init__(self, parent=None, initial_path: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("QThermonet — Dimensioning Settings")
        self.resize(860, 780)

        self.path: Path | None = None
        self._mode: str | None = None
        self._raw: dict = {}
        self._field_rows: list[_FieldRow] = []
        self._role_boxes: dict[str, QGroupBox] = {}

        self._build_ui()
        if initial_path:
            self._load_file(initial_path)

    # -- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel("Settings file:"))
        self._path_display = QLineEdit()
        self._path_display.setReadOnly(True)
        header.addWidget(self._path_display)
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._on_browse)
        header.addWidget(browse_button)
        new_button = QPushButton("New…")
        new_button.clicked.connect(self._on_new)
        header.addWidget(new_button)
        outer.addLayout(header)

        self._mode_label = QLabel("No settings file loaded.")
        self._mode_label.setStyleSheet("color: #5f5f5f; font-size: 11px;")
        outer.addWidget(self._mode_label)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setWidget(QWidget())
        outer.addWidget(self._scroll_area, stretch=1)

        self._status_label = QLabel("Ready.")
        self._status_label.setStyleSheet("color: #6b6b6b; font-size: 11px;")
        outer.addWidget(self._status_label)

        self._error_panel = self._build_error_panel()
        self._error_panel.setVisible(False)
        outer.addWidget(self._error_panel)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _build_error_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("errorPanel")
        panel.setStyleSheet(
            "QFrame#errorPanel { border: 1px solid #d13438; border-radius: 4px; "
            "background-color: #fde7e9; }"
        )
        layout = QVBoxLayout(panel)

        self._error_message_label = QLabel()
        mono_font = QFont("Consolas")
        mono_font.setStyleHint(QFont.StyleHint.Monospace)
        self._error_message_label.setFont(mono_font)
        self._error_message_label.setWordWrap(True)
        self._error_message_label.setStyleSheet("color: #7a1721;")
        layout.addWidget(self._error_message_label)

        self._error_guidance_label = QLabel()
        self._error_guidance_label.setWordWrap(True)
        self._error_guidance_label.setStyleSheet("color: #3a3a3a; font-size: 11px;")
        layout.addWidget(self._error_guidance_label)

        path_row = QHBoxLayout()
        self._error_path_display = QLineEdit()
        self._error_path_display.setReadOnly(True)
        path_row.addWidget(self._error_path_display)
        copy_button = QPushButton("Copy Path")
        copy_button.clicked.connect(self._on_copy_path)
        path_row.addWidget(copy_button)
        self._error_path_row = QWidget()
        self._error_path_row.setLayout(path_row)
        layout.addWidget(self._error_path_row)

        return panel

    # -- Loading / role rendering -----------------------------------------

    def _load_file(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Can't open settings file", str(exc))
            return

        mode = detect_mode(raw)
        if mode is None:
            QMessageBox.critical(
                self,
                "Can't open settings file",
                "This file doesn't contain any role QThermonet recognizes as "
                "BHE- or HHE-specific, so its mode can't be determined.",
            )
            return

        self.path = Path(path)
        self._mode = mode
        self._raw = raw
        self._path_display.setText(str(self.path))
        self._mode_label.setText(
            f"Mode: {mode} ({'Borehole' if mode == 'BHE' else 'Horizontal'} Heat "
            "Exchanger) · fields are edited as plain text and validated on Save."
        )
        self._set_status("Ready.")
        self._rebuild_sections()

    def _roles_for_mode(self) -> tuple[str, ...]:
        return ROLES_BHE if self._mode == "BHE" else ROLES_HHE

    def _rebuild_sections(self) -> None:
        self._field_rows = []
        self._role_boxes = {}

        normal_font = QFont(self.font())
        normal_font.setBold(False)
        label_width = self._measure_label_width(normal_font)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        for role in self._roles_for_mode():
            block = self._raw.get(role)
            if block is None:
                continue
            box = self._build_role_box(role, block, normal_font, label_width)
            self._role_boxes[role] = box
            layout.addWidget(box)
        layout.addStretch(1)

        self._scroll_area.setWidget(container)

    def _measure_label_width(self, normal_font: QFont) -> int:
        """Compute one label width shared by every section's value boxes.

        Every role is wrapped in its own `QGroupBox` with its own grid, so
        nothing would otherwise tell e.g. "Brine"'s labels and "Soil"'s
        labels to agree on a column width -- their editors would each drift
        to wherever their own longest label ends. Measuring the single
        widest label across every currently-visible role/field and using it
        everywhere keeps every value box, in every section, in one column.

        Parameters
        ----------
        normal_font : QFont
            The font labels are actually rendered in, so the measurement
            matches what will really be drawn.

        Returns
        -------
        int
            Fixed label width in px, wide enough for the longest label plus
            a small margin.
        """
        metrics = QFontMetrics(normal_font)
        widths = [
            metrics.horizontalAdvance(_display_label(field_name))
            for role in self._roles_for_mode()
            for field_name in self._raw.get(role, {}).get("values", {})
        ]
        return (max(widths) if widths else 0) + 8

    def _build_role_box(
        self, role: str, block: dict, normal_font: QFont, label_width: int
    ) -> QGroupBox:
        box = QGroupBox(_display_label(role))
        box.setStyleSheet(_NORMAL_BOX_STYLE)

        # `QGroupBox::title { font-weight: bold }` isn't honored by every Qt
        # style (notably the native-themed ones QGIS 4 can run under), so the
        # title's bold weight is set directly on the widget's QFont instead
        # -- which children would normally inherit too, hence every child
        # below gets the (non-bold) `normal_font` explicitly.
        bold_font = QFont(normal_font)
        bold_font.setBold(True)
        box.setFont(bold_font)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)

        entries = list(block.get("values", {}).items())
        for index, (field_name, entry) in enumerate(entries):
            row_index, column = divmod(index, 2)

            label = QLabel(_display_label(field_name))
            label.setFont(normal_font)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            label.setFixedWidth(label_width)

            editor = QLineEdit(str(entry["value"]))
            editor.setFont(normal_font)
            editor.setFixedWidth(100)

            unit_label = QLabel(_display_unit(str(entry.get("unit", ""))))
            unit_label.setFont(normal_font)
            unit_label.setStyleSheet("color: #6b6b6b; font-size: 11px;")
            unit_label.setMinimumWidth(40)

            container = QFrame()
            container.setStyleSheet(_NORMAL_FIELD_STYLE)
            row_layout = QHBoxLayout(container)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.addWidget(label)
            row_layout.addWidget(editor)
            row_layout.addWidget(unit_label)
            row_layout.addStretch(1)

            grid.addWidget(container, row_index, column)
            self._field_rows.append(
                _FieldRow(
                    role=role,
                    field_name=field_name,
                    editor=editor,
                    container=container,
                    original_value=entry["value"],
                )
            )

        box.setLayout(grid)
        return box

    # -- Actions -----------------------------------------------------------

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Settings File", "", "Settings JSON (*.json)"
        )
        if path:
            self._load_file(path)

    def _on_new(self) -> None:
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

        self._load_file(choice.destination_path)

    def _on_save(self) -> None:
        if self.path is None:
            QMessageBox.warning(
                self, "Nothing to save", "Use Browse or New to load a settings file first."
            )
            return

        updated_raw = copy.deepcopy(self._raw)
        parse_failure = self._apply_edits(updated_raw)
        if parse_failure is not None:
            role, field_name, message = parse_failure
            self._clear_error_highlight()
            self._highlight_field(role, field_name)
            self._show_error(
                f"Can't save — {role}.{field_name}: {message}",
                guidance=None,
                status="Save failed — couldn't parse an edited value.",
            )
            return

        try:
            write_settings_json(self.path, updated_raw)
        except ValueError as exc:
            self._handle_validation_failure(str(exc))
            return

        self._raw = updated_raw
        # Rebuild rather than just clear-highlight: each row's
        # `original_value` must re-baseline to what was just saved, or the
        # unsaved-changes check on Close would keep comparing against the
        # pre-save values forever.
        self._rebuild_sections()
        self._error_panel.setVisible(False)
        self._set_status("Saved.")

        # One of the two known trigger points for refreshing an already-
        # exported HHE trench layer -- see refresh_hhe_trench_layer()'s own
        # docstring. A no-op unless this file has a qthermonet_hhe_settings
        # section naming a layer currently loaded in the project.
        refresh_hhe_trench_layer(str(self.path))

    def _apply_edits(self, updated_raw: dict) -> tuple[str, str, str] | None:
        """Write every field row's current text back into `updated_raw`.

        Parameters
        ----------
        updated_raw : dict
            Deep copy of `self._raw` to mutate in place.

        Returns
        -------
        tuple[str, str, str] | None
            `(role, field_name, message)` for the first field that can't be
            parsed back to its original type, or `None` if all succeeded.
        """
        for row in self._field_rows:
            try:
                value = _coerce_value(row.editor.text(), row.original_value)
            except ValueError as exc:
                return row.role, row.field_name, str(exc)
            updated_raw[row.role]["values"][row.field_name]["value"] = value
        return None

    def _on_copy_path(self) -> None:
        if self.path is not None:
            QApplication.clipboard().setText(str(self.path))

    def _list_unsaved_changes(self) -> list[str]:
        """List human-readable descriptions of every unsaved edit.

        Compares each field's current editor text against the value it had
        when last loaded/saved (`row.original_value`).

        Returns
        -------
        list[str]
            One line per changed or unparsable field, e.g.
            ``"Soil → Thermal Conductivity: 2.36 → 2.5"``. Empty if
            nothing is loaded or nothing has changed.
        """
        if self.path is None:
            return []
        changes = []
        for row in self._field_rows:
            text = row.editor.text()
            try:
                value = _coerce_value(text, row.original_value)
            except ValueError:
                changes.append(
                    f"{_display_label(row.role)} → {_display_label(row.field_name)}: "
                    f"invalid value {text!r}"
                )
                continue
            if value != row.original_value:
                changes.append(
                    f"{_display_label(row.role)} → {_display_label(row.field_name)}: "
                    f"{row.original_value} → {value}"
                )
        return changes

    def reject(self) -> None:  # noqa: D102 (Qt override, not new public API)
        changes = self._list_unsaved_changes()
        if changes:
            max_shown = 15
            shown = changes[:max_shown]
            remaining = len(changes) - len(shown)
            details = "\n".join(f"• {line}" for line in shown)
            if remaining:
                details += f"\n…and {remaining} more change{'s' if remaining != 1 else ''}"
            response = QMessageBox.warning(
                self,
                "Unsaved changes",
                "These edits don't match the settings file on disk:\n\n"
                f"{details}\n\nClose without saving?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        super().reject()

    # -- Validation-failure handling ---------------------------------------

    def _handle_validation_failure(self, message: str) -> None:
        problems = _parse_problems(message)
        self._clear_error_highlight()
        for problem in problems:
            self._highlight_field(problem["role"], problem["field"])

        unit_mismatches = [p for p in problems if p["kind"] == "unit_mismatch"]
        guidance = None
        if unit_mismatches:
            names = ", ".join(f"{p['role']}.{p['field']}" for p in unit_mismatches)
            guidance = (
                f"{names} has the wrong unit type — units can't be edited from "
                "this window. Open the file and correct it directly:"
            )
        self._show_error(
            message, guidance=guidance, status="Save failed — settings file did not validate."
        )

    def _show_error(self, message: str, guidance: str | None, status: str) -> None:
        self._error_message_label.setText(message)
        if guidance is not None and self.path is not None:
            self._error_guidance_label.setText(guidance)
            self._error_guidance_label.setVisible(True)
            self._error_path_row.setVisible(True)
            self._error_path_display.setText(str(self.path))
        else:
            self._error_guidance_label.setVisible(False)
            self._error_path_row.setVisible(False)
        self._error_panel.setVisible(True)
        self._set_status(status)

    def _highlight_field(self, role: str, field_name: str | None) -> None:
        box = self._role_boxes.get(role)
        if box is not None:
            box.setStyleSheet(_ERROR_BORDER_STYLE_BOX)
        if field_name is None:
            return
        for row in self._field_rows:
            if row.role == role and row.field_name == field_name:
                row.container.setStyleSheet(_ERROR_BORDER_STYLE_FIELD)

    def _clear_error_highlight(self) -> None:
        for box in self._role_boxes.values():
            box.setStyleSheet(_NORMAL_BOX_STYLE)
        for row in self._field_rows:
            row.container.setStyleSheet(_NORMAL_FIELD_STYLE)

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)


def _parse_problems(message: str) -> list[dict[str, str | None]]:
    """Extract (role, field, kind) from a load_settings ValueError message.

    load_settings() reports every problem in one aggregated message rather
    than structured data, so this is the only way to know which role/field
    a given problem line refers to for highlighting purposes.

    Parameters
    ----------
    message : str
        The exact string of the `ValueError` raised by
        `pythermonet.input.load_settings`.

    Returns
    -------
    list[dict[str, str | None]]
        One entry per parsed problem line: ``{"role", "field", "kind"}``,
        `field` is `None` when the problem is role-level (missing/
        unrecognized fields, or an unknown type tag). `kind` is one of
        `"unit_mismatch"`, `"shape"`, `"fields"`, `"unknown_type"`.
    """
    problems: list[dict[str, str | None]] = []
    for line in message.splitlines():
        match = _PROBLEM_LINE_RE.match(line)
        if not match:
            continue
        role = match.group("role")
        cls = match.group("cls")
        field_name = match.group("field")
        rest = match.group("rest")

        if field_name is not None and "unit '" in rest and "expected, got" in rest:
            kind = "unit_mismatch"
        elif field_name is not None:
            kind = "shape"
        elif cls is not None:
            kind = "fields"
        else:
            kind = "unknown_type"

        problems.append({"role": role, "field": field_name, "kind": kind})
    return problems
