# -*- coding: utf-8 -*-
"""
Created on Fri Mar 20 14:04:19 2026
Contains functions that should be reachable from any processing algorithm
@author: JANA
"""

# utils.py
import json
import os
from pathlib import Path

import requests as rq

def fetch_api_data(X, Y):
    """
    Fetch raw API data for a given point.
    Returns data dict or raises a ValueError with a descriptive message.
    """
    
    # Check if coordinates are within Denmark's bounding box (EPSG:25832)
    DK_X_MIN = 441000
    DK_X_MAX = 894000
    DK_Y_MIN = 6048000
    DK_Y_MAX = 6403000

    if not (DK_X_MIN <= X <= DK_X_MAX and DK_Y_MIN <= Y <= DK_Y_MAX):
        raise ValueError(
            f"Coordinates (X={X}, Y={Y}) are outside Denmark. "
            f"Ground thermal conductivity cannot be estimated."
        )
        
    base_url = "https://data.geus.dk/geusmapmore/termiskejordarter/indexapimodel.jsp"
    params   = {"x": X, "y": Y}

    try:
        # (connect, read) seconds -- same as Get Buildings. Get Ground
        # Conductivity calls this ~10 times per run, so one slow reply under
        # the old flat 10 s failed the whole run (seen 2026-10-01).
        response = rq.get(base_url, params=params, timeout=(20, 120))
    except rq.exceptions.ConnectionError:
        raise ValueError("Could not connect to the GEUS API. Check your internet connection.")
    except rq.exceptions.Timeout:
        raise ValueError(f"The GEUS API request timed out for X={X}, Y={Y}.")
    except rq.exceptions.RequestException as e:
        raise ValueError(f"Unexpected error during API request: {str(e)}")

    if response.status_code != 200:
        raise ValueError(f"API returned status code {response.status_code} for X={X}, Y={Y}.")

    # Try to parse JSON
    try:
        data = response.json()
    except ValueError:
        raise ValueError(f"API returned invalid JSON for X={X}, Y={Y}: {response.text[:200]}")

    # Check for error key in response
    if "error" in data:
        raise ValueError(f"API returned an error for X={X}, Y={Y}: {data['error']}")

    # Check that expected keys are present
    for key in ["layers", "groundlevel", "phreatic"]:
        if key not in data:
            raise ValueError(f"API response missing expected key '{key}' for X={X}, Y={Y}.")

    return data

def calculate_tc(X, Y, depth):
    """
    Fetch API data and calculate weighted average thermal conductivity 
    for a single point with coordinates X,Y down to depth.
    Returns thermal conductivity value or None if the API call fails.
    """

    try:
        data = fetch_api_data(X, Y)
    except ValueError:
        return None
    if depth == 150:
        return data["tc_avg_0m_150m"]

    layers = data["layers"]

    tc_weighted_sum = 0
    total_thickness = 0

    for layer in layers:
        layer_top    = layer["top"]
        layer_bottom = layer["bottom"]

        if layer_bottom <= 0 or layer_top >= depth:
            continue

        clipped_top    = max(layer_top,    0)
        clipped_bottom = min(layer_bottom, depth)

        thickness_within_depth = clipped_bottom - clipped_top

        if thickness_within_depth > 0:
            tc_weighted_sum += layer["tc_corrected_for_phreatic"] * thickness_within_depth
            total_thickness += thickness_within_depth

    if total_thickness > 0:
        return tc_weighted_sum / total_thickness
    else:
        return None
    
def get_representative_point(input_layer):
    """
    Extract a representative point from a vector layer in EPSG:25832.

    Combines every feature's geometry (`QgsGeometry.unaryUnion`) and takes the
    centroid of the result -- for a single polygon this is just its centroid
    (unchanged from before), but it also handles a multi-feature source layer
    (a BHE borefield's many points, or an HHE field's many trench lines)
    without requiring exactly one feature: a point cloud's centroid is the
    plain average of its points, and a multi-line centroid is length-weighted
    along the lines, both reasonable definitions of "the middle of the field."

    Returns (x, y) tuple in EPSG:25832 or raises an exception if input is invalid.
    """
    from qgis.core import (QgsGeometry, QgsVectorLayer, QgsWkbTypes, QgsCoordinateReferenceSystem,
                           QgsCoordinateTransform, QgsProject)

    if not input_layer or not isinstance(input_layer, QgsVectorLayer):
        raise ValueError("Invalid input layer!")

    if input_layer.geometryType() not in [
        QgsWkbTypes.PolygonGeometry,
        QgsWkbTypes.PointGeometry,
        QgsWkbTypes.LineGeometry
    ]:
        raise ValueError("Input layer must be a polygon, point or line!")

    geometries = [
        feature.geometry()
        for feature in input_layer.getFeatures()
        if feature.isValid() and not feature.geometry().isEmpty()
    ]
    if not geometries:
        raise ValueError("Input layer has no features with valid geometry!")

    combined = QgsGeometry.unaryUnion(geometries)

    # Reproject to EPSG:25832 if needed
    source_crs = input_layer.crs()
    target_crs = QgsCoordinateReferenceSystem("EPSG:25832")

    if source_crs != target_crs:
        transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
        combined.transform(transform)

    point = combined.centroid().asPoint()

    return round(point.x()), round(point.y())


PLUGIN_ROOT = Path(__file__).parent
RESOURCES_DIR = PLUGIN_ROOT / "resources"

def get_resource(filename: str) -> str:
    """Build an absolute path to a file bundled in the plugin's resources folder.

    Parameters
    ----------
    filename : str
        Path relative to the ``resources/`` folder, e.g. ``"icon.png"`` or
        ``"logos/logo2.png"``.

    Returns
    -------
    str
        Absolute filesystem path to the resource.
    """
    return str(RESOURCES_DIR / filename)


def get_logo(filename: str) -> str:
    """Build an absolute path to a per-algorithm toolbar/menu icon.

    Parameters
    ----------
    filename : str
        Icon filename inside ``resources/logos/``, e.g. ``"logo2.png"``.

    Returns
    -------
    str
        Absolute filesystem path to the icon.
    """
    return get_resource(f"logos/{filename}")


def dimmed_text_css() -> str:
    """CSS colour for secondary text (hints, status, units) in the current theme.

    The theme's placeholder-text colour: the text colour at reduced opacity,
    so it works in both light and dark mode -- fixed greys were near
    unreadable on Windows' dark theme. Written as ``rgba()`` because
    `QColor.name()` drops the alpha (white at half opacity became white).

    Returns
    -------
    str
        E.g. ``"rgba(255, 255, 255, 128)"``, for a stylesheet ``color:``.
    """
    from qgis.PyQt.QtGui import QPalette
    from qgis.PyQt.QtWidgets import QApplication

    color = QApplication.palette().color(QPalette.ColorRole.PlaceholderText)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


#: Roles that exist only in one settings-file template -- used to detect a
#: loaded file's mode without asking the user, by checking which side's
#: unique roles it has.
_BHE_ONLY_ROLES = frozenset(
    {"grout", "pipe_material_bhe", "borehole", "pipe_segment_bhe", "vhe_field_parameters"}
)
_HHE_ONLY_ROLES = frozenset(
    {"pipe_material_hhe", "pipe_segment_hhe", "hhe_field_parameters"}
)


def detect_mode(settings: dict) -> str | None:
    """Detect whether a settings file/dict is BHE or HHE.

    Shared by the Dimensioning Settings dialog, Source Placement, and
    `full_dimensioning_algorithm.py`, so a settings file's mode is always
    derived the same way instead of asked for separately in each place.

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


def write_settings_json(path: str | Path, raw: dict) -> None:
    """Validate and atomically write a settings file's raw JSON.

    The one place in QThermonet that actually writes a settings file to
    disk: `SettingsEditorDialog._on_save`, Source Placement's HHE export,
    Full Dimensioning's computed-length write-back, and the file-path cache
    below all go through this (directly, or via
    :func:`update_settings_fields`). Writes `raw` to a temp file, validates
    it with `pythermonet.input.load_settings` (the real file is never
    touched if invalid), then atomically replaces `path`. Deliberately
    never goes through `pythermonet.output.save_settings` (which
    reconstructs the file purely from typed domain objects and would
    silently drop anything -- like QThermonet's reserved `qthermonet_`-
    prefixed sections -- not represented in them); writing raw JSON directly
    naturally carries forward everything this call doesn't explicitly touch.

    Parameters
    ----------
    path : str
        Settings file path.
    raw : dict
        The complete settings JSON to write.

    Raises
    ------
    ValueError
        If `raw` doesn't validate against pythermonet's schema. The real
        file is left untouched.
    """
    from pythermonet.input import load_settings

    p = Path(path)
    temp_path = p.with_suffix(p.suffix + ".tmp")
    temp_path.write_bytes((json.dumps(raw, indent=2) + "\n").encode("utf-8"))
    try:
        load_settings(temp_path)
    except ValueError:
        os.remove(temp_path)
        raise
    os.replace(temp_path, p)


def update_settings_fields(path: str, updates: dict[str, dict[str, object]]) -> None:
    """Update specific field values in a settings file, leaving everything else untouched.

    Parameters
    ----------
    path : str
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


#: The reserved settings-file key holding QThermonet's cross-tool file-path
#: cache -- see the module docstring section below for the full schema and
#: the read/write rules around it.
_QTHERMONET_CACHE_KEY = "qthermonet_cache"


def _read_qthermonet_cache(path: str) -> dict[str, str]:
    """Read a settings file's `qthermonet_cache` section, if any.

    Parameters
    ----------
    path : str
        Settings file path.

    Returns
    -------
    dict of str to str
        `{role: path}`, or `{}` if the file can't be read/parsed or has no
        cache section yet.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    cache = raw.get(_QTHERMONET_CACHE_KEY)
    return dict(cache) if isinstance(cache, dict) else {}


def _write_qthermonet_cache(path: str, updates: dict[str, str]) -> None:
    """Merge `updates` into a settings file's `qthermonet_cache` section.

    Additive: only the given roles change, every other role already cached
    (and everything else in the file) is left as-is.

    Parameters
    ----------
    path : str
        Settings file path.
    updates : dict of str to str
        `{role: path}` entries to merge in.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cache = raw.get(_QTHERMONET_CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
    cache.update(updates)
    raw[_QTHERMONET_CACHE_KEY] = cache
    write_settings_json(path, raw)


def _current_project_key() -> str:
    """A stable key for the current QGIS project, for the session cache below."""
    from qgis.core import QgsProject

    return QgsProject.instance().fileName()


#: Per-project, in-memory, session-lifetime cache -- reset on plugin
#: reload/QGIS restart, never persisted directly. Each project's slot is
#: `{"settings_path": str | None, "pending": dict[str, str],
#: "setup_asked": bool, "crs_choice": str}`:
#:
#: - `settings_path` is the settings file currently active for that
#:   project, or `None` if none has been opened/created yet this session.
#: - `pending` holds `{role: path}` entries for files produced *before* a
#:   settings file exists to hold them (e.g. the AOI layer used by "Get
#:   Buildings and BBR", which runs before any settings file is involved).
#:   Once a settings file becomes active, its `qthermonet_cache` section
#:   (see `_read_qthermonet_cache`/`_write_qthermonet_cache`) is the sole
#:   source of truth for every role, and `pending` is cleared -- there is
#:   deliberately no in-memory mirror of settings-file-backed roles, so
#:   there is nothing to go stale or need clearing when the active settings
#:   file changes.
#: - `setup_asked`/`crs_choice`: see `maybe_prompt_project_setup` below --
#:   deliberately session-cached, not settings-file-backed, since which CRS
#:   to reproject into is a per-session QGIS/UX decision, not a physical
#:   property of the thermonet design itself.
#: - `overwrite_confirm_off`: see `overwrite_confirm_disabled` below.
_session_cache: dict[str, dict] = {}


def _project_slot() -> dict:
    return _session_cache.setdefault(_current_project_key(), {"settings_path": None, "pending": {}})


def get_current_settings_path() -> str | None:
    """The settings file currently active for the current QGIS project.

    Returns
    -------
    str | None
        The path, or `None` if no settings file has been opened/created yet
        this session for this project.
    """
    return _project_slot()["settings_path"]


def load_existing_settings_path(path: str) -> None:
    """Register `path` as the active settings file, having just been opened.

    If this is the first settings file opened/created this session for the
    current project, any `pending` entries (files produced before a
    settings file existed) are merged into `path`'s `qthermonet_cache` --
    `pending` overwrites matching roles already in the file, since the
    session's data is fresher -- and then cleared. If a settings file was
    already active, this just repoints to `path`; there is no other
    session-side state to reconcile (see `_session_cache`'s docstring).

    Parameters
    ----------
    path : str
        The settings file that was just opened.
    """
    slot = _project_slot()
    if slot["settings_path"] is None and slot["pending"]:
        try:
            merged = {**_read_qthermonet_cache(path), **slot["pending"]}
            _write_qthermonet_cache(path, merged)
        except (OSError, json.JSONDecodeError, ValueError):
            pass  # Best-effort: leave `pending` intact so a later attempt can retry.
        else:
            slot["pending"] = {}
    slot["settings_path"] = path
    _link_settings_path_to_project(path)


def create_new_settings_path(path: str) -> None:
    """Register `path` as the active settings file, having just been created.

    Carries the "current cache" forward into the new file: the previously
    active settings file's own `qthermonet_cache` (read fresh from disk) if
    one was active, otherwise `pending`. This is why creating a new
    settings file (e.g. a BHE/HHE variant of the same project) inherits
    already-known file paths, while opening a genuinely different existing
    settings file (:func:`load_existing_settings_path`) does not.

    Parameters
    ----------
    path : str
        The newly created settings file.
    """
    slot = _project_slot()
    previous_path = slot["settings_path"]
    carry_forward = _read_qthermonet_cache(previous_path) if previous_path else dict(slot["pending"])
    if carry_forward:
        try:
            _write_qthermonet_cache(path, carry_forward)
        except (OSError, json.JSONDecodeError, ValueError):
            pass  # Best-effort: leave `pending` intact so a later attempt can retry.
        else:
            slot["pending"] = {}
    else:
        slot["pending"] = {}
    slot["settings_path"] = path
    _link_settings_path_to_project(path)


#: Where the active settings file is remembered inside the QGIS project file
#: (`QgsProject.writeEntry`), so reopening the project -- even after a QGIS
#: restart -- reactivates it. The paths themselves stay in the settings
#: file's `qthermonet_cache`; the project only stores which file that is.
_PROJECT_ENTRY_SCOPE = "QThermonet"
_PROJECT_ENTRY_KEY = "settings_path"


def _link_settings_path_to_project(path: str) -> None:
    """Store `path` in the project, only if it changed (avoids a needless "unsaved")."""
    from qgis.core import QgsProject

    project = QgsProject.instance()
    current, ok = project.readEntry(_PROJECT_ENTRY_SCOPE, _PROJECT_ENTRY_KEY)
    if not ok or current != path:
        project.writeEntry(_PROJECT_ENTRY_SCOPE, _PROJECT_ENTRY_KEY, path)


#: The project file name seen last, to tell the transitions apart (signal
#: order verified 2026-10-01): opening a project goes name → "" (cleared) →
#: readProject → name; a first save goes "" → name with no readProject; a
#: "Save As" goes straight from one name to another, before the file is
#: written.
_project_state = {"file_name": "", "reading": False}


def on_project_read(_document=None) -> None:
    """Reactivate the settings file the opened project is linked to.

    Connected to `QgsProject.readProject`; does nothing if the project has
    no link or the linked file no longer exists.

    Parameters
    ----------
    _document : QDomDocument or None
        Passed by the signal; unused.

    """
    from qgis.core import QgsProject

    _project_state["reading"] = True
    path, ok = QgsProject.instance().readEntry(_PROJECT_ENTRY_SCOPE, _PROJECT_ENTRY_KEY)
    if not ok or not path:
        return
    if os.path.isfile(path):
        load_existing_settings_path(path)
    else:
        # Linked file deleted/moved: don't keep pointing at it from earlier
        # in this session -- the tools then start without saved paths.
        _project_slot()["settings_path"] = None


def on_project_file_name_changed() -> None:
    """Handle "Save As" (reset the link) and a first save (keep the session).

    Connected to `QgsProject.fileNameChanged`.
    """
    from qgis.core import QgsProject

    project = QgsProject.instance()
    old, new = _project_state["file_name"], project.fileName()
    if old and new and os.path.normcase(old) != os.path.normcase(new):
        # Save As: the copy starts without a settings file, like a new
        # project (its session slot is new too, being keyed by file name).
        project.removeEntry(_PROJECT_ENTRY_SCOPE, _PROJECT_ENTRY_KEY)
    elif not old and new and not _project_state["reading"]:
        # First save of a new project: keep what this session already knew.
        _carry_session_over("", new)
    _project_state["file_name"] = new
    _project_state["reading"] = False


def on_project_cleared() -> None:
    """Forget the unsaved project's session state on "New project" / before opening one.

    Connected to `QgsProject.cleared`.
    """
    _session_cache.pop("", None)


def _carry_session_over(old_key: str, new_key: str) -> None:
    """Move one project's session slot to another key (first save of a new project)."""
    old_slot = _session_cache.pop(old_key, None)
    if not old_slot:
        return
    new_slot = _session_cache.setdefault(new_key, {"settings_path": None, "pending": {}})
    new_slot["pending"] = {**old_slot.get("pending", {}), **new_slot.get("pending", {})}
    if new_slot.get("settings_path") is None and old_slot.get("settings_path"):
        new_slot["settings_path"] = old_slot["settings_path"]
    for key in ("setup_asked", "crs_choice", "overwrite_confirm_off"):
        if key in old_slot and key not in new_slot:
            new_slot[key] = old_slot[key]


def connect_project_signals() -> None:
    """Connect the project-link handlers above; call once when the plugin loads."""
    from qgis.core import QgsProject

    project = QgsProject.instance()
    project.readProject.connect(on_project_read)
    project.fileNameChanged.connect(on_project_file_name_changed)
    project.cleared.connect(on_project_cleared)
    _project_state["file_name"] = project.fileName()
    if project.fileName():
        # Plugin (re)loaded with a project already open.
        on_project_read()
        _project_state["reading"] = False


def disconnect_project_signals() -> None:
    """Disconnect the project-link handlers; call when the plugin unloads."""
    from qgis.core import QgsProject

    project = QgsProject.instance()
    for signal, handler in (
        (project.readProject, on_project_read),
        (project.fileNameChanged, on_project_file_name_changed),
        (project.cleared, on_project_cleared),
    ):
        try:
            signal.disconnect(handler)
        except (TypeError, RuntimeError):
            pass


def get_cached_path(role: str) -> str | None:
    """The most recently cached path for `role`, for the current project.

    Never falls back between sources: if a settings file is active, only
    its `qthermonet_cache` is consulted (a role missing there means "not
    cached," not "check `pending` instead") -- otherwise only `pending` is
    consulted. Mixing the two could resurrect a stale path from a different
    point in time.

    Parameters
    ----------
    role : str
        Cache key, e.g. `"borefield_file"`.

    Returns
    -------
    str | None
        The cached path, or `None` if nothing is cached for `role` yet.
    """
    slot = _project_slot()
    if slot["settings_path"] is not None:
        return _read_qthermonet_cache(slot["settings_path"]).get(role)
    return slot["pending"].get(role)


def set_cached_path(role: str, path: str) -> None:
    """Remember `path` under `role`, for the current project.

    Written straight into the active settings file's `qthermonet_cache` if
    one exists, otherwise staged in `pending` until one does. Failures
    (e.g. the active settings file was deleted mid-session) are swallowed
    -- this is best-effort bookkeeping and must never break the calling
    tool's actual output.

    Parameters
    ----------
    role : str
        Cache key, e.g. `"borefield_file"`.
    path : str
        The path to remember.
    """
    slot = _project_slot()
    if slot["settings_path"] is not None:
        try:
            _write_qthermonet_cache(slot["settings_path"], {role: path})
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    else:
        slot["pending"][role] = path


def read_datafordeler_api_key() -> str:
    """Read the Datafordeler API key from the plugin's ``.env`` file.

    Parses the minimal ``KEY=value`` format directly instead of depending on
    ``python-dotenv``, which is not guaranteed to be present in QGIS's
    bundled Python environment.

    Returns
    -------
    str
        The value of ``DATAFORDELER_API_KEY``.

    Raises
    ------
    ValueError
        If ``.env`` is missing, or has no non-empty ``DATAFORDELER_API_KEY``
        entry.
    """
    env_path = PLUGIN_ROOT / ".env"
    if not env_path.exists():
        raise ValueError(
            f"Missing {env_path}. Copy .env-template to .env and set DATAFORDELER_API_KEY."
        )

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DATAFORDELER_API_KEY":
            api_key = value.strip().strip('"').strip("'")
            if api_key:
                return api_key

    raise ValueError(f"DATAFORDELER_API_KEY not set in {env_path}.")


def default_save_directory() -> str:
    """The current QGIS project's folder, for seeding a file/folder-save dialog.

    Used so a new output file (or a new settings file) defaults to sitting
    next to the project it belongs to, rather than wherever Qt's own
    fallback -- the last folder used in any dialog this session, or the OS
    default -- happens to be. Reachable from any processing algorithm or
    dialog in this plugin, per this module's purpose.

    Returns
    -------
    str
        The project file's parent directory, or ``""`` if the project
        hasn't been saved yet. ``os.path.join("", filename)`` is just
        ``filename``, so callers can use this unconditionally as the
        directory component of a suggested save path.
    """
    from qgis.core import QgsProject

    project_path = QgsProject.instance().fileName()
    return str(Path(project_path).parent) if project_path else ""


def warn_if_project_unsaved(parent) -> None:
    """Nudge the user if the project has no folder to default a file dialog to.

    Not fatal -- a file dialog opened right after this still works either
    way, just without a sensible starting folder (Qt falls back to the
    last-used directory, or the OS default) instead of the project's own.

    Parameters
    ----------
    parent : QWidget
        Parent widget for the message box (typically the dialog about to
        open a file-save dialog).
    """
    if default_save_directory():
        return

    from qgis.PyQt.QtWidgets import QMessageBox

    QMessageBox.information(
        parent,
        "Project not saved",
        "This QGIS project hasn't been saved yet, so the file dialog won't "
        "default to the project's folder. Consider saving the project first.",
    )


#: Rough bounding boxes in WGS84 lon/lat -- a quick sanity check, not an
#: authoritative administrative-boundary lookup. Bornholm sits east of
#: mainland Denmark's UTM zone (32N) and needs its own zone (33N), so it
#: gets its own, tighter box checked first.
_BORNHOLM_BBOX_WGS84 = (14.6, 54.9, 15.3, 55.35)   # (min_lon, min_lat, max_lon, max_lat)
_DENMARK_MAINLAND_BBOX_WGS84 = (8.0, 54.5, 13.0, 57.8)


def _suggest_danish_crs(point_wgs84) -> tuple[str, str] | None:
    """`(epsg_authid, region_label)` if `point_wgs84` falls in Denmark or
    Bornholm's rough bounding box, else `None`.

    Parameters
    ----------
    point_wgs84 : QgsPointXY
        A point already in WGS84 (EPSG:4326) lon/lat.

    Returns
    -------
    tuple of (str, str) or None
        `("EPSG:25832", "Denmark")`, `("EPSG:25833", "Bornholm")`, or
        `None` if outside both boxes.
    """
    lon, lat = point_wgs84.x(), point_wgs84.y()
    if _BORNHOLM_BBOX_WGS84[0] <= lon <= _BORNHOLM_BBOX_WGS84[2] and _BORNHOLM_BBOX_WGS84[1] <= lat <= _BORNHOLM_BBOX_WGS84[3]:
        return "EPSG:25833", "Bornholm"
    if _DENMARK_MAINLAND_BBOX_WGS84[0] <= lon <= _DENMARK_MAINLAND_BBOX_WGS84[2] and _DENMARK_MAINLAND_BBOX_WGS84[1] <= lat <= _DENMARK_MAINLAND_BBOX_WGS84[3]:
        return "EPSG:25832", "Denmark"
    return None


def maybe_prompt_project_setup(parent, crs=None, point=None, feedback=None) -> bool:
    """Once per project session: prompt to save an unsaved project, and
    suggest a recommended Denmark/Bornholm CRS based on a representative point.

    Fires at most once per project (session-cached via `setup_asked`) --
    call this unconditionally at the top of any routine that takes
    coordinate input; it no-ops immediately on every call after the first
    for a given project. Deliberately session-cached rather than persisted
    to the settings file -- see the module-level `_session_cache` docstring.

    Two independent prompts, in order:

    1. If the project has never been saved (`default_save_directory()` is
       empty), an active "Save Now" button (triggers QGIS's own Save-As
       action) vs. "Continue Without Saving" -- a passive nudge like
       `warn_if_project_unsaved` risks being overlooked, and this project
       needing a stable file is what makes the CRS choice below (and the
       rest of the session cache) actually persist for the project's
       lifetime rather than colliding with every other unsaved project.
    2. If `point` (in `crs`) falls in Denmark or Bornholm's rough bounding
       box, asks whether to use the recommended UTM zone for reprojections
       in QThermonet (`project_crs_choice()` reads the answer back).
       Declining, being outside both boxes, or passing no `crs`/`point`
       leaves the existing `EPSG:3857` fallback in place -- this never
       changes any algorithm's behavior by itself, it only records a
       preference for `project_crs_choice()` to be read later.

    Both prompts require a GUI (skipped entirely, falling back to
    `EPSG:3857`, when `qgis.utils.iface` is `None` -- e.g. running headless
    via `qgis_process`) -- detection is a nicety, never something that
    should block or fail an algorithm run.

    Parameters
    ----------
    parent : QWidget or None
        Parent widget for the message boxes. Processing algorithms don't
        have a natural widget parent to pass -- `None` is fine, the dialogs
        still show, just unparented.
    crs : QgsCoordinateReferenceSystem or None
        The CRS `point` is expressed in -- typically an already-validated
        input layer's `.crs()`, or a map canvas's `destinationCrs()` for a
        picked point rather than a layer. `None` (with `point` also `None`)
        skips the CRS suggestion but still runs the unsaved-project check.
    point : QgsPointXY or None
        A representative point for the project's data, in `crs` --
        typically a layer's `.extent().center()`, or a directly picked
        point (e.g. Source Placement's connection node). Required together
        with `crs`; either alone is treated as "not given."
    feedback : QgsProcessingFeedback or None
        If given, used to explain why the prompts were skipped when running
        headless. Purely informational.

    Returns
    -------
    bool
        `True` if the project was just saved for the first time during this
        call (via "Save Now"). A caller running as part of a
        `QgsProcessingAlgorithm` should treat this as "the parameter values
        already resolved for this run (e.g. an output path defaulted from
        the project's folder) were captured before the project had a
        location, and are now stale" -- see `prepare_algorithm_project_setup`,
        which uses this to abort the run and ask the user to re-open the
        tool rather than silently proceeding with outdated defaults.

    """
    slot = _project_slot()
    if slot.get("setup_asked"):
        return False
    slot["setup_asked"] = True

    from qgis.utils import iface as _iface

    if _iface is None:
        if feedback is not None:
            feedback.pushInfo(
                "Running headless -- skipping the project-save/coordinate-system prompts."
            )
        return False

    from qgis.PyQt.QtWidgets import QMessageBox

    just_saved = False
    if not default_save_directory():
        box = QMessageBox(parent)
        box.setWindowTitle("Project not saved")
        box.setText(
            "This QGIS project hasn't been saved yet. Some QThermonet "
            "helper/pathing functionality (default file-save locations, "
            "remembering paths between tools, and which coordinate system "
            "to use) only works once the project has a file, and may not "
            "behave as expected until then. Save it now?"
        )
        save_button = box.addButton("Save Now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Continue Without Saving", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is save_button:
            _iface.actionSaveProjectAs().trigger()
            just_saved = bool(default_save_directory())
            # Saving just changed the project's filename, which is the
            # session cache's key (see _current_project_key) -- re-fetch so
            # the writes below land in the now-current, stable slot instead
            # of the old, now-orphaned empty-filename one. But re-fetching
            # alone isn't enough: anything already cached this session under
            # the old (empty-filename) slot -- pending file paths from
            # earlier tool runs (get_cached_path/set_cached_path), or an
            # already-open settings file -- would otherwise be silently
            # forgotten, since a new key starts as a brand-new empty slot.
            # Migrate it forward. If the save was cancelled (project still
            # unsaved), the key hasn't actually changed and this is a no-op
            # (same slot object, nothing to migrate).
            old_slot = slot
            slot = _project_slot()
            if slot is not old_slot:
                slot["pending"] = {**old_slot.get("pending", {}), **slot.get("pending", {})}
                if slot.get("settings_path") is None and old_slot.get("settings_path") is not None:
                    slot["settings_path"] = old_slot["settings_path"]
            slot["setup_asked"] = True

    slot.setdefault("crs_choice", "EPSG:3857")

    if crs is None or point is None:
        return just_saved

    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
    center_wgs84 = transform.transform(point)
    suggestion = _suggest_danish_crs(center_wgs84)
    if suggestion is None:
        return just_saved

    epsg, region = suggestion
    answer = QMessageBox.question(
        parent,
        "Coordinate system",
        f"This project's data looks like it's in {region}. Set the QGIS "
        f"project's coordinate system to the recommended {epsg}, and use "
        f"it for QThermonet's reprojections?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )
    if answer == QMessageBox.StandardButton.Yes:
        slot["crs_choice"] = epsg
        # Not just an internal QThermonet preference -- the project's own
        # display/map CRS should actually change too, so what's shown in
        # QGIS matches what QThermonet reprojects into.
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(epsg))

    return just_saved


def raise_if_nan(value, what: str) -> None:
    """Stop the run with a clear message if a QGIS length/distance came back NaN.

    Seen 2026-10-01: in a long-running QGIS session every ellipsoidal
    measurement (`QgsDistanceArea`, `$length`) returned NaN until QGIS was
    restarted -- Service Pipes then silently produced an empty layer. Called
    from inside `processAlgorithm`, so the run's temp files are discarded and
    no output is changed.

    Parameters
    ----------
    value : float or None
        The measured value.
    what : str
        What was measured, for the message (e.g. "a pipe length").

    Raises
    ------
    QgsProcessingException
        If `value` is NaN or not a number.

    """
    import math

    from qgis.core import QgsProcessingException

    try:
        is_nan = value is None or math.isnan(float(value))
    except (TypeError, ValueError):
        is_nan = True
    if is_nan:
        raise QgsProcessingException(
            f"QGIS returned an invalid value (NaN) for {what}. This happens when the "
            "QGIS session gets into a bad state -- save your project, restart QGIS "
            "and run the tool again. No files were changed."
        )


def open_algorithm_dialog(algorithm_id: str, parameters: dict | None = None) -> None:
    """Open a Processing tool's dialog the way QGIS's own Processing Toolbox does.

    Like QGIS 4's `ProcessingPlugin.executeAlgorithm`: create the dialog and
    `exec()` it, without reading its results afterwards.
    `processing.execAlgorithmDialog` does read them (`widget.results()`),
    which fails with "wrapped C/C++ object of type AlgorithmWidget has been
    deleted" when the dialog has already deleted itself on close (seen
    2026-09-30). QThermonet's menu never uses those results anyway.

    Parameters
    ----------
    algorithm_id : str
        E.g. ``"QThermonet:full_dimensioning"``.
    parameters : dict or None
        Initial parameter values for the dialog.

    """
    import processing

    dialog = processing.createAlgorithmDialog(algorithm_id, parameters or {})
    if dialog is not None:
        dialog.exec()


def overwrite_confirm_disabled() -> bool:
    """Whether the overwrite confirmation is switched off for this project.

    Session-cached per project, like `setup_asked` -- resets on QGIS restart
    or project switch, deliberately never persisted.

    Returns
    -------
    bool
        `True` once the user ticked "Don't warn me again" in
        `output_handling.confirm_overwrite` for the current project.
    """
    return bool(_project_slot().get("overwrite_confirm_off", False))


def disable_overwrite_confirm() -> None:
    """Switch the overwrite confirmation off for this project, this session."""
    _project_slot()["overwrite_confirm_off"] = True


def project_crs_choice() -> str:
    """The current project's recommended-CRS choice, cached this session.

    Returns
    -------
    str
        An EPSG authid (e.g. ``"EPSG:25832"``), or the ``"EPSG:3857"``
        fallback if `maybe_prompt_project_setup` hasn't run yet this
        project, or the user declined/was outside Denmark and Bornholm.
    """
    return _project_slot().get("crs_choice", "EPSG:3857")


def prepare_algorithm_project_setup(
    algorithm, parameters, context, feedback, layer_parameter_name=None
) -> bool:
    """Standard `prepareAlgorithm` body for a coordinate-input QThermonet algorithm.

    `prepareAlgorithm` always runs on the main thread, unlike
    `processAlgorithm` (which runs in the background by default) -- the only
    safe place to call `maybe_prompt_project_setup` without forcing the
    whole algorithm onto the main thread via `flags()`/`FlagNoThreading`
    (which would also make QGIS block on the run with a modal "processing"
    dialog, same as `get_ground_conductivity_algorithm.py`'s matplotlib
    workaround -- not something every algorithm should inherit just for
    this). Every algorithm that needs the project-setup prompt should call
    this, one line, from its own `prepareAlgorithm`, rather than
    reimplementing the layer lookup + call each time.

    Parameters
    ----------
    algorithm : QgsProcessingAlgorithm
        The algorithm instance (pass `self`, from its own `prepareAlgorithm`).
    parameters : dict
        As received by the caller's `prepareAlgorithm`.
    context : QgsProcessingContext
        As received by the caller's `prepareAlgorithm`.
    feedback : QgsProcessingFeedback
        As received by the caller's `prepareAlgorithm`.
    layer_parameter_name : str or None
        The parameter name of a vector-layer input to read `.crs()`/
        `.extent()` from for the CRS suggestion, e.g. `self.PIPES_LAYER`.
        `None` for an algorithm with no layer input this early (e.g. Full
        Dimensioning, whose inputs are `.dat`/`.json` file paths) -- still
        runs the unsaved-project check, just skips the CRS suggestion.

    Returns
    -------
    bool
        For the caller to return directly. Normally `True`. `False` if the
        project was just saved for the first time during this call (see
        `maybe_prompt_project_setup`'s return value) -- in that case, this
        run's already-resolved parameters (e.g. an output path defaulted
        from the project's folder before it had one) are stale, so the run
        is aborted and the same tool's dialog is automatically reopened
        (`open_algorithm_dialog`, deferred via `QTimer.singleShot`
        to avoid re-entrancy with the still-unwinding aborted run) with
        fresh defaults -- previously entered parameter values, including
        input layer selections, are not carried over into the reopened
        dialog.

    """
    layer = (
        algorithm.parameterAsVectorLayer(parameters, layer_parameter_name, context)
        if layer_parameter_name is not None else None
    )
    if layer:
        just_saved = maybe_prompt_project_setup(
            None, crs=layer.crs(), point=layer.extent().center(), feedback=feedback
        )
    else:
        just_saved = maybe_prompt_project_setup(None, feedback=feedback)

    if just_saved:
        feedback.reportError(
            "The project was just saved. Re-opening this tool with fresh "
            "defaults (e.g. an output location that now points at the "
            "project's folder) -- previously entered values, including "
            "input layer selections, are not carried over. Please run it "
            "again."
        )
        from qgis.utils import iface as _iface
        if _iface is not None:
            from qgis.PyQt.QtCore import QTimer

            algorithm_id = algorithm.id()
            # Deferred to the next event-loop iteration rather than called
            # directly here -- this run (and its dialog) is still unwinding
            # from returning False; opening a new dialog for the same
            # algorithm before that finishes risks re-entrancy issues.
            QTimer.singleShot(0, lambda: open_algorithm_dialog(algorithm_id))
        return False

    return True
