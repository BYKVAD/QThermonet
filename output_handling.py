# -*- coding: utf-8 -*-
"""Shared output handling for every QThermonet tool that writes files.

Two pieces, used together (see
``claude/plans/2026-09-25-step1-output-handling-plan.md``):

- :func:`confirm_overwrite` -- the Continue/Cancel pop-up listing output
  files that already exist. Called from ``prepareAlgorithm`` (GUI thread).
- :class:`OutputSet` -- a run writes every output to a temporary file in a
  ``.qthermonet-tmp/`` folder next to the real file, and only after the run
  succeeded does :meth:`OutputSet.commit` (GUI thread, from
  ``postProcessAlgorithm``) briefly point any loaded layer on the real file
  at an in-memory placeholder, swap the file in, point the layer back at it
  (same layer: ID, style, position, visibility and joins all kept), and
  cache the path.

Why the swap exists: a loaded layer keeps its file open on Windows, so
writing over it directly fails ("Permission denied"); detaching the layer
from the file first releases the lock. Why it runs after the run: layers and the project
must only be touched from the GUI thread, and a failed run must leave the
real files untouched.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsDataProvider,
    QgsMemoryProviderUtils,
    QgsProcessingException,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QEvent

from . import utils

TEMP_FOLDER_NAME = ".qthermonet-tmp"


class OutputCommitError(QgsProcessingException):
    """Swapping a finished run's outputs into place failed partway."""


def _map_canvas():
    """QGIS's main map canvas, or `None` when running headless."""
    from qgis.utils import iface

    return iface.mapCanvas() if iface is not None else None


def _replace_or_flush_and_retry(source: str, target: str) -> None:
    """`os.replace`; if the target is still held, let QGIS close it and retry once.

    Removing a layer doesn't close its file straight away: QGIS's OGR
    connection pool only schedules closing it (`deleteLater`), which runs
    when control returns to the event loop -- and plain `processEvents()`
    doesn't run it. Flushing those scheduled deletions fixes that, but it
    flushes *every* pending deletion in QGIS, not just the pool's -- so only
    do it when the plain rename actually failed.
    """
    try:
        os.replace(source, target)
    except OSError:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        os.replace(source, target)


def write_geojson(path, fields, geometry_type, crs, features) -> None:
    """Write features to a GeoJSON file, and check the file kept its CRS.

    Goes through a memory layer + `QgsVectorFileWriter.writeAsVectorFormatV3`
    -- the path every QThermonet output that reliably kept its CRS uses.
    Writing features directly (`QgsVectorFileWriter(...)` or `.create()`)
    has produced GeoJSONs with no CRS at all in some sessions (seen in
    `load_calculation_algorithm.py` earlier, and 2026-09-30 for trenches and
    pipe topology), which QGIS then reads as WGS84 -- an invisible layer.

    Parameters
    ----------
    path : str
        Output `.geojson` path (normally a temp path from `OutputSet`).
    fields : QgsFields
        Attribute fields of `features`.
    geometry_type : QgsWkbTypes.Type
        E.g. ``QgsWkbTypes.LineString``.
    crs : QgsCoordinateReferenceSystem
        CRS the features' coordinates are in.
    features : iterable of QgsFeature
        Features to write.

    Raises
    ------
    OSError
        If `crs` is invalid, writing fails, or the written file doesn't
        carry `crs`.

    """
    name = os.path.basename(path)
    if not crs.isValid():
        raise OSError(f"Refusing to write {name}: no valid coordinate reference system given.")
    if not crs.authid():
        # GeoJSON can only reference a CRS by code -- GDAL writes no "crs" at
        # all for a custom one, and QGIS would read the file back as WGS84.
        raise OSError(
            f"Refusing to write {name}: its coordinate system has no code (e.g. "
            "EPSG:25832), so it can't be saved in a GeoJSON file. Please use a "
            "standard CRS for the project and layers."
        )

    layer = QgsVectorLayer(QgsWkbTypes.displayString(geometry_type), "output", "memory")
    layer.setCrs(crs)
    layer.dataProvider().addAttributes(fields.toList())
    layer.updateFields()
    features = list(features)
    ok, _ = layer.dataProvider().addFeatures(features)
    if not ok or layer.featureCount() != len(features):
        # e.g. an attribute value that can't be converted to its field type,
        # or a geometry of the wrong type -- without this the file would be
        # written with features silently missing (verified 2026-10-01).
        errors = layer.dataProvider().errors()
        reason = f" ({errors[-1]})" if errors else ""
        raise OSError(
            f"Could not write {name}: only {layer.featureCount()} of {len(features)} "
            f"features could be stored{reason}."
        )
    layer.updateExtents()

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GeoJSON"
    options.fileEncoding = "UTF-8"
    error = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), options
    )
    error_code = error[0] if isinstance(error, tuple) else error
    if error_code != QgsVectorFileWriter.NoError:
        raise OSError(f"Could not write {name}: {error}")

    # Check the file's own "crs" member -- exactly what went missing in the
    # broken files. Plain text only: no WKT/GDAL/PROJ round trip, which
    # itself failed ("OGR Error: Corrupt data" on crs.toWkt()) in a live
    # QGIS session (2026-10-01), and no QGIS layer, whose pooled handle on
    # the temp file would block the swap.
    written = _geojson_crs_authid(path)
    expected = crs.authid().upper()
    if expected in ("OGC:CRS84", "EPSG:4326"):
        expected = "EPSG:4326"  # GeoJSON writes EPSG:4326 as CRS84 -- same system
    if written != expected:
        raise OSError(
            f"{name} was written without its coordinate system ({crs.authid()}; "
            f"found: {written or 'none'}) -- it would show up in the wrong place. "
            "Please report this."
        )


def _geojson_crs_authid(path: str) -> str | None:
    """The CRS a GeoJSON file declares, as an authid (e.g. "EPSG:25832"), or None.

    Reads only the start of the file: GDAL writes the "crs" member before the
    features. ``urn:ogc:def:crs:<AUTHORITY>:<version>:<CODE>`` →
    ``<AUTHORITY>:<CODE>``, e.g. ``urn:ogc:def:crs:EPSG::25832`` →
    ``EPSG:25832``, ``urn:ogc:def:crs:ESRI::102001`` → ``ESRI:102001``;
    ``urn:ogc:def:crs:OGC:1.3:CRS84`` → ``EPSG:4326``.
    """
    import re

    with open(path, encoding="utf-8", errors="replace") as f:
        head = f.read(4096)
    match = re.search(r'"crs"\s*:\s*\{.*?"name"\s*:\s*"([^"]+)"', head, re.DOTALL)
    if match is None:
        return None
    name = match.group(1).upper()
    if name.endswith("CRS84"):
        return "EPSG:4326"
    urn = re.fullmatch(r"URN:OGC:DEF:CRS:([A-Z0-9_]+):[^:]*:(.+)", name)
    if urn:
        return f"{urn.group(1)}:{urn.group(2)}"
    return name  # e.g. a plain "EPSG:25832"


def normalize_path(path: str) -> str:
    """Normalize a file path or layer source for comparison.

    Strips an OGR sublayer suffix (e.g. ``"file.geojson|layername=x"``)
    and normalizes case and separators, so a layer's ``source()`` can be
    compared against a plain output path.

    Parameters
    ----------
    path : str
        A file path or a layer source string.

    Returns
    -------
    str
        The normalized absolute path.

    """
    return os.path.normcase(os.path.abspath(path.split("|", 1)[0]))


def confirm_overwrite(paths: Sequence[str], parent=None) -> bool:
    """Ask before overwriting output files that already exist.

    Returns `True` without asking when none of `paths` exist, when the user
    switched the warning off earlier this session for this project, or when
    running headless. Must be called from the GUI thread (normally
    ``prepareAlgorithm``).

    Parameters
    ----------
    paths : sequence of str
        Output file paths the run will write.
    parent : QWidget or None
        Parent widget for the dialog.

    Returns
    -------
    bool
        `True` to go ahead, `False` if the user pressed Cancel.

    """
    existing = [p for p in paths if p and os.path.isfile(p)]
    if not existing or utils.overwrite_confirm_disabled():
        return True

    from qgis.utils import iface

    if iface is None:
        return True

    from qgis.PyQt.QtWidgets import QCheckBox, QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Overwrite existing files?")
    box.setText("This run will overwrite these existing files:")
    box.setInformativeText("\n".join(existing))
    continue_button = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    checkbox = QCheckBox("Don't warn me again about overwriting (this project, this session)")
    box.setCheckBox(checkbox)
    box.exec()

    if box.clickedButton() is not continue_button:
        return False
    if checkbox.isChecked():
        utils.disable_overwrite_confirm()
    return True


@dataclass
class _FileEntry:
    path_real: str
    path_temp: str
    layer_name: str | None
    cache_roles: tuple[str, ...]
    style_default: Callable[[QgsVectorLayer], None] | None


@dataclass
class _FolderEntry:
    path_real: str
    path_temp: str


@dataclass
class OutputSet:
    """The outputs of one run, written to temporary files until committed.

    Parameters
    ----------
    step_mode : bool
        `True` when the tool runs as a step of Build Pipe Network
        (``RUN_AS_STEP``). Then `add_file` returns the path unchanged and
        `commit`/`discard` do nothing -- the parent owns the swap, caching
        and layer loading.

    """

    step_mode: bool = False
    _files: list[_FileEntry] = field(default_factory=list)
    _folders: list[_FolderEntry] = field(default_factory=list)

    def add_file(
        self,
        path_real: str,
        *,
        layer_name: str | None = None,
        cache_roles: Sequence[str] = (),
        style_default: Callable[[QgsVectorLayer], None] | None = None,
    ) -> str:
        """Register an output file and get the path to write it to.

        Parameters
        ----------
        path_real : str
            Where the file must end up.
        layer_name : str or None
            Load the committed file as a map layer with this name. `None`
            for files that aren't map layers (``.dat``, ``.png``, ...).
        cache_roles : sequence of str
            `utils.set_cached_path` roles to point at `path_real` on commit.
        style_default : callable or None
            Styling for a newly loaded layer, used only when no old layer
            on `path_real` was loaded (an old layer's style is copied).

        Returns
        -------
        str
            The path to write to: a fresh temporary path next to
            `path_real`, or `path_real` itself in step mode.

        """
        if self.step_mode:
            return path_real
        path_temp = self._temp_path_for(path_real)
        if os.path.isfile(path_temp):
            os.remove(path_temp)  # crash leftover; the GeoJSON driver won't overwrite
        self._files.append(
            _FileEntry(path_real, path_temp, layer_name, tuple(cache_roles), style_default)
        )
        return path_temp

    def add_folder(self, path_real: str) -> str:
        """Register an output folder and get the folder to write into.

        Every file written into the returned folder is moved into
        `path_real` on commit. Files aren't loaded as layers.

        Parameters
        ----------
        path_real : str
            Where the files must end up.

        Returns
        -------
        str
            A fresh, empty temporary folder, or `path_real` in step mode.

        """
        if self.step_mode:
            return path_real
        path_temp = self._temp_path_for(path_real)
        if os.path.isdir(path_temp):
            shutil.rmtree(path_temp)
        os.makedirs(path_temp)
        self._folders.append(_FolderEntry(path_real, path_temp))
        return path_temp

    def drop(self, path_real: str) -> None:
        """Forget one registered output and delete its temp file.

        For optional outputs whose failure isn't fatal to the run (e.g. Get
        Buildings' roads), so the rest can still be committed. Best-effort:
        never raises.

        Parameters
        ----------
        path_real : str
            The real path the output was registered under.

        """
        if self.step_mode:
            return
        target = normalize_path(path_real)
        for entry in [e for e in self._files if normalize_path(e.path_real) == target]:
            self._files.remove(entry)
            if os.path.isfile(entry.path_temp):
                try:
                    os.remove(entry.path_temp)
                except OSError:
                    pass
            # The dropped entry is no longer listed, so the later cleanup
            # wouldn't visit its temp folder -- remove it here if now empty
            # (e.g. roads saved to a different folder than the buildings).
            temp_folder = os.path.dirname(entry.path_temp)
            if os.path.isdir(temp_folder) and not os.listdir(temp_folder):
                try:
                    os.rmdir(temp_folder)
                except OSError:
                    pass

    def discard(self) -> None:
        """Delete all temporary files -- call when the run failed or was cancelled."""
        if self.step_mode:
            return
        # Best-effort: this runs while handling another failure, so it must
        # never raise itself (e.g. a failed writer still holding its file).
        for entry in self._files:
            if os.path.isfile(entry.path_temp):
                try:
                    os.remove(entry.path_temp)
                except OSError:
                    pass
        for entry in self._folders:
            shutil.rmtree(entry.path_temp, ignore_errors=True)
        self._remove_empty_temp_folders()

    def commit(self, feedback=None) -> None:
        """Swap every output into place, point loaded layers at it, cache paths.

        GUI thread only (``postProcessAlgorithm``, or a dialog). Files are
        swapped one at a time; if one can't be (e.g. it's open in another
        program), the rest are left as they were and an `OutputCommitError`
        says which files were and weren't updated. Every output is
        regenerated by re-running, so a partial swap loses nothing.

        Parameters
        ----------
        feedback : QgsProcessingFeedback or None
            For progress messages; `None` from a dialog.

        Raises
        ------
        OutputCommitError
            If a file couldn't be swapped or a layer couldn't be reloaded.

        """
        if self.step_mode:
            return
        # A background drawing job keeps the files it's drawing open; cancel
        # it before removing layers so it lets go of them.
        canvas = _map_canvas()
        if canvas is not None:
            canvas.stopRendering()
        try:
            self._commit_all(feedback)
        finally:
            if canvas is not None:
                canvas.refresh()

    def _commit_all(self, feedback) -> None:
        updated: list[str] = []
        try:
            for entry in self._files:
                self._commit_file(entry, updated, feedback)
            for entry in self._folders:
                os.makedirs(entry.path_real, exist_ok=True)
                for name in sorted(os.listdir(entry.path_temp)):
                    target = os.path.join(entry.path_real, name)
                    os.replace(os.path.join(entry.path_temp, name), target)
                    updated.append(target)
        except (OSError, OutputCommitError) as exc:
            not_updated = [e.path_real for e in self._files if e.path_real not in updated]
            # A folder's own path is never in `updated` (only the files moved
            # into it are), so look at what's still waiting in its temp folder
            # -- before discard() deletes it.
            for e in self._folders:
                waiting = sorted(os.listdir(e.path_temp)) if os.path.isdir(e.path_temp) else []
                not_updated += [os.path.join(e.path_real, name) for name in waiting]
            self.discard()
            lines = [f"Could not update the output files: {exc}"]
            if updated:
                lines.append("Updated: " + ", ".join(updated))
            lines.append("Not updated (left as they were): " + ", ".join(not_updated))
            lines.append("Close any program that has these files open and run again.")
            raise OutputCommitError("\n".join(lines)) from exc

        self.discard()  # leftovers only: removes the now-empty temp folders
        if feedback is not None:
            for path in updated:
                feedback.pushInfo(f"Updated {path}")

    def _commit_file(self, entry: _FileEntry, updated: list[str], feedback) -> None:
        loaded = self._detach_loaded_layers(entry.path_real)
        if feedback is not None:
            feedback.pushInfo(
                f"{os.path.basename(entry.path_real)}: replacing {len(loaded)} loaded layer(s)"
            )
        try:
            _replace_or_flush_and_retry(entry.path_temp, entry.path_real)
        except OSError:
            # File unchanged -- point every layer back at it, as it was.
            self._reattach_layers(loaded)
            raise
        updated.append(entry.path_real)
        for role in entry.cache_roles:
            utils.set_cached_path(role, entry.path_real)
        if loaded:
            # The user's own layers (duplicates included) stay the same layer
            # objects -- same ID, name, style, place, visibility, joins -- and
            # just show the new file.
            self._reattach_layers(loaded)
        elif entry.layer_name is not None:
            self._load_new_layer(entry.path_real, entry.layer_name, entry.style_default)

    @staticmethod
    def _detach_loaded_layers(path_real: str) -> list[tuple[QgsVectorLayer, str, str]]:
        """Point every loaded layer on `path_real` at an empty placeholder; return them.

        Each layer on the file holds it open on Windows, so all must let go
        before the swap -- but instead of removing them, each layer object is
        kept and only its data source is switched (to an in-memory
        placeholder with the same geometry type and fields, so its style and
        field settings don't notice). That keeps its layer ID, and with it
        everything that refers to it: joins, relations, map themes, print
        layouts. [Verified 2026-10-01 -- headless experiment: lock released
        (incl. after a pooled read), ID/style/visibility/position/join kept]

        Returns
        -------
        list of (QgsVectorLayer, str, str)
            Each detached layer with its own original source and provider,
            for `_reattach_layers`.

        """
        target = normalize_path(path_real)
        loaded = [
            (layer, layer.source(), layer.providerType())
            for layer in QgsProject.instance().mapLayers().values()
            if isinstance(layer, QgsVectorLayer) and normalize_path(layer.source()) == target
        ]
        for layer, _source, _provider in loaded:
            placeholder = QgsMemoryProviderUtils.createMemoryLayer(
                "placeholder", layer.fields(), layer.wkbType(), layer.crs()
            )
            layer.setDataSource(
                placeholder.source(), layer.name(), "memory", QgsDataProvider.ProviderOptions()
            )
        return loaded

    @staticmethod
    def _reattach_layers(loaded: list[tuple[QgsVectorLayer, str, str]]) -> None:
        """Point each layer back at its own original source (keeps filters/options in it)."""
        failed = []
        for layer, source, provider in loaded:
            layer.setDataSource(source, layer.name(), provider, QgsDataProvider.ProviderOptions())
            if not layer.isValid():
                failed.append(layer.name())
            layer.triggerRepaint()
        if failed:
            raise OutputCommitError(f"Could not reload layer(s): {', '.join(failed)}.")

    @staticmethod
    def _load_new_layer(path, name, style_default) -> None:
        """Load an output that wasn't on the map yet, with the tool's default style."""
        layer = QgsVectorLayer(path, name, "ogr")
        if not layer.isValid():
            raise OutputCommitError(f"Could not load {path} as a layer.")
        QgsProject.instance().addMapLayer(layer)
        if style_default is not None:
            style_default(layer)
        layer.triggerRepaint()

    @staticmethod
    def _temp_path_for(path_real: str) -> str:
        folder, name = os.path.split(os.path.abspath(path_real))
        temp_folder = os.path.join(folder, TEMP_FOLDER_NAME)
        os.makedirs(temp_folder, exist_ok=True)
        return os.path.join(temp_folder, name)

    def _remove_empty_temp_folders(self) -> None:
        for entry in [*self._files, *self._folders]:
            temp_folder = os.path.dirname(entry.path_temp)
            if os.path.isdir(temp_folder) and not os.listdir(temp_folder):
                try:
                    os.rmdir(temp_folder)
                except OSError:
                    pass
