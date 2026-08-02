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

set "REQUIRED_PYTHON=3.10"
set "OFFERED_PYTHON=3.12.7"

rem An unsupported or missing Python is offered an installer rather than simply
rem refused; a colleague on a campaign laptop should not have to diagnose this
rem themselves. The offer is written inline because a failure inside a CALLed
rem subroutine would return here instead of stopping the launcher.
call :detect_python
if defined PYTHON_COMMAND goto :python_ready

echo.
echo ----------------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on this computer.
) else (
  python -c "import sys; print('Python ' + '.'.join(map(str, sys.version_info[:3])) + ' was found.')" 2>nul
)
echo CC-FLUX requires Python %REQUIRED_PYTHON% or newer.
echo CC-FLUX can download the official Python %OFFERED_PYTHON% installer from
echo python.org and open it for you. Nothing is installed without your
echo confirmation, and any existing Python is left in place.
echo ----------------------------------------------------------------
set "INSTALL_PYTHON="
set /p "INSTALL_PYTHON=Download and open the Python installer now? [y/N] "
if /i not "!INSTALL_PYTHON:~0,1!"=="y" goto :python_missing

set "PYTHON_URL=https://www.python.org/ftp/python/%OFFERED_PYTHON%/python-%OFFERED_PYTHON%-amd64.exe"
set "PYTHON_SETUP=%TEMP%\ccflux-python-%OFFERED_PYTHON%-amd64.exe"
echo.
echo Downloading Python %OFFERED_PYTHON%...
>>"%LAUNCH_LOG%" echo [!date! !time!] Downloading Python from !PYTHON_URL!
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri $env:PYTHON_URL -OutFile $env:PYTHON_SETUP -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 (
  del /q "%PYTHON_SETUP%" >nul 2>&1
  goto :python_download_failed
)

echo Opening the installer. Select "Add python.exe to PATH", complete the
echo installation, then return to this window.
>>"%LAUNCH_LOG%" echo [!date! !time!] Opening the downloaded Python installer.
start /wait "" "%PYTHON_SETUP%"
del /q "%PYTHON_SETUP%" >nul 2>&1

call :detect_python
if not defined PYTHON_COMMAND goto :python_still_missing
echo Python is now available. Continuing.
>>"%LAUNCH_LOG%" echo [!date! !time!] Continuing after Python installation.

:python_ready

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

"%DASHBOARD_PYTHON%" -c "import numpy, pandas, PIL, yaml, scipy, matplotlib, tables, flask, plotly, werkzeug, openpyxl, cryptography" >nul 2>&1
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

:detect_python
rem Sets PYTHON_COMMAND only to an interpreter that is actually supported.
set "PYTHON_COMMAND="
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYTHON_COMMAND=py -3"
)
if not defined PYTHON_COMMAND (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_COMMAND=python"
  )
)
goto :eof

:python_download_failed
echo.
echo ERROR: The Python installer could not be downloaded.
echo Install Python %REQUIRED_PYTHON% or newer from
echo https://www.python.org/downloads/windows/ and run this file again.
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: The Python installer could not be downloaded.
pause
endlocal
exit /b 1

:python_still_missing
echo.
echo ERROR: Python %REQUIRED_PYTHON% or newer was still not found after installation.
echo Close this window, open a new one, and run this file again so that the
echo updated PATH takes effect.
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: Python still not found after installation.
pause
endlocal
exit /b 1

:python_missing
echo.
echo ERROR: Python %REQUIRED_PYTHON% or newer is required and was not installed.
echo Install it from https://www.python.org/downloads/windows/
echo During installation, select "Add Python to PATH", then run this file again.
>>"%LAUNCH_LOG%" echo [!date! !time!] ERROR: A supported Python was not available.
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
