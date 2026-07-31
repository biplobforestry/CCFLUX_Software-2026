@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title CC-FLUX 2026 Zeppelin Dashboard
set "PYTHONUNBUFFERED=1"
set "OMP_NUM_THREADS=1"
set "OPENBLAS_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "NUMEXPR_NUM_THREADS=1"

if not exist "logs" mkdir "logs"
set "LAUNCH_LOG=%CD%\logs\launcher.log"

echo.
echo ================================================================
echo CC-FLUX 2026 Zeppelin Dashboard
echo © 2026 Biplob Dey - Forschungszentrum Jülich GmbH
echo ================================================================
echo Windows launcher - automatic environment check and browser startup
echo.
>>"%LAUNCH_LOG%" echo [!date! !time!] Launcher started.

echo Locating Python 3...
set "PYTHON_COMMAND="
where py >nul 2>&1
if not errorlevel 1 set "PYTHON_COMMAND=py -3"
if not defined PYTHON_COMMAND (
  where python >nul 2>&1
  if not errorlevel 1 set "PYTHON_COMMAND=python"
)
if not defined PYTHON_COMMAND goto :python_missing

if not exist ".venv\Scripts\python.exe" (
  echo Creating the private CC-FLUX Python environment...
  >>"%LAUNCH_LOG%" echo [!date! !time!] Creating .venv.
  %PYTHON_COMMAND% -m venv ".venv"
  if errorlevel 1 goto :setup_failed
)

set "DASHBOARD_PYTHON=%CD%\.venv\Scripts\python.exe"
"%DASHBOARD_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :python_version_failed

echo Checking dashboard libraries...
"%DASHBOARD_PYTHON%" -c "import numpy, pandas, PIL, yaml, scipy, matplotlib, tables, flask, plotly, werkzeug" >nul 2>&1
if errorlevel 1 (
  echo One or more required libraries are missing. Installing them now...
  >>"%LAUNCH_LOG%" echo [!date! !time!] Missing dependencies detected; installation started.
  "%DASHBOARD_PYTHON%" -m pip install --disable-pip-version-check --upgrade pip
  if errorlevel 1 goto :setup_failed
  "%DASHBOARD_PYTHON%" -m pip install --disable-pip-version-check --upgrade-strategy only-if-needed -e ".[noseboom,miro,partector,ins-gimbal,sif,micasense,flir,gopro]"
  if errorlevel 1 goto :setup_failed
  >>"%LAUNCH_LOG%" echo [!date! !time!] Dependency installation completed.
) else (
  echo Required libraries are ready.
)

echo.
echo Starting the dashboard and opening the default browser...
echo Keep this command window open while using CC-FLUX.
echo If startup fails, the error will remain visible here.
echo CPU-intensive numerical libraries are limited to one thread per job
echo so the computer remains responsive during scanning and processing.
echo.
>>"%LAUNCH_LOG%" echo [!date! !time!] Starting dashboard server with an automatically selected free port.

"%DASHBOARD_PYTHON%" -m app.main --port 0
set "DASHBOARD_EXIT=%ERRORLEVEL%"
if not "%DASHBOARD_EXIT%"=="0" goto :runtime_failed

echo.
echo The CC-FLUX Dashboard has been closed.
>>"%LAUNCH_LOG%" echo [!date! !time!] Dashboard closed normally.
pause
endlocal
exit /b 0

:python_missing
echo.
echo ERROR: Python 3 was not found.
echo Install Python 3.10 or newer, then run this BAT file again.
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: Python 3 was not found.
pause
endlocal
exit /b 1

:python_version_failed
echo.
echo ERROR: Python 3.10 or newer is required.
echo Remove the .venv folder after installing a newer Python version, then run this BAT file again.
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: The private environment uses Python older than 3.10.
pause
endlocal
exit /b 1

:setup_failed
echo.
echo ERROR: The dashboard libraries could not be prepared.
echo Review the installation messages above and:
echo %LAUNCH_LOG%
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: Environment or dependency setup failed.
pause
endlocal
exit /b 1

:runtime_failed
echo.
echo ERROR: The dashboard stopped during startup with exit code %DASHBOARD_EXIT%.
echo Review the error above and:
echo %LAUNCH_LOG%
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: Dashboard startup failed with exit code %DASHBOARD_EXIT%.
pause
endlocal
exit /b %DASHBOARD_EXIT%
