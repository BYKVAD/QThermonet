# Headless tests

Small regression tests that run QThermonet code inside a headless QGIS — no
GUI, no project, no test-case files needed. They cover the fixes from the
output-handling work (see
`claude/handoffs/2026-09-25-output-handling-parts-overview.md`):

| Test | What it guards |
|---|---|
| `test_output_set.py` | Temp files + swap over a loaded layer (Windows file lock, OGR pool), name/style/position kept, locked file, discard, step mode, folder outputs |
| `test_write_geojson.py` | Written GeoJSONs keep their CRS (EPSG, compound, ESRI codes), CRS-less / custom CRSs refused, works on a background thread |
| `test_localize_borefield.py` | QThermonet's QGIS-based borefield localization gives the same result as pythermonet's pyproj-based one, and survives repeated background-thread runs |
| `test_source_placement_restore.py` | Source Placement reads node and rotation back from a previous export |
| `test_trench_refresh.py` | The HHE trench refresh rewrites the file (no doubled trenches), keeps style and CRS |
| `test_build_pipe_network.py` | Build Pipe Network on a generated network: full run, re-run keeps layers in place, a failing step 2/3 commits only the steps before it, a step 1 failure or cancel changes nothing, no temp files left |
| `test_network_cropping.py` | Network cropping rules: dead branches removed, pipe ends trimmed to the last connection, a removed branch doesn't protect a tail, unconnected pipes left as is |
| `test_project_settings_link.py` | The project remembers its settings file across a QGIS restart (simulated in a fresh process); first save keeps the session; "Save As", a new project and a deleted settings file start empty |

## Running

Use QGIS's own Python, so the QGIS libraries and pythermonet are found. From
the repo folder:

```bat
"C:\Program Files\QGIS 4.2.2\bin\python-qgis.bat" tests\headless\run_all.py
```

or a single test:

```bat
"C:\Program Files\QGIS 4.2.2\bin\python-qgis.bat" tests\headless\test_output_set.py
```

Each test prints `PASS`/`FAIL` lines and exits non-zero on any failure;
`run_all.py` runs each in its own process (so a crash in one is reported, not
fatal) and prints a summary.

Requirements: the repo folder must be named `QThermonet` (the plugin package
name), and pythermonet must be installed into QGIS's Python (as for using the
plugin). Adjust the QGIS path to your installed version.

## Limits

These run headless, so they can't cover anything that only happens in the
GUI: the map canvas, dialogs, or environment problems such as a `PROJ_LIB`
pointing to another PROJ installation (see the overview handoff). Those still
need a manual test run in QGIS.
