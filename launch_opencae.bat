@echo off
rem ===========================================================================
rem  OpenCAE Studio launcher (Windows)
rem
rem    launch_opencae.bat [project.ocae]   start the application
rem    launch_opencae.bat --doctor         check Python, libraries and CalculiX
rem    launch_opencae.bat --acceptance     run the GUI acceptance test (cantilever)
rem
rem  Uses your EXISTING conda environment (default: qt_test). Installs nothing
rem  and changes no global/system environment variables: all settings below
rem  are local to this console window.
rem  CalculiX is found automatically (Tools > Settings path, OPENCAE_CCX, or any
rem  conda env such as 'opencae') and always runs as a separate child process
rem  with its own DLL search path, so its DLLs never mix with Qt's.
rem
rem  Optional overrides:  OPENCAE_CONDA_ROOT (e.g. C:\Users\User\miniforge3)
rem                      OPENCAE_GUI_ENV    (e.g. qt_test)
rem ===========================================================================
setlocal EnableExtensions
set "RC=1"
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
if not defined OPENCAE_GUI_ENV set "OPENCAE_GUI_ENV=qt_test"

rem ---- locate conda ----------------------------------------------------------
if defined OPENCAE_CONDA_ROOT goto :have_root
if defined CONDA_EXE for %%I in ("%CONDA_EXE%\..\..") do set "OPENCAE_CONDA_ROOT=%%~fI"
if defined OPENCAE_CONDA_ROOT if exist "%OPENCAE_CONDA_ROOT%\condabin\conda.bat" goto :have_root
for %%D in (miniforge3 mambaforge miniconda3 anaconda3) do (
    if exist "%USERPROFILE%\%%D\condabin\conda.bat" if not defined OPENCAE_CONDA_ROOT set "OPENCAE_CONDA_ROOT=%USERPROFILE%\%%D"
)
:have_root
if not exist "%OPENCAE_CONDA_ROOT%\condabin\conda.bat" goto :no_conda
set "ENV_DIR=%OPENCAE_CONDA_ROOT%\envs\%OPENCAE_GUI_ENV%"
if not exist "%ENV_DIR%\python.exe" goto :no_env

rem ---- activate the GUI environment (this console only) -----------------------
call "%OPENCAE_CONDA_ROOT%\condabin\conda.bat" activate "%OPENCAE_GUI_ENV%"
if errorlevel 1 goto :activate_failed
cd /d "%PROJECT_DIR%"
set "PYTHONPATH=%PROJECT_DIR%"

rem ---- validate the interpreter and libraries ----------------------------------
python -c "import sys; assert sys.version_info >= (3, 11), sys.version; print('[OpenCAE] Python', sys.version.split()[0], sys.executable)"
if errorlevel 1 goto :bad_python
python -c "import PySide6.QtCore, vtkmodules.vtkCommonCore, OCP.TopoDS, gmsh, pyvistaqt" 2>nul
if errorlevel 1 goto :bad_libs

if /I "%~1"=="--doctor" goto :doctor
if /I "%~1"=="--acceptance" goto :acceptance

rem ---- CalculiX quick check (the application runs a full self-test itself) -----
python -c "from opencae.solver.launcher import find_install as f; from opencae.utilities.settings import get_setting as g; i=f(g('ccx_path'), g('ccx_extra_dll_dirs') or []); print('[OpenCAE] CalculiX:', i.label if i else 'NOT FOUND - solving disabled, see Tools > Settings')"

echo [OpenCAE] Starting OpenCAE Studio ...
python -m opencae.app.main %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :end
echo [OpenCAE] The application exited with code %RC%.
echo           Log file: %USERPROFILE%\.opencae\logs\opencae.log
echo           Run "launch_opencae.bat --doctor" for a full diagnosis.
pause
goto :end

:doctor
python -m opencae.app.doctor
set "RC=%ERRORLEVEL%"
pause
goto :end

:acceptance
echo [OpenCAE] GUI acceptance test - results in "%PROJECT_DIR%\acceptance_output"
python -m opencae.app.main --selftest "%PROJECT_DIR%\acceptance_output"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" echo [OpenCAE] ACCEPTANCE TEST PASSED
if not "%RC%"=="0" echo [OpenCAE] ACCEPTANCE TEST FAILED - see acceptance_output\selftest.json
pause
goto :end

:no_conda
echo [OpenCAE] ERROR: no conda/miniforge installation found.
echo           Set OPENCAE_CONDA_ROOT to your miniforge3 folder, e.g.
echo           set OPENCAE_CONDA_ROOT=%USERPROFILE%\miniforge3
goto :fail
:no_env
echo [OpenCAE] ERROR: conda environment "%OPENCAE_GUI_ENV%" not found at "%ENV_DIR%".
echo           Set OPENCAE_GUI_ENV to the environment that has PySide6 installed.
goto :fail
:activate_failed
echo [OpenCAE] ERROR: "conda activate %OPENCAE_GUI_ENV%" failed.
goto :fail
:bad_python
echo [OpenCAE] ERROR: Python 3.11 or newer is required in "%OPENCAE_GUI_ENV%".
goto :fail
:bad_libs
echo [OpenCAE] ERROR: required libraries cannot be imported in "%OPENCAE_GUI_ENV%". Details:
python -m opencae.app.doctor --no-solver
goto :fail

:fail
echo.
echo [OpenCAE] Startup aborted. Nothing was installed or changed.
pause
set "RC=1"

:end
endlocal & exit /b %RC%
