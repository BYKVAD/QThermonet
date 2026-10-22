@echo off
rem build_plugin_zip.bat
rem Builds the plugin zip for plugins.qgis.org from the last commit (HEAD).
rem The tutorial and development files are left out of the plugin zip only;
rem GitHub's own downloads still contain the whole repository.

setlocal
cd /d "%~dp0"

set "PLUGIN=qthermonet_processing_v_0_1"

set "VERSION="
for /f "tokens=2 delims==" %%V in ('findstr /b "version=" metadata.txt') do set "VERSION=%%V"
if not defined VERSION goto :no_version

git diff --quiet HEAD
if errorlevel 1 echo WARNING: uncommitted changes are NOT included - the zip is built from the last commit.

set "ZIP=%PLUGIN%-v%VERSION%.zip"
git archive --format=zip --prefix=%PLUGIN%/ -o "%ZIP%" HEAD -- . ^
    ":(exclude)Tutorial1" ^
    ":(exclude)help" ^
    ":(exclude)scripts" ^
    ":(exclude).gitattributes" ^
    ":(exclude).gitignore" ^
    ":(exclude)CONTRIBUTING.md" ^
    ":(exclude)Makefile" ^
    ":(exclude)pb_tool.cfg" ^
    ":(exclude)pylintrc" ^
    ":(exclude)build_plugin_zip.bat"
if errorlevel 1 goto :failed

echo Built %ZIP% (version %VERSION%)
goto :end

:no_version
echo ERROR: no "version=" line found in metadata.txt.
goto :end

:failed
echo ERROR: git archive failed - see the message above.

:end
echo.
pause
endlocal
