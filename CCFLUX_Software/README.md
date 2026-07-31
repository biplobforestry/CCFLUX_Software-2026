# CCFLUX Zeppelin

## Campaign folder selection

Each scan uses two independent, read-only roots:

1. the Zeppelin flight folder (for example `Flight_2707`); and
2. a user-selected Camera System folder.

The desktop folder workflow asks for both locations before discovery starts.
The camera location is never inferred from a hard-coded disk path. If an
external camera disk is stalled, discovery fails early with a concise message
and asks for the flight-specific camera subfolder. Outputs must be selected
separately and may not be inside either raw-data root.

The dashboard integrates the campaign instrument adapters and keeps scanning,
processing, and saved Flight Projects behind one local browser interface.

## GoPro capture map

1. Select the **Flight Folder**, **Camera Folder**, and **Output Folder**.
2. Run **Initial Check**, process Noseboom, and start **Remote Sensing**.
3. Open the **GoPro** instrument card to view capture locations.

GoPro EXIF clock values are interpreted in `Europe/Berlin` (CET/CEST), changed
to UTC immediately during Camera Folder detection, and shown as a corrected
UTC availability interval in the main GUI before remote-sensing processing.
Captures are then matched to the nearest processed Noseboom 1 Hz navigation
sample within 2.5 seconds. A marker popup shows latitude, longitude, altitude,
image ID, and UTC capture time. **Show more** streams the image with percentage
progress; clicking the image reveals download and full-screen controls.

## Development

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest
python3 -m app.main
```

Python 3.10 and newer are supported. Startup opens
`http://127.0.0.1:8765/` automatically in the default browser. Use
`python3 -m app.main --no-browser` only when a server-only launch is preferred.

## One-click launchers

- **Windows:** double-click `Start_CCFLUX_Dashboard.bat`.
- **macOS Finder:** double-click `Start_CCFLUX_Dashboard.command`.
- **macOS Terminal:** open Terminal in the project folder and run:

  ```bash
  bash Start_CCFLUX_Dashboard.sh
  ```

Both launchers create a private virtual environment, install missing libraries, start the
dashboard on an available local port, and request the default browser to open.
Launcher diagnostics are appended to `logs/launcher.log`.

On macOS, a Windows-style `.venv/Scripts` folder is preserved and a separate
`.venv-macos` is created automatically.

The `logs/` and `outputs/` directories are placeholders for local development.
Production projects will use an operator-selected output directory.
