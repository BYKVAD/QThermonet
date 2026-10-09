"""Build Pipe Network: runs Main Pipe Hierarchy, Service Pipes and Pipe Topology in one go."""

from __future__ import annotations

import os

from qgis import processing
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingMultiStepFeedback,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterVectorLayer,
    QgsVectorLayer,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMessageBox

from .. import output_handling, utils
from .main_pipe_hierarchy_algorithm import check_pipes_layer, style_main_pipes
from .service_pipes_algorithm import style_service_pipes


class BuildPipeNetworkAlgorithm(QgsProcessingAlgorithm):
    """Run the three pipe network steps as one tool.

    Each step runs as a child algorithm with ``RUN_AS_STEP=True``, writing
    to this tool's own temporary files. Every step that completes is
    committed at the end; a failed step and the steps after it change
    nothing, and a cancel changes nothing at all.

    """

    PIPES_LAYER = "PIPES_LAYER"
    SOURCE_LAYER = "SOURCE_LAYER"
    BUILDINGS_LAYER = "BUILDINGS_LAYER"
    CROP_NETWORK = "CROP_NETWORK"
    CROP_MAINS_FILE = "CROP_MAINS_FILE"
    MAINS_OUTPUT = "MAINS_OUTPUT"
    SERVICE_PIPES_OUTPUT = "SERVICE_PIPES_OUTPUT"
    TOPOLOGY_OUTPUT = "TOPOLOGY_OUTPUT"
    TOPOLOGY_DAT_OUTPUT = "TOPOLOGY_DAT_OUTPUT"

    #: The four outputs, in the order the steps produce them.
    OUTPUTS = (MAINS_OUTPUT, SERVICE_PIPES_OUTPUT, TOPOLOGY_OUTPUT, TOPOLOGY_DAT_OUTPUT)

    #: Per step, in run order: display name, child algorithm ID, and the
    #: outputs it writes.
    STEPS = (
        ("Main Pipe Hierarchy", "QThermonet:main_pipe_hierarchy", (MAINS_OUTPUT,)),
        ("Shortest Service Pipes", "QThermonet:service_pipes", (SERVICE_PIPES_OUTPUT,)),
        ("Pipe Topology", "QThermonet:pipe_topology", (TOPOLOGY_OUTPUT, TOPOLOGY_DAT_OUTPUT)),
    )

    def initAlgorithm(self, config=None):
        """Define the three inputs and four outputs (same defaults as the individual tools)."""
        param = QgsProcessingParameterVectorLayer(
            self.PIPES_LAYER,
            "Select the main pipes layer",
            [QgsProcessing.TypeVectorLine],
            defaultValue=utils.get_cached_path("roads_file"),
        )
        param.setHelp("The main pipes of the thermonet, e.g. the roads layer from Get Buildings.")
        self.addParameter(param)

        param = QgsProcessingParameterVectorLayer(
            self.SOURCE_LAYER,
            "Select the source placement layer",
            [QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorLine],
            defaultValue=utils.get_cached_path("source_file"),
        )
        param.setHelp(
            "The output of the 'Source Placement' tool: a BHE borefield point layer "
            "or an HHE trench line layer, with an 'is_connection_node' attribute."
        )
        self.addParameter(param)

        param = QgsProcessingParameterVectorLayer(
            self.BUILDINGS_LAYER,
            "Select the buildings layer",
            [QgsProcessing.TypeVectorPolygon],
            defaultValue=utils.get_cached_path("load_geojson_file") or utils.get_cached_path("buildings_file"),
        )
        param.setHelp(
            "Building footprints with the fields 'Thermonet' (buildings set to 'Yes' "
            "are connected) and 'id_lokalId' (the heat pump IDs) -- e.g. the Get "
            "Buildings or Heat Loads output. The IDs must match the heat pump IDs "
            "in the heat loads file used for Full Dimensioning."
        )
        self.addParameter(param)

        param = QgsProcessingParameterBoolean(
            self.CROP_NETWORK, "Remove main pipes without heat pumps", defaultValue=True
        )
        param.setHelp(
            "Removes main pipes with no heat pumps on them or downstream, and trims "
            "the end of a pipe past its last connection -- such pipes carry no flow. "
            "Unticked, a main pipe without heat pumps stops the Pipe Topology step."
        )
        self.addParameter(param)

        param = QgsProcessingParameterBoolean(
            self.CROP_MAINS_FILE, "Also crop the main pipe hierarchy file", defaultValue=False
        )
        param.setHelp(
            "Also writes the cropped main pipes to the main pipe hierarchy output. "
            "Unticked, only the topology outputs are cropped, so comparing the two "
            "layers shows what was removed."
        )
        self.addParameter(param)

        save_directory = utils.default_save_directory()
        for name, description, file_filter, file_name in (
            (self.MAINS_OUTPUT, "Output main pipe hierarchy", "GeoJSON (*.geojson)", "main_pipe_hierarchy.geojson"),
            (self.SERVICE_PIPES_OUTPUT, "Output service pipes", "GeoJSON (*.geojson)", "service_pipes.geojson"),
            (self.TOPOLOGY_OUTPUT, "Output pipe topology", "GeoJSON (*.geojson)", "pipe_topology.geojson"),
            (self.TOPOLOGY_DAT_OUTPUT, "Output pipe topology DAT file", "DAT files (*.dat)", "pipe_topology.dat"),
        ):
            self.addParameter(
                QgsProcessingParameterFileDestination(
                    name,
                    description,
                    fileFilter=file_filter,
                    defaultValue=os.path.join(save_directory, file_name),
                )
            )

    def checkParameterValues(self, parameters, context):
        """Reject unsuitable inputs and duplicate output paths before anything runs.

        Rejects a Source Placement output as pipes layer, a buildings layer
        without heat pump IDs (``id_lokalId``), and two outputs on one file.

        Parameters
        ----------
        parameters : dict
            The run's parameter values.
        context : QgsProcessingContext
            The run's context.

        Returns
        -------
        tuple[bool, str]
            ``(True, "")`` if the run can start, else ``(False, message)``.

        """
        ok, message = super().checkParameterValues(parameters, context)
        if not ok:
            return ok, message
        ok, message = check_pipes_layer(self.parameterAsVectorLayer(parameters, self.PIPES_LAYER, context))
        if not ok:
            return ok, message
        # Pipe Topology (step 3) needs the heat pump IDs; catch a missing
        # field here instead of after steps 1 and 2 have run.
        buildings_layer = self.parameterAsVectorLayer(parameters, self.BUILDINGS_LAYER, context)
        if buildings_layer is not None and "id_lokalId" not in buildings_layer.fields().names():
            return False, (
                f"The selected buildings layer ('{buildings_layer.name()}') has no "
                "'id_lokalId' field. It holds the heat pump IDs that link the pipe "
                "network to the heat loads -- select a layer that has it, e.g. the "
                "Get Buildings or Heat Loads output."
            )
        # Two outputs on one file would overwrite each other in the swap.
        paths = self._output_paths(parameters, context)
        normalized = [output_handling.normalize_path(path) for path in paths]
        duplicates = [
            path for i, path in enumerate(paths)
            if normalized.count(normalized[i]) > 1 and normalized.index(normalized[i]) == i
        ]
        if duplicates:
            return False, (
                "Each output needs its own file, but these are used more than once: "
                + ", ".join(duplicates)
            )
        return True, ""

    def prepareAlgorithm(self, parameters, context, feedback):
        """Run the project-setup check, then one overwrite pop-up for all four outputs.

        Parameters
        ----------
        parameters : dict
            The run's parameter values.
        context : QgsProcessingContext
            The run's context.
        feedback : QgsProcessingFeedback
            For the cancel message.

        Returns
        -------
        bool
            `False` to stop the run before anything is written.

        """
        if not utils.prepare_algorithm_project_setup(self, parameters, context, feedback, self.PIPES_LAYER):
            return False
        if not output_handling.confirm_overwrite(self._output_paths(parameters, context)):
            feedback.reportError("Cancelled -- no files were changed.")
            return False
        return True

    def processAlgorithm(self, parameters, context, feedback):
        """Run the three steps into temporary files.

        A failure in step 1 fails the run (nothing has completed). A failure
        in step 2 or 3 drops that step's outputs and the later ones, and the
        run returns normally so `postProcessAlgorithm` commits what did
        complete. A cancel discards everything.

        Parameters
        ----------
        parameters : dict
            The run's parameter values.
        context : QgsProcessingContext
            The run's context, shared with the child steps.
        feedback : QgsProcessingFeedback
            For progress, log messages and cancelling.

        Returns
        -------
        dict
            The four real output paths.

        Raises
        ------
        QgsProcessingException
            If the run is cancelled or step 1 fails.

        """
        paths_real = dict(zip(self.OUTPUTS, self._output_paths(parameters, context)))
        self._outputs_real = paths_real
        self._failure: tuple[int, str, str] | None = None
        self._outputs = output_handling.OutputSet()
        paths_temp = {
            self.MAINS_OUTPUT: self._outputs.add_file(
                paths_real[self.MAINS_OUTPUT],
                layer_name="Main pipe hierarchy",
                cache_roles=["mains_file"],
                style_default=style_main_pipes,
            ),
            self.SERVICE_PIPES_OUTPUT: self._outputs.add_file(
                paths_real[self.SERVICE_PIPES_OUTPUT],
                layer_name="Service pipes",
                cache_roles=["service_pipes_file"],
                style_default=style_service_pipes,
            ),
            self.TOPOLOGY_OUTPUT: self._outputs.add_file(
                paths_real[self.TOPOLOGY_OUTPUT],
                layer_name="Pipe topology",
                cache_roles=["topology_geojson_file"],
            ),
            self.TOPOLOGY_DAT_OUTPUT: self._outputs.add_file(
                paths_real[self.TOPOLOGY_DAT_OUTPUT],
                cache_roles=["topology_dat_file"],
            ),
        }
        # Cropped main pipes go to a second temp file: step 1's is still open
        # as the layer handed to step 3 (see add_replacement).
        self._cropped_mains_temp = None
        if self.parameterAsBoolean(parameters, self.CROP_NETWORK, context) and self.parameterAsBoolean(
            parameters, self.CROP_MAINS_FILE, context
        ):
            self._cropped_mains_temp = self._outputs.add_replacement(paths_real[self.MAINS_OUTPUT])
        # Intermediate results are passed on as layer objects this tool owns,
        # not as paths: a child given a path loads it into the context's
        # layer store, which keeps the temp file locked until after commit
        # (headless experiment 2026-10-02). Released in `finally`.
        intermediates: dict[str, QgsVectorLayer] = {}
        step_feedback = QgsProcessingMultiStepFeedback(len(self.STEPS), feedback)
        try:
            for index, (step_name, algorithm_id, step_outputs) in enumerate(self.STEPS):
                step_feedback.setCurrentStep(index)
                step_feedback.pushInfo(f"\nStep {index + 1} of {len(self.STEPS)}: {step_name}")
                child_parameters = self._child_parameters(index, parameters, paths_temp, intermediates)
                try:
                    processing.run(
                        algorithm_id,
                        child_parameters,
                        context=context,
                        feedback=step_feedback,
                        is_child_algorithm=True,
                    )
                    if not feedback.isCanceled() and index < len(self.STEPS) - 1:
                        intermediates[step_outputs[0]] = self._open_intermediate(
                            paths_temp[step_outputs[0]], step_name
                        )
                except Exception as exc:
                    if feedback.isCanceled():
                        raise QgsProcessingException("Cancelled -- no files were changed.") from exc
                    message = f"Step {index + 1} of {len(self.STEPS)} ({step_name}) failed: {exc}"
                    if index == 0:
                        raise QgsProcessingException(message + "\nNo files were changed.") from exc
                    self._failure = (index, step_name, str(exc))
                    # Step 3 writes the cropped mains before it can still fail --
                    # keep step 1's uncropped result then.
                    if self._cropped_mains_temp and os.path.isfile(self._cropped_mains_temp):
                        try:
                            os.remove(self._cropped_mains_temp)
                        except OSError:
                            pass
                    for _, _, later_outputs in self.STEPS[index:]:
                        for name in later_outputs:
                            self._outputs.drop(paths_real[name])
                    feedback.reportError(message, fatalError=False)
                    break
                if feedback.isCanceled():
                    raise QgsProcessingException("Cancelled -- no files were changed.")
        except Exception:
            self._outputs.discard()
            raise
        finally:
            # Destroyed, not just dereferenced: after a failed step a stale
            # reference to them survives the child's exception (not a Python
            # cycle -- gc.collect() doesn't free it), keeping the temp file
            # open so commit can't move it (headless test 2026-10-02).
            for layer in intermediates.values():
                sip.delete(layer)
            intermediates.clear()
        return paths_real

    def postProcessAlgorithm(self, context, feedback):
        """Commit the completed steps' outputs, then report a failed step loudly.

        QGIS only logs an exception raised here and still reports the run as
        completed (``QgsProcessingAlgorithm::postProcess``), so a failure is
        also shown in a message box -- safe, this runs on the GUI thread.

        Parameters
        ----------
        context : QgsProcessingContext
            The run's context.
        feedback : QgsProcessingFeedback
            For progress and log messages.

        Returns
        -------
        dict
            Always empty.

        Raises
        ------
        QgsProcessingException
            If a step failed or the outputs couldn't be swapped into place.

        """
        try:
            self._outputs.commit(feedback)
        except output_handling.OutputCommitError as exc:
            self._show_warning(str(exc))
            raise
        if self._failure is not None:
            message = self._failure_summary()
            self._show_warning(message)
            raise QgsProcessingException(message)
        return {}

    def _failure_summary(self) -> str:
        """What failed, what was and wasn't updated, and what to do next."""
        index, step_name, error = self._failure
        updated, not_updated = [], []
        for step_index, (_, _, step_outputs) in enumerate(self.STEPS):
            target = updated if step_index < index else not_updated
            target.extend(self._outputs_real[name] for name in step_outputs)
        return "\n\n".join([
            f"Step {index + 1} of {len(self.STEPS)} ({step_name}) failed:\n{error}",
            "Updated:\n" + "\n".join(updated),
            "Not updated (left as they were):\n" + "\n".join(not_updated),
            # Generic on purpose: the step's own error above gives the
            # specific fix (e.g. trim a stub pipe), and that fix is often in
            # an earlier step's input -- so never suggest one step alone.
            "Fix the problem and run Build Pipe Network again (it re-runs everything "
            "from Main Pipe Hierarchy). Full Dimensioning keeps using the old pipe "
            "topology until Pipe Topology runs successfully.",
        ])

    @staticmethod
    def _show_warning(message) -> None:
        from qgis.utils import iface

        if iface is None:  # headless: the exception's log message is enough
            return
        QMessageBox.warning(iface.mainWindow(), "Build Pipe Network", message)

    def _child_parameters(self, index, parameters, paths_temp, intermediates) -> dict:
        """The parameters for step `index`: the user's inputs, earlier steps' layers, temp outputs."""
        if index == 0:
            return {
                "PIPES_LAYER": parameters[self.PIPES_LAYER],
                "SOURCE_LAYER": parameters[self.SOURCE_LAYER],
                "OUTPUT": paths_temp[self.MAINS_OUTPUT],
                "RUN_AS_STEP": True,
            }
        if index == 1:
            return {
                "BUILDINGS_LAYER": parameters[self.BUILDINGS_LAYER],
                "PIPES_LAYER": intermediates[self.MAINS_OUTPUT],
                "OUTPUT_LAYER": paths_temp[self.SERVICE_PIPES_OUTPUT],
                "RUN_AS_STEP": True,
            }
        return {
            "PIPES_LAYER": intermediates[self.MAINS_OUTPUT],
            "SERVICE_PIPES_LAYER": intermediates[self.SERVICE_PIPES_OUTPUT],
            "SOURCE_LAYER": parameters[self.SOURCE_LAYER],
            "CROP_NETWORK": parameters.get(self.CROP_NETWORK, True),
            "CROP_MAINS_FILE": self._cropped_mains_temp is not None,
            "CROPPED_MAINS_OUTPUT": self._cropped_mains_temp,
            "OUTPUT": paths_temp[self.TOPOLOGY_OUTPUT],
            "DAT_OUTPUT": paths_temp[self.TOPOLOGY_DAT_OUTPUT],
            "RUN_AS_STEP": True,
        }

    @staticmethod
    def _open_intermediate(path, step_name) -> QgsVectorLayer:
        layer = QgsVectorLayer(path, step_name, "ogr")
        if not layer.isValid():
            raise QgsProcessingException(f"Could not read its output file {path}.")
        return layer

    def _output_paths(self, parameters, context) -> list[str]:
        return [self.parameterAsFileOutput(parameters, name, context) for name in self.OUTPUTS]

    def name(self):
        """Return the algorithm ID."""
        return "build_pipe_network"

    def displayName(self):
        """Return the name shown in the toolbox and menus."""
        return self.tr("Build Pipe Network")

    def group(self):
        """Return the translated group name."""
        return self.tr(self.groupId())

    def groupId(self):
        """Return the group ID."""
        return "2. Thermonet"

    def tr(self, string):
        """Translate `string` in the Processing context."""
        return QCoreApplication.translate("Processing", string)

    def icon(self):
        """Return the tool icon."""
        return QIcon(utils.get_logo("logo6-pipes-simple.png"))

    def shortHelpString(self):
        """Return the help text shown in the dialog."""
        return (
            "<p>Builds the pipe network in one run: <b>Main Pipe Hierarchy</b> → "
            "<b>Shortest Service Pipes</b> → <b>Pipe Topology</b>.</p>"
            "<p>Every step that completes replaces its output files; if a step fails, "
            "that step and the ones after it leave their files as they were, and a "
            "message says what was updated. Fix the problem, then run this tool again "
            "-- or, if your fix only changed the failed step's own inputs, run that "
            "step on its own. Cancelling changes no files.</p>"
        )

    def createInstance(self):
        """Return a new instance of this algorithm."""
        return BuildPipeNetworkAlgorithm()
