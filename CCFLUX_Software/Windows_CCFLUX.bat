@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title CC-FLUX 2026 Zeppelin Software

if not exist "logs" mkdir "logs"
set "LAUNCH_LOG=%CD%\logs\launcher.log"

echo.
echo ================================================================
echo CC-FLUX 2026 Zeppelin Software
echo © 2026 Biplob Dey - Forschungszentrum Jülich GmbH
echo Windows automatic setup and launch
echo ================================================================
echo.
>>"%LAUNCH_LOG%" echo [!date! !time!] Windows_CCFLUX.bat started.

set "PYTHON_COMMAND="
where py >nul 2>&1
if not errorlevel 1 set "PYTHON_COMMAND=py -3"
if not defined PYTHON_COMMAND (
  where python >nul 2>&1
  if not errorlevel 1 set "PYTHON_COMMAND=python"
)
if not defined PYTHON_COMMAND goto :python_missing

%PYTHON_COMMAND% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :python_version_failed

if not exist ".venv-windows\Scripts\python.exe" (
  echo Creating the private CC-FLUX Python environment...
  >>"%LAUNCH_LOG%" echo [!date! !time!] Creating .venv-windows.
  %PYTHON_COMMAND% -m venv ".venv-windows"
  if errorlevel 1 goto :setup_failed
)

set "DASHBOARD_PYTHON=%CD%\.venv-windows\Scripts\python.exe"
"%DASHBOARD_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :python_version_failed

echo Installing and checking all required CC-FLUX libraries...
>>"%LAUNCH_LOG%" echo [!date! !time!] Dependency installation and validation started.
"%DASHBOARD_PYTHON%" -m pip install --disable-pip-version-check --upgrade-strategy only-if-needed -e ".[noseboom,miro,partector,ins-gimbal,sif,micasense,flir,gopro]"
if errorlevel 1 goto :setup_failed

"%DASHBOARD_PYTHON%" -c "import numpy, pandas, PIL, yaml, scipy, matplotlib, tables, flask, plotly, werkzeug" >nul 2>&1
if errorlevel 1 goto :verification_failed
>>"%LAUNCH_LOG%" echo [!date! !time!] All required libraries are ready.

set "PYTHONUNBUFFERED=1"
set "OMP_NUM_THREADS=1"
set "OPENBLAS_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "NUMEXPR_NUM_THREADS=1"

echo.
echo Starting CC-FLUX and opening the default browser...
echo Keep this command window open while using the software.
echo Press Control-C here to stop CC-FLUX.
echo.
>>"%LAUNCH_LOG%" echo [!date! !time!] Starting dashboard on an automatically selected free port.

"%DASHBOARD_PYTHON%" -m app.main --port 0
set "DASHBOARD_EXIT=%ERRORLEVEL%"
if not "%DASHBOARD_EXIT%"=="0" goto :runtime_failed

echo.
echo CC-FLUX has been closed.
>>"%LAUNCH_LOG%" echo [!date! !time!] CC-FLUX closed normally.
pause
endlocal
exit /b 0

:python_missing
echo.
echo ERROR: Python 3 was not found.
echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
echo During installation, select "Add Python to PATH", then run this file again.
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: Python 3 was not found.
pause
endlocal
exit /b 1

:python_version_failed
echo.
echo ERROR: Python 3.10 or newer is required.
echo Install a newer Python, remove .venv-windows, and run this file again.
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: Python 3.10 or newer is required.
pause
endlocal
exit /b 1

:setup_failed
echo.
echo ERROR: Required libraries could not be installed.
echo Check the internet connection and available disk space.
echo Launcher log: %LAUNCH_LOG%
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: Dependency installation failed.
pause
endlocal
exit /b 1

:verification_failed
echo.
echo ERROR: Library verification failed after installation.
echo Launcher log: %LAUNCH_LOG%
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: Library verification failed.
pause
endlocal
exit /b 1

:runtime_failed
echo.
echo ERROR: CC-FLUX stopped with exit code %DASHBOARD_EXIT%.
echo Launcher log: %LAUNCH_LOG%
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: CC-FLUX stopped with exit code %DASHBOARD_EXIT%.
pause
endlocal
exit /b %DASHBOARD_EXIT%
