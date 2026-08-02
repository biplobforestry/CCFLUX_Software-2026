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

- **Windows:** double-click `Windows_CCFLUX.bat`.
- **macOS:** double-click `Mac_CCFLUX.command`, or run `bash Mac_CCFLUX.command`
  from Terminal in the project folder.

Each launcher creates a private virtual environment, installs missing libraries,
starts the dashboard on an available local port, and requests the default browser
to open. An unsupported or missing Python is offered an installer rather than
simply refused. Launcher diagnostics are appended to `logs/launcher.log`.

The two environments are named apart — `.venv-windows` and `.venv-macos` — so one
folder can be shared between platforms without either overwriting the other.
There is exactly one launcher per platform; earlier `Start_CCFLUX_Dashboard.*`
files built a third environment named `.venv` and have been removed, because
running one while updating the other looked precisely like an update that had
failed to apply.

The `logs/` and `outputs/` directories are placeholders for local development.
Production projects will use an operator-selected output directory.
