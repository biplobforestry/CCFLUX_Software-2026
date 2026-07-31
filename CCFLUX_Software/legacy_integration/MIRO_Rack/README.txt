MIRO Rack - Trace Gas Measurement
Zeppelin CCFLUX Campaign 2026

QUICK START
1. Double-click Run_MIRO_Rack.bat.
2. On the first run, a private .venv folder is created and the required
   Python libraries are installed automatically.
3. The dashboard opens in the default web browser.
4. Keep the command window open while the dashboard is running.

DATA INPUT
- MIRO: select a folder containing .txt files. Subfolders are scanned.
- Picarro: select a folder containing .dat files. Subfolders are scanned.
- Other file extensions are ignored.

PROJECT FILES
- Select an output directory before saving or exporting.
- Saved projects use the HDF format and contain loaded data, settings,
  analysis results, and diagnostic logs.

REQUIREMENTS
- Windows 10 or Windows 11
- Python 3.10 or newer
- Internet access is required only when libraries must be installed.

MANUAL START
Open Command Prompt in this folder and run:

    .venv\Scripts\python.exe MIRO_Rack_GUI.py

If setup fails, run Run_MIRO_Rack.bat again and read the displayed error.