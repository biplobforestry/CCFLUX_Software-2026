CC-FLUX 2026 ZEPPELIN SOFTWARE
================================

This folder is the portable colleague-distribution package.
Keep the complete folder together; do not copy only the launcher.

SYSTEM REQUIREMENTS
-------------------
- Windows 10/11 or a current macOS release
- Python 3.10 or newer
- Internet connection during the first launch
- Sufficient free disk space for Python environment

WINDOWS
-------
1. Double-click Windows_CCFLUX.bat.
2. The launcher creates .venv-windows.
3. It installs and verifies every required Python library.
4. CC-FLUX starts on a free local port and opens the default browser.

If Python is missing, install it from:
https://www.python.org/downloads/windows/
Select "Add Python to PATH" during Python installation.

macOS
-----
1. Double-click Mac_CCFLUX.command.
2. The launcher creates .venv-macos.
3. It installs and verifies every required Python library.
4. CC-FLUX starts on a free local port and opens the default browser.

If macOS blocks the first launch, right-click Mac_CCFLUX.command, choose Open,
and confirm. Python can be installed from:
https://www.python.org/downloads/

OPERATION
---------
- Keep the launcher Terminal/Command Prompt window open while using CC-FLUX.
- Use the GUI to select Flight, Camera, and Output folders.
- Raw input folders are read-only; generated products go to the selected
  Output Folder.
- Use Control-C in the launcher window to stop the local server.
- Launcher diagnostics are stored in logs/launcher.log.


The launchers install dependencies from pyproject.toml before every start,
using an only-if-needed strategy. Existing compatible libraries are reused.

© 2026 Biplob Dey - Forschungszentrum Jülich GmbH
