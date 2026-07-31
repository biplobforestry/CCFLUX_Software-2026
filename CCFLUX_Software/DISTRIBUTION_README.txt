CC-FLUX 2026 ZEPPELIN SOFTWARE
================================

Integrated post-flight scientific payload review for the CC-FLUX Zeppelin
campaign 2026.

This directory is the complete, self-contained distribution. Keep it intact:
the launchers, application code, configuration and bundled scientific routines
depend on one another, and copying the launcher alone will not work.


SYSTEM REQUIREMENTS
-------------------
- Windows 10 or 11, or a currently supported release of macOS
- Python 3.10 or newer
- An internet connection for the first launch, while the required libraries
  are installed
- Approximately 2 GB of free disk space for the Python environment
- Sufficient additional space for processed output, which is written to a
  directory you select


INSTALLATION AND LAUNCH
-----------------------
Windows
  1. Double-click Windows_CCFLUX.bat.
  2. The launcher creates a private environment in .venv-windows.
  3. Required libraries are installed and verified.
  4. CC-FLUX starts on a free local port and opens your default browser.

macOS
  1. Double-click Mac_CCFLUX.command.
  2. The launcher creates a private environment in .venv-macos.
  3. Required libraries are installed and verified.
  4. CC-FLUX starts on a free local port and opens your default browser.

If Python 3.10 or newer is not present, the launcher reports the version it
found and offers to install a supported release. Nothing is installed without
your confirmation. You may also install Python yourself:

  Windows  https://www.python.org/downloads/windows/
           Select "Add Python to PATH" during installation.
  macOS    https://www.python.org/downloads/

If macOS blocks the first launch, right-click Mac_CCFLUX.command, choose Open,
and confirm.


OPERATION
---------
- Keep the launcher window open while CC-FLUX is running. Closing it stops the
  local server.
- Select the Flight Folder, Camera Folder and Output Folder in the interface.
- Raw campaign data is read only. All generated products are written to the
  Output Folder you select, which must be separate from the raw data.
- Press Control-C in the launcher window, or use Exit in the interface, to stop
  the server.
- Startup and installation diagnostics are recorded in logs/launcher.log.
  Processing diagnostics are available in the interface and are saved with each
  Flight Project.


SOFTWARE UPDATES
----------------
The interface reports whether a newer release has been published. Nothing is
downloaded or installed automatically. Complete any processing in progress and
save your Flight Project before updating.

The update check contacts GitHub once per launch. Section 7 of manual.text
describes what this discloses and how to disable it.


DOCUMENTATION
-------------
  manual.text        Operating manual: workflow, instruments, time handling,
                     project files, updates and troubleshooting
  ARCHITECTURE.md    System architecture and known limitations
  License.txt        Licence and conditions of use


SCIENTIFIC RESPONSIBILITY
-------------------------
Review the warnings, calibration inputs, time alignment and quality-control
information presented for each instrument before interpreting or publishing any
result. This software supports reproducible processing; it does not replace
validation by the responsible instrument investigator.


© 2026 Biplob Dey · Forschungszentrum Jülich GmbH
