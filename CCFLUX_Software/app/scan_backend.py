"""Background Flight Folder scanning state for the local dashboard."""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import sys
from collections import defaultdict
import threading
import time
import importlib.util
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd
from uuid import uuid4

from core.browser_payload import decimate_for_view
from core.configuration import load_detection_configuration
from core.detector import InputCandidate
from core.dashboard_time import (
    CAMERA_INSTRUMENTS,
    DashboardTimeState,
    parse_dashboard_datetime,
)
from core.enums import DetectionStatus, ProcessingStatus
from core.hybrid_processing import (
    MAXIMUM_FUSION_PACKAGES,
    HybridPlan,
    WorkerAssignment,
    create_work_packages,
    export_result_package,
    fuse,
    load_result_package,
    review_fusion,
)
from core.hybrid_processing import load_work_package as load_work_package_file
from core.flight_project import (
    LEGACY_PROJECT_FILENAME,
    PROJECT_FILENAME,
    PROJECT_SUFFIX,
    FlightProject,
    FlightProjectStore,
    InstrumentProjectState,
)
from core.gopro_georeference import georeference_captures, public_capture
from core.flir_georeference import georeference_temperature_records
from core.flir_discovery import (
    describe_selection,
    discover_flir_exports,
    locate_time_window_bytes,
    select_exports_for_interval,
)
from core.instrument_registry import InstrumentRegistry
from core.logging_manager import LogLevel, ProcessingLogManager
from .noseboom_statistics_export import NoseboomStatisticsExportManager
from core.resource_manager import CameraBatchPolicy, GIB, ResourceLimits, ResourceManager
from core.priority_manager import create_default_priority_queue
from core.exceptions import ProcessingCancelledError
from core.processing_manager import (
    JobOutcome,
    ProcessingContext,
    ProcessingJob,
    ProcessingScheduler,
    WorkerGroup,
    worker_group_capacities,
)
from core.camera_level2 import (
    level2_capability_snapshot,
    validate_level2_selection,
)
from core.scanner import (
    FlightFolderScanner,
    InstrumentCandidate,
    ScanCancellationToken,
    ScanProgress,
    ScanReport,
)
from instruments.micasense import MicaSenseLevel1Adapter
from instruments.flir import FlirLevel1Adapter
from instruments.gopro import GoProLevel1Adapter
from core.time_extraction import TimestampExtractor
from core.update_check import UpdateStatus, check_for_update
from core.version import SOFTWARE_VERSION
from core.timestamp_repair import repair_chronology, repair_interval


INTEGRATED_PROCESSING_JOB_IDS = frozenset({
    "noseboom",
    "miro",
    "picarro",
    "opc_hbx4",
    "opc_hbx5",
    "partector",
    "ins_gimbal",
    "sif",
    "micasense_quick",
    "flir_quick",
    "gopro_quick",
})

# Job states after which no further update arrives, so the project must be
# persisted immediately rather than waiting for the next throttled checkpoint.
TERMINAL_PROCESSING_STATUSES = frozenset({
    ProcessingStatus.COMPLETE,
    ProcessingStatus.WARNING,
    ProcessingStatus.FAILED,
    ProcessingStatus.CANCELLED,
})

# Minimum wall-clock gap between checkpoints triggered by progress reports.
PROGRESS_CHECKPOINT_INTERVAL_SECONDS = 5.0

# Upper bound on a project file considered during Load discovery. Compressed
# projects carry their generated products, so the former 10 MB plain-JSON limit
# would have skipped most real ones.
PROJECT_DISCOVERY_BYTE_LIMIT = 2 * 1024 * 1024 * 1024

GOPRO_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
GOPRO_FOLDER_NAME = "GoPro"
GOPRO_FOLDER_REQUIREMENT = (
    'The selected folder must be named "GoPro", or contain a folder named '
    '"GoPro" holding the camera media.'
)
GOPRO_RECONNECT_PROMPT = "Do you have the hard disk with the GoPro data?"
GOPRO_MEDIA_UNAVAILABLE = (
    "The GoPro image is not available because the camera media is not "
    "reachable. Reconnect the hard disk holding the GoPro folder."
)
GOPRO_NO_DISK_MESSAGE = (
    "Sorry — the GoPro images are stored on the campaign hard disk and are not "
    "part of the project file. Please contact Dr. Eva Pfannerstill, "
    "Dr. Georgios I. Gkatzelis or Biplob Dey."
)

# FLIR Level 2 defaults. Apparent mode needs no environment measurements and is
# a sensor sanity check; corrected mode is quantitative only when every value is
# a recorded measurement, which the provenance field records.
DEFAULT_FLIR_LEVEL2_OPTIONS: dict[str, object] = {
    "mode": "apparent",
    "environment_inputs_provenance": "assumed_for_testing",
    "emissivity": None,
    "object_distance_m": None,
    "atmospheric_temperature_c": None,
    "reflected_apparent_temperature_c": None,
    "relative_humidity_percent": None,
    "external_optics_transmission": 1.0,
    "external_optics_temperature_c": None,
    "valid_temperature_min_c": None,
    "valid_temperature_max_c": None,
    "save_temperature_npz": False,
}

LOGGER = logging.getLogger(__name__)

# Shown on the packages so a worker can see which campaign they belong to.
CAMPAIGN_NAME = "CC-FLUX Campaign 2026"
# Presentation order for the hybrid plan; the same order the cards use.
INSTRUMENT_ORDER = (
    "noseboom", "miro", "picarro", "opc_hbx4", "opc_hbx5",
    "partector", "ins_gimbal", "sif", "gopro", "flir", "micasense",
)

DEFAULT_SIF_OPTIONS: dict[str, object] = {
    "modes": ["FULL", "FLUO"],
    "position_mode": "uav_airship",
    "raw_min_kb": 100.0,
    "apply_nonlinearity_correction": False,
    "spectral_shift_correction": False,
    "drop_unmatched_telemetry": True,
    "drop_invalid_spectral_rows": False,
    "altitude_filter": False,
    "max_position_gap_seconds": 0.2,
    "static_lat": None,
    "static_lon": None,
    "static_alt": None,
    # None means the bundled CAL_FROG / Indices_ICOS files. An operator with a
    # recalibrated instrument, or a different index definition list, points
    # these at their own files instead.
    "calibration_full": None,
    "calibration_fluo": None,
    "indices_file": None,
}

# The order the SIF pipeline reports, with the progress value each stage has
# reached by the time it completes. The adapter already emits a percentage and a
# phase; mapping that onto a fixed list is what lets the GUI show a checklist
# rather than a single moving bar.
SIF_PROGRESS_STAGES: tuple[dict[str, object], ...] = (
    {"key": "validate", "label": "Validating AirFloX files and calibration", "percent": 10.0},
    {"key": "position", "label": "Preparing Gimbal attitude and Noseboom position", "percent": 22.0},
    {"key": "full", "label": "FULL / FLOX radiance, reflectance and indices", "percent": 59.0},
    {"key": "fluo", "label": "FLUO fluorescence, SIF A/B iFLD and indices", "percent": 90.0},
    {"key": "export", "label": "Writing exports, maps and GIS", "percent": 99.0},
    {"key": "complete", "label": "SIF processing complete", "percent": 100.0},
)


@dataclass(slots=True)
class InstrumentScanState:
    instrument_id: str
    display_name: str
    physical_group: str
    detection_status: DetectionStatus = DetectionStatus.NOT_DETECTED
    file_count: int = 0
    confidence: float | None = None
    candidate_paths: list[str] = field(default_factory=list)
    ambiguous: bool = False
    warnings: list[str] = field(default_factory=list)
    timestamp_warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    utc_start_time: datetime | None = None
    utc_end_time: datetime | None = None
    original_start_time: str | None = None
    original_end_time: str | None = None
    processing_status: str = "idle"
    processing_progress: float = 0.0
    processing_step: str = "Not started"
    processing_elapsed_seconds: float = 0.0
    output_files: list[str] = field(default_factory=list)
    quicklook: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "display_name": self.display_name,
            "physical_group": self.physical_group,
            "detection_status": self.detection_status.value,
            "file_count": self.file_count,
            "confidence": self.confidence,
            "candidate_paths": list(self.candidate_paths),
            "ambiguous": self.ambiguous,
            "warnings": list(self.warnings),
            "timestamp_warnings": list(self.timestamp_warnings),
            "errors": list(self.errors),
            "utc_start_time": _iso(self.utc_start_time),
            "utc_end_time": _iso(self.utc_end_time),
            "original_start_time": self.original_start_time,
            "original_end_time": self.original_end_time,
            "processing_status": self.processing_status,
            "processing_progress": self.processing_progress,
            "processing_step": self.processing_step,
            "processing_elapsed_seconds": self.processing_elapsed_seconds,
            "output_files": list(self.output_files),
            "quicklook": dict(self.quicklook),
        }


def _instrument_is_processable(state: InstrumentScanState) -> bool:
    """Allow scientifically usable warning data while retaining health advisories."""
    return (
        state.detection_status in {DetectionStatus.READY, DetectionStatus.WARNING}
        and not state.ambiguous
        and not state.errors
    )


class FolderDialog:
    """Foreground native chooser, injectable for automated tests."""

    @staticmethod
    def _choose_with_windows_dialog(
        method_name: str,
        *,
        title: str,
        filetypes: tuple[tuple[str, str], ...] | None = None,
    ) -> Path | None:
        escaped_title = title.replace("'", "''")
        if method_name == "askdirectory":
            dialog_setup = (
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                f"$dialog.Description = '{escaped_title}'; "
                "$dialog.ShowNewFolderButton = $true; "
            )
            selected_property = "SelectedPath"
        else:
            filter_value = "|".join(
                f"{label}|{pattern}" for label, pattern in (filetypes or ())
            ).replace("'", "''")
            dialog_setup = (
                "$dialog = New-Object System.Windows.Forms.OpenFileDialog; "
                f"$dialog.Title = '{escaped_title}'; "
                f"$dialog.Filter = '{filter_value}'; "
                "$dialog.CheckFileExists = $true; $dialog.Multiselect = $false; "
            )
            selected_property = "FileName"
        script = (
            "Add-Type @'\n"
            "using System;\nusing System.Runtime.InteropServices;\n"
            "public static class CCFluxDialogWindow {\n"
            "[DllImport(\"user32.dll\", CharSet = CharSet.Auto)] public static extern IntPtr FindWindow(string className, string windowName);\n"
            "[DllImport(\"user32.dll\", SetLastError = true)] public static extern bool SetWindowPos(IntPtr handle, IntPtr insertAfter, int x, int y, int width, int height, uint flags);\n"
            "}\n'@; "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.Application]::EnableVisualStyles(); "
            "$owner = New-Object System.Windows.Forms.Form; "
            "$owner.TopMost = $true; $owner.ShowInTaskbar = $false; "
            "$owner.Opacity = 0; $owner.Width = 1; $owner.Height = 1; "
            "$owner.StartPosition = 'CenterScreen'; "
            + dialog_setup
            + "$resizeTimer = New-Object System.Windows.Forms.Timer; "
            + "$resizeTimer.Interval = 35; "
            + "$resizeTimer.Add_Tick({ "
            + "$dialogHandle = [CCFluxDialogWindow]::FindWindow('#32770', $null); "
            + "if ($dialogHandle -ne [IntPtr]::Zero) { "
            + "[CCFluxDialogWindow]::SetWindowPos($dialogHandle, [IntPtr]::Zero, 0, 0, 980, 700, 0x0056); "
            + "$resizeTimer.Stop() } }); "
            + "$resizeTimer.Start(); "
            + "$owner.Show(); try { "
            "$result = $dialog.ShowDialog($owner); "
            "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { "
            f"[Console]::Out.Write($dialog.{selected_property})"
            " } } finally { $resizeTimer.Stop(); $resizeTimer.Dispose(); $dialog.Dispose(); $owner.Close(); $owner.Dispose() }"
        )
        run_options: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "check": False,
        }
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if create_no_window:
            run_options["creationflags"] = create_no_window
        startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_type is not None:
            startupinfo = startupinfo_type()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            run_options["startupinfo"] = startupinfo
        # Bounded, like the macOS chooser. Without this a PowerShell dialog that
        # never returns - blocked by an execution policy, or opened where the
        # operator cannot see it - holds the single-dialog lock for the life of
        # the session, and every later attempt to choose a folder fails.
        run_options["timeout"] = FolderDialog.CHOOSER_TIMEOUT_SECONDS
        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-STA",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    script,
                ],
                **run_options,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "The folder window did not return within "
                f"{FolderDialog.CHOOSER_TIMEOUT_SECONDS // 60} minutes and was "
                "closed. If you never saw it, type the folder path instead."
            ) from None
        except OSError as exc:
            raise RuntimeError(
                f"The folder window could not be opened: {exc}. "
                "Type the folder path instead."
            ) from None
        if completed.returncode != 0:
            details = completed.stderr.strip() or "Windows dialog process failed"
            raise RuntimeError(
                f"{details} Type the folder path instead."
            )
        selected = completed.stdout.strip()
        return Path(selected) if selected else None

    @classmethod
    def _choose_native(
        cls,
        method_name: str,
        *,
        title: str,
        filetypes: tuple[tuple[str, str], ...] | None = None,
    ) -> Path | None:
        if sys.platform.startswith("win"):
            return cls._choose_with_windows_dialog(
                method_name, title=title, filetypes=filetypes
            )
        return cls._choose_with_tkinter(
            method_name, title=title, filetypes=filetypes
        )

    @staticmethod
    def _choose_with_tkinter(
        method_name: str,
        *,
        title: str,
        filetypes: tuple[tuple[str, str], ...] | None = None,
    ) -> Path | None:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        try:
            try:
                root.attributes("-topmost", True)
                root.lift()
                root.update_idletasks()
                root.update()
            except (tkinter.TclError, AttributeError):
                # Some window managers do not expose every foreground hint.
                pass
            options: dict[str, object] = {"title": title, "parent": root}
            if filetypes is not None:
                options["filetypes"] = filetypes
            selected = getattr(filedialog, method_name)(**options)
        finally:
            root.destroy()
        return Path(selected) if selected else None

    def choose_flight_folder(self) -> Path | None:
        if sys.platform == "darwin":
            return self._choose_with_osascript(
                'choose folder with prompt '
                '"Select the root folder for one Zeppelin flight"'
            )
        return self._choose_native(
            "askdirectory",
            title="Select the root folder for one Zeppelin flight",
        )

    def choose_output_folder(self) -> Path | None:
        if sys.platform == "darwin":
            return self._choose_with_osascript(
                'choose folder with prompt '
                '"Select the independent CCFLUX Output Folder"'
            )
        return self._choose_native(
            "askdirectory",
            title="Select CCFLUX Output Folder",
        )

    # A generous backstop, not a time limit on browsing. Its purpose is to stop
    # a dialog nobody can see from wedging folder selection for the session.
    CHOOSER_TIMEOUT_SECONDS = 600

    @staticmethod
    def _run_chooser_script(script: str) -> tuple[Path | None, str | None]:
        """Run one AppleScript chooser. Returns (selection, failure reason).

        A cancel and a failure used to be indistinguishable: every non-zero exit
        became "cancelled", so a machine that refused the Automation permission
        looked exactly like an operator changing their mind, and folder
        selection simply did nothing with no way to find out why. AppleScript
        reports a cancel as error -128, which is what separates the two here.
        """
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=FolderDialog.CHOOSER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return None, (
                "The folder window was open for longer than "
                f"{FolderDialog.CHOOSER_TIMEOUT_SECONDS // 60} minutes and was "
                "closed. If you never saw it, it may have opened behind another "
                "window."
            )
        except OSError as exc:
            return None, f"The folder window could not be opened: {exc}"
        if completed.returncode == 0:
            selected = completed.stdout.strip()
            return (Path(selected) if selected else None), None
        error = completed.stderr.strip()
        if "-128" in error or "cancel" in error.casefold():
            return None, None          # the operator pressed Cancel
        return None, error or "The folder window closed without a selection."

    def _choose_with_osascript(self, chooser_clause: str) -> Path | None:
        """Run a macOS chooser out of process, in front of the browser.

        Tk must own the main thread. These choosers are invoked from an HTTP
        request thread, so building a Tk root there means the panel never
        appears and the request dies - the browser reports "Failed to fetch".
        Every chooser therefore goes through osascript on macOS.

        osascript alone is not enough. It is a background-only process, so a
        panel it owns opens *behind* the browser: the operator sees the "select
        a folder" prompt, no window, and an action that appears to hang. Hosting
        the chooser inside a Finder tell block gives the panel a foreground
        owner that can be activated.

        That costs an Automation permission, which a machine can refuse. If the
        Finder route fails for any reason other than a cancel, the plain
        osascript chooser is tried instead - it needs no permission, and a panel
        behind the browser is far better than no panel at all.

        ``chooser_clause`` is the AppleScript expression that returns the
        selection, for example ``choose folder with prompt "..."``.
        """
        hosted = (
            'tell application "Finder"\n'
            "\tactivate\n"
            f"\tset ccfluxSelection to {chooser_clause}\n"
            "end tell\n"
            "POSIX path of ccfluxSelection"
        )
        selection, failure = self._run_chooser_script(hosted)
        if selection is not None or failure is None:
            return selection
        # Finder could not be used. Say so where the operator will see it: this
        # used to go to a module logger wired to nothing, so a machine that
        # refused the Automation permission left no trace anywhere.
        LOGGER.warning("Finder could not open the folder window: %s", failure)
        self._chooser_warning = (
            f"The folder window could not be opened through Finder ({failure}). "
            "Trying again without it; it may appear behind the browser."
        )
        selection, fallback_failure = self._run_chooser_script(
            f"POSIX path of ({chooser_clause})"
        )
        if selection is None and fallback_failure is not None:
            raise RuntimeError(
                "The folder window could not be opened. "
                f"{fallback_failure} On macOS, allow this application to "
                "control Finder under System Settings > Privacy & Security > "
                "Automation, or start the launcher again."
            )
        return selection

    def choose_project_file(self) -> Path | None:
        if sys.platform == "darwin":
            return self._choose_with_osascript(
                'choose file with prompt '
                '"Open a saved CC-FLUX Flight Project (.ccflux)"'
            )
        return self._choose_native(
            "askopenfilename",
            title="Open CC-FLUX Flight Project",
            filetypes=(
                ("CC-FLUX project", "*.ccflux"),
                ("Legacy CC-FLUX JSON project", "flight_project.json"),
            ),
        )

    def choose_sif_essential_file(self, kind: str) -> Path | None:
        prompts = {
            "calibration_full": "Select the FULL / FLOX calibration file (CAL_...csv)",
            "calibration_fluo": "Select the FLUO calibration file (CAL_...csv)",
            "indices_file": "Select the vegetation-index definition file (.txt)",
        }
        prompt = prompts.get(kind, "Select a SIF calibration file")
        if sys.platform == "darwin":
            return self._choose_with_osascript(f'choose file with prompt "{prompt}"')
        return self._choose_native(
            "askopenfilename",
            title=prompt,
            filetypes=(
                ("Calibration or index file", "*.csv *.txt"),
                ("All files", "*.*"),
            ),
        )

    def choose_project_folder(self) -> Path | None:
        if sys.platform == "darwin":
            return self._choose_with_osascript(
                'choose folder with prompt '
                '"Select a folder containing saved CC-FLUX Flight Projects"'
            )
        return self._choose_native(
            "askdirectory",
            title="Select a folder containing saved CC-FLUX Flight Projects",
        )

    def choose_camera_folder(self) -> Path | None:
        if sys.platform == "darwin":
            return self._choose_with_osascript(
                'choose folder with prompt '
                '"Select the Camera System data folder for this flight"'
            )
        return self._choose_native(
            "askdirectory",
            title="Select the Camera System data folder for this flight",
        )


class DashboardScanBackend:
    """Own one cancellable scan while exposing thread-safe dashboard snapshots."""

    def __init__(
        self,
        application_root: Path,
        *,
        folder_dialog: FolderDialog | None = None,
        logger: ProcessingLogManager | None = None,
        scanner_factory: Callable[[], FlightFolderScanner] | None = None,
    ) -> None:
        self.application_root = Path(application_root)
        self.folder_dialog = folder_dialog or FolderDialog()
        self.logger = logger or ProcessingLogManager(
            self.application_root / "logs" / "processing.jsonl"
        )
        self.resource_manager = ResourceManager(logger=self.logger)
        system = self.resource_manager.system
        balanced_workers = _balanced_worker_count(system)
        balanced_ram_target = min(
            system.safely_available_ram_bytes,
            max(1, int(system.total_ram_bytes * 0.25)),
        )
        balanced_ram = max(
            (
                value * GIB
                for value in (1, 2, 4, 8, 16, 32, 64, 128)
                if value * GIB <= balanced_ram_target
            ),
            default=min(system.safely_available_ram_bytes, GIB),
        )
        self._resource_limits = self.resource_manager.create_limits(
            balanced_workers,
            balanced_ram,
        )
        self._resources_auto_selected = True
        self._dialog_lock = threading.Lock()
        self.processing_queue = create_default_priority_queue()
        self._scheduler: ProcessingScheduler | None = None
        self._registry = InstrumentRegistry()
        self._scanner_factory = scanner_factory or self._default_scanner
        self._lock = threading.RLock()
        self._token: ScanCancellationToken | None = None
        self._scan_tokens: dict[str, ScanCancellationToken] = {}
        self._scan_channels = {
            "flight": self._new_scan_channel("flight"),
            "camera": self._new_scan_channel("camera"),
        }
        self._worker: threading.Thread | None = None
        self._scan_id: str | None = None
        self._selected_folder: Path | None = None
        self._selected_camera_folder: Path | None = None
        self._phase = "idle"
        self._current_folder: Path | None = None
        self._current_file: Path | None = None
        self._current_instrument: str | None = None
        self._files_scanned = 0
        self._progress: float | None = None
        self._detected: tuple[str, ...] = ()
        self._messages: list[str] = []
        self._cancelled = False
        self._error: str | None = None
        self._report: ScanReport | None = None
        self._instruments = self._new_instrument_states()
        self._time_state = DashboardTimeState()
        self._flight_project: FlightProject | None = None
        self._selected_output_folder: Path | None = None
        self._project_store = FlightProjectStore()
        self._noseboom_straight_settings: dict[str, float] = {}
        self._noseboom_preview_quicklook: dict[str, object] | None = None
        self._noseboom_preview_settings: dict[str, float] | None = None
        self._noseboom_recalculation: dict[str, object] = {
            "job_id": None,
            "running": False,
            "ready": False,
            "progress": 0.0,
            "message": "No straight-flight recalculation is running.",
            "error": None,
            "result": None,
            "started_monotonic": None,
        }
        self._noseboom_recalculation_thread: threading.Thread | None = None
        self._noseboom_statistics_export = NoseboomStatisticsExportManager(
            self.logger, self._save_noseboom_statistics_exports
        )
        self._miro_rack_bridge: object | None = None
        self._hatchbox_view_lock = threading.RLock()
        self._sif_options = dict(DEFAULT_SIF_OPTIONS)
        # The remote-sensing interval is chosen against camera coverage and is
        # kept apart from the flight Time Filter, which the cameras no longer
        # take part in.
        self._dialog_holder: tuple[str | None, float | None] = (None, None)
        # Set only on a worker computer, by adopting a hybrid work package. Its
        # presence is what makes the scientific configuration read-only.
        self._work_package = None
        self._camera_selected_start: datetime | None = None
        self._camera_selected_end: datetime | None = None
        self._flir_level2_options = dict(DEFAULT_FLIR_LEVEL2_OPTIONS)
        self._update_status: UpdateStatus | None = None
        self._last_checkpoint_monotonic = 0.0
        # Set when an operator reconnects the camera disk somewhere new.
        self._gopro_media_root: Path | None = None
        self._gopro_index_cache: tuple[Path, dict[str, Path]] | None = None

    def attach_miro_rack_bridge(self, bridge: object) -> None:
        """Connect shared MIRO/Picarro browser science to queue processing."""
        with self._lock:
            self._miro_rack_bridge = bridge

    def _require_miro_rack_bridge(self) -> object:
        """Return the MIRO Rack bridge, creating it if nothing attached one.

        Picarro has no standalone adapter — all of its science lives in the
        preserved MIRO Rack application. Only the HTTP server attached the
        bridge, so Picarro failed with "the shared MIRO Rack processing bridge
        is unavailable" whenever processing ran without it, which is every
        headless run and every test. Building it on demand makes the dependency
        an implementation detail rather than a launch-order requirement.
        """
        with self._lock:
            bridge = self._miro_rack_bridge
        if bridge is not None:
            return bridge
        try:
            from .miro_rack_bridge import MiroRackBridge

            bridge = MiroRackBridge(self.application_root, self)
        except Exception as exc:
            raise RuntimeError(
                "Picarro processing needs the preserved MIRO Rack application, "
                f"which could not be loaded: {exc}"
            ) from exc
        with self._lock:
            # Another thread may have attached one while this was loading.
            if self._miro_rack_bridge is None:
                self._miro_rack_bridge = bridge
            return self._miro_rack_bridge

    @staticmethod
    def _new_scan_channel(
        source: str, root: Path | None = None, *, running: bool = False
    ) -> dict[str, object]:
        return {
            "source": source,
            "root": root,
            "phase": "starting" if running else "idle",
            "running": running,
            "current_folder": root,
            "current_file": None,
            "current_instrument": None,
            "files_scanned": 0,
            "folder_counts": defaultdict(int),
            "last_group": None,
            "progress": None,
            "detected_instruments": (),
            "message": (
                f"{source.title()} data discovery is starting." if running else ""
            ),
            "cancelled": False,
            "error": None,
        }

    @staticmethod
    def _scan_channel_snapshot(channel: dict[str, object]) -> dict[str, object]:
        return {
            **channel,
            "root": str(channel["root"]) if channel["root"] else None,
            "current_folder": (
                str(channel["current_folder"])
                if channel["current_folder"]
                else None
            ),
            "current_file": (
                str(channel["current_file"]) if channel["current_file"] else None
            ),
            "detected_instruments": list(channel["detected_instruments"]),
            "folder_counts": [
                {"name": name, "files": count}
                for name, count in sorted(
                    channel["folder_counts"].items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
        }

    @staticmethod
    def _scan_group_name(root, current_file) -> str | None:
        """Which top-level entry under the scan root a file belongs to."""
        if current_file is None:
            return None
        try:
            relative = Path(current_file).relative_to(Path(root))
        except (TypeError, ValueError):
            return Path(current_file).name
        return relative.parts[0] if relative.parts else Path(current_file).name

    def select_output_folder(self, folder: str | Path | None = None) -> dict[str, object]:
        typed = self._typed_folder("output-folder", folder)
        if typed is None:
            self.logger.log(
                LogLevel.INFO, "output-folder", "Opening Output Folder chooser"
            )
        folder = typed or self._choose_folder_once(
            "output-folder", self.folder_dialog.choose_output_folder
        )
        if folder is None:
            self.logger.log(
                LogLevel.INFO, "output-folder", "Output Folder selection cancelled"
            )
            return {"cancelled": True}
        output = Path(folder).expanduser().resolve()
        if not output.is_dir():
            raise ValueError(f"Output Folder does not exist: {output}")
        probe = output / f".ccflux-write-test-{uuid4().hex}"
        try:
            probe.write_text("test", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"Output Folder is not writable: {output}") from exc
        with self._lock:
            if self._selected_folder:
                raw = self._selected_folder.resolve(strict=False)
                if output == raw or output.is_relative_to(raw) or raw.is_relative_to(output):
                    raise ValueError("Output Folder must be independent from Flight Folder")
            self._selected_output_folder = output
            if self._flight_project:
                self._flight_project.output_folder_path = output
            project = self._flight_project
        # Once a flight scan exists, choosing an Output Folder should also
        # establish a recoverable project checkpoint. Previously this action
        # created only the log folder, leaving nothing for Load .ccflux to open.
        if project is not None:
            self._checkpoint_project()
        self.logger.log(LogLevel.SUCCESS, "project", "Output Folder selected", file_path=output)
        return {
            "cancelled": False,
            "folder": str(output),
            "project_file": (
                str(project.project_file)
                if project is not None and project.project_file.is_file()
                else None
            ),
            "project_saved": bool(
                project is not None and project.project_file.is_file()
            ),
        }

    def select_and_start(self) -> dict[str, object]:
        selection = self.select_folders()
        if selection["cancelled"]:
            return selection
        return self.start_scan(
            Path(str(selection["folder"])),
            camera_folder=(
                Path(str(selection["camera_folder"]))
                if selection.get("camera_folder")
                else None
            ),
        )

    def _typed_folder(self, component: str, folder: str | Path | None) -> Path | None:
        """Accept a path the operator typed instead of opening a window."""
        if folder in (None, ""):
            return None
        candidate = Path(str(folder)).expanduser()
        if not candidate.is_dir():
            raise ValueError(f"No such folder: {candidate}")
        resolved = candidate.resolve(strict=False)
        self.logger.log(
            LogLevel.INFO, component, f"Folder entered directly: {resolved}",
            file_path=resolved,
        )
        return resolved

    def _choose_folder_once(
        self, component: str, chooser: Callable[[], Path | None]
    ) -> Path | None:
        """Prevent delayed duplicate native pickers from stacking behind the GUI.

        The refusal names which window is already open and when it was opened.
        It used to say only that one was, which is no help when the window is
        behind the browser and the operator cannot find it - every later attempt
        then failed with the same unexplained message.
        """
        if not self._dialog_lock.acquire(blocking=False):
            holder, since = self._dialog_holder
            waited = (
                f" for {int(time.monotonic() - since)} second(s)"
                if since is not None else ""
            )
            raise RuntimeError(
                f"A {holder or 'folder'} selection window is already open"
                f"{waited}. It may be behind the browser - look for it in the "
                "Dock, or answer it, and try again."
            )
        self._dialog_holder = (component, time.monotonic())
        try:
            self.logger.log(LogLevel.INFO, component, "Opening folder chooser")
            self.folder_dialog._chooser_warning = None
            selected = chooser()
            note = getattr(self.folder_dialog, "_chooser_warning", None)
            if note:
                self.logger.log(LogLevel.WARNING, component, note)
            return selected
        finally:
            self._dialog_holder = (None, None)
            self._dialog_lock.release()

    def select_folders(self, folder: str | Path | None = None) -> dict[str, object]:
        """Select only the Flight Folder; camera selection is an explicit action.

        ``folder`` sets it directly. macOS decides for itself whether a window
        opened by a launcher-started server may come to the front, so the native
        chooser sometimes appears behind the browser and the operator sees
        nothing happen. Typing the path always works.
        """
        folder = self._typed_folder("flight-folder", folder) or self._choose_folder_once(
            "flight-folder", self.folder_dialog.choose_flight_folder
        )
        if folder is None:
            self.logger.log(
                LogLevel.INFO, "flight-folder", "Flight Folder selection cancelled"
            )
            return {"cancelled": True}
        resolved = Path(folder).expanduser().resolve()
        with self._lock:
            self._selected_folder = resolved
            self._selected_camera_folder = None
            self._phase = "folder-selected"
            self._current_folder = None
            self._current_file = None
            self._current_instrument = None
            self._files_scanned = 0
            self._progress = None
            self._detected = ()
            self._messages = [
                "Flight Folder selected. Click Initial Check to scan files."
            ]
            self._error = None
            self._report = None
            self._instruments = self._new_instrument_states()
            self._time_state = DashboardTimeState()
            self._flight_project = None
            self._sif_options = dict(DEFAULT_SIF_OPTIONS)
            self.processing_queue = create_default_priority_queue()
            self._noseboom_preview_quicklook = None
            self._noseboom_preview_settings = None
            self._noseboom_recalculation = {
                "job_id": None,
                "running": False,
                "ready": False,
                "progress": 0.0,
                "message": "No straight-flight recalculation is running.",
                "error": None,
                "result": None,
                "started_monotonic": None,
            }
            self._noseboom_recalculation_thread = None
            self._token = None
            self._scan_tokens = {}
            self._worker = None
            self._scan_channels = {
                "flight": self._new_scan_channel("flight", resolved),
                "camera": self._new_scan_channel("camera"),
            }
            self._scan_channels["flight"]["phase"] = "folder-selected"
            self._scan_channels["flight"]["message"] = (
                "Flight Folder selected. Click Initial Check to scan files."
            )
            self._scan_channels["camera"]["phase"] = "not_selected"
        self.logger.log(
            LogLevel.SUCCESS,
            "flight-folder",
            "Flight Folder selected; scanning is waiting for Initial Check",
            file_path=resolved,
            processing_step="folder-selected",
        )
        return {
            "cancelled": False,
            "folder": str(resolved),
            "camera_folder": None,
        }

    def select_camera_folder(self, folder: str | Path | None = None) -> dict[str, object]:
        chooser = getattr(self.folder_dialog, "choose_camera_folder", None)
        if not callable(chooser):
            raise RuntimeError("Camera Folder selection is not available")
        folder = self._typed_folder("camera-folder", folder) or self._choose_folder_once(
            "camera-folder", chooser
        )
        if folder is None:
            self.logger.log(
                LogLevel.INFO, "camera-folder", "Camera Folder selection cancelled"
            )
            return {"cancelled": True}
        resolved = Path(folder).expanduser().resolve()
        with self._lock:
            flight = self._selected_folder
        if flight and (
            resolved == flight
            or resolved.is_relative_to(flight)
            or flight.is_relative_to(resolved)
        ):
            raise ValueError("Camera Folder and Flight Folder must be independent")
        with self._lock:
            self._selected_camera_folder = resolved
            camera_channel = self._new_scan_channel("camera", resolved)
            camera_channel["phase"] = "folder-selected"
            camera_channel["message"] = (
                "Camera Folder selected. Click Initial Check and choose whether "
                "to include camera scanning."
            )
            self._scan_channels["camera"] = camera_channel
            self._messages.append(
                "Camera Folder selected; scanning is waiting for Initial Check."
            )
            if self._flight_project is not None:
                self._flight_project.camera_folder_path = resolved
        self.logger.log(
            LogLevel.SUCCESS,
            "camera-folder",
            "Camera Folder selected; no scan was started",
            file_path=resolved,
            processing_step="folder-selected",
        )
        return {"cancelled": False, "folder": str(resolved)}

    def start_scan(
        self,
        folder: Path,
        *,
        camera_folder: Path | None = None,
        include_camera: bool = True,
    ) -> dict[str, object]:
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Flight Folder does not exist: {root}")
        selected_camera_root = (
            Path(camera_folder).expanduser().resolve()
            if camera_folder is not None
            else None
        )
        if selected_camera_root is not None:
            if not selected_camera_root.is_dir():
                raise ValueError(
                    f"Camera System Folder does not exist: {selected_camera_root}"
                )
            if (
                selected_camera_root == root
                or selected_camera_root.is_relative_to(root)
                or root.is_relative_to(selected_camera_root)
            ):
                raise ValueError(
                    "Flight Folder and Camera System Folder must be independent"
                )
        camera_root = selected_camera_root if include_camera else None
        if camera_root is not None:
            _assert_directory_responsive(camera_root)
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise RuntimeError("A Flight Folder scan is already running")
            existing_project = (
                self._flight_project
                if self._flight_project is not None
                and self._flight_project.flight_folder_path.resolve(
                    strict=False
                )
                == root.resolve(strict=False)
                else None
            )
            incremental = bool(
                existing_project is not None
                and existing_project.completed_jobs
            )
            self._scan_id = uuid4().hex
            self._selected_folder = root
            self._selected_camera_folder = selected_camera_root
            self._phase = "starting"
            self._current_folder = root
            self._current_file = None
            self._current_instrument = None
            self._files_scanned = 0
            self._progress = None
            self._detected = ()
            self._messages = [
                (
                    "Incremental scan is starting. Previously processed "
                    "instruments will remain completed and skipped."
                    if incremental
                    else "Flight Folder selected; discovery is starting."
                )
            ]
            self._cancelled = False
            self._error = None
            self._report = None
            if not incremental:
                self._instruments = self._new_instrument_states()
            self._time_state = DashboardTimeState()
            if existing_project is not None:
                self._flight_project = existing_project
                self._flight_project.camera_folder_path = selected_camera_root
                self._flight_project.cpu_allocation = (
                    self._resource_limits.worker_count
                )
                self._flight_project.ram_allocation_bytes = (
                    self._resource_limits.memory_bytes
                )
                if self._selected_output_folder is not None:
                    self._flight_project.output_folder_path = (
                        self._selected_output_folder
                    )
            else:
                self._flight_project = FlightProject(
                    flight_id=root.name,
                    flight_folder_path=root,
                    output_folder_path=self.application_root / "outputs",
                    camera_folder_path=selected_camera_root,
                    cpu_allocation=self._resource_limits.worker_count,
                    ram_allocation_bytes=self._resource_limits.memory_bytes,
                )
            self._sync_project_queue_state()
            flight_token = ScanCancellationToken()
            camera_token = ScanCancellationToken() if camera_root is not None else None
            self._token = flight_token
            self._scan_tokens = {"flight": flight_token}
            if camera_token is not None:
                self._scan_tokens["camera"] = camera_token
            self._scan_channels = {
                "flight": self._new_scan_channel("flight", root, running=True),
                "camera": self._new_scan_channel(
                    "camera", camera_root, running=camera_root is not None
                ),
            }
            if camera_root is None:
                if selected_camera_root is not None:
                    self._scan_channels["camera"] = self._new_scan_channel(
                        "camera", selected_camera_root
                    )
                    self._scan_channels["camera"]["phase"] = "folder-selected"
                    self._scan_channels["camera"]["message"] = (
                        "Camera Folder retained but excluded from this scan."
                    )
                else:
                    self._scan_channels["camera"]["phase"] = "not_selected"
            worker = threading.Thread(
                target=self._run_scan,
                args=(root, camera_root, flight_token, camera_token),
                name=f"ccflux-scan-{self._scan_id[:8]}",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            scan_id = self._scan_id
        self.logger.log(
            LogLevel.INFO,
            "flight-scanner",
            (
                "Flight Folder scan started with one bounded discovery worker; "
                "live GUI progress is throttled to 10 updates/s after the first "
                "five files; "
                f"processing allocation is {self._resource_limits.worker_count} CPU "
                f"worker(s) and {self._resource_limits.memory_bytes // GIB} GiB RAM"
            ),
            file_path=root,
            processing_step="discovery",
        )
        return {
            "cancelled": False,
            "scan_id": scan_id,
            "folder": str(root),
            "camera_folder": str(camera_root) if camera_root else None,
            "selected_camera_folder": (
                str(selected_camera_root) if selected_camera_root else None
            ),
        }

    def cancel(self, source: str | None = None) -> bool:
        if source not in {None, "flight", "camera", "all"}:
            raise ValueError("Scan source must be 'flight' or 'camera'")
        with self._lock:
            targets = (
                ("flight", "camera")
                if source in {None, "all"}
                else (source,)
            )
            cancelled_sources = []
            for target in targets:
                token = self._scan_tokens.get(target)
                channel = self._scan_channels[target]
                if token is not None and bool(channel["running"]):
                    token.cancel()
                    channel["message"] = "Cancellation requested; stopping safely."
                    cancelled_sources.append(target)
            if not cancelled_sources:
                return False
            self._messages.append(
                "Cancellation requested for " + ", ".join(cancelled_sources) + " scan."
            )
        for target in cancelled_sources:
            self.logger.cancelled_job(f"{self._scan_id or 'scan'}-{target}")
        return True

    def reset_system(self) -> dict[str, object]:
        """Return the dashboard to a clean state without touching raw data or outputs."""
        with self._lock:
            tokens = tuple(self._scan_tokens.values())
            worker = self._worker
            scheduler = self._scheduler
        for token in tokens:
            token.cancel()
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        if scheduler is not None:
            scheduler.shutdown(wait=False, cancel_pending=True)
        with self._lock:
            self.processing_queue = create_default_priority_queue()
            self._scheduler = None
            self._token = None
            self._scan_tokens = {}
            self._scan_channels = {
                "flight": self._new_scan_channel("flight"),
                "camera": self._new_scan_channel("camera"),
            }
            self._worker = None
            self._scan_id = None
            self._selected_folder = None
            self._selected_camera_folder = None
            self._selected_output_folder = None
            self._phase = "idle"
            self._current_folder = None
            self._current_file = None
            self._current_instrument = None
            self._files_scanned = 0
            self._progress = None
            self._detected = ()
            self._messages = ["System reset completed. No raw data or output files were changed."]
            self._cancelled = False
            self._error = None
            self._report = None
            self._instruments = self._new_instrument_states()
            self._time_state = DashboardTimeState()
            self._flight_project = None
        self.logger.log(
            LogLevel.WARNING,
            "application",
            "Dashboard system state reset by user; files were not modified",
            processing_step="system-reset",
        )
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "scan_id": self._scan_id,
                "selected_folder": (
                    str(self._selected_folder) if self._selected_folder else None
                ),
                "selected_output_folder": (
                    str(self._selected_output_folder)
                    if self._selected_output_folder else None
                ),
                "selected_camera_folder": (
                    str(self._selected_camera_folder)
                    if self._selected_camera_folder else None
                ),
                "flight_id": (
                    self._flight_project.flight_id if self._flight_project else
                    self._selected_folder.name if self._selected_folder else None
                ),
                "project_file": (
                    str(self._flight_project.project_file)
                    if self._flight_project else None
                ),
                "project_saved": bool(
                    self._flight_project
                    and (
                        self._flight_project.project_file.is_file()
                        or (
                            self._flight_project.flight_output_root
                            / "project"
                            / LEGACY_PROJECT_FILENAME
                        ).is_file()
                    )
                ),
                "phase": self._phase,
                "running": any(
                    bool(channel["running"])
                    for channel in self._scan_channels.values()
                ),
                "current_folder": (
                    str(self._current_folder) if self._current_folder else None
                ),
                "current_file": (
                    str(self._current_file) if self._current_file else None
                ),
                "files_scanned": self._files_scanned,
                "progress": self._progress,
                "detected_instruments": list(self._detected),
                "current_instrument": self._current_instrument or (
                    self._instruments[self._detected[-1]].display_name
                    if self._detected
                    else None
                ),
                "messages": list(self._messages[-100:]),
                "cancelled": self._cancelled,
                "error": self._error,
                "instruments": {
                    key: value.to_dict() for key, value in self._instruments.items()
                },
                "summary": self._summary(),
                "time_filter": self._time_state.to_dict(),
                "resources": self._resource_snapshot(),
                "processing_queue": self._queue_snapshot(),
                "level2_capabilities": level2_capability_snapshot(),
                "sif_options": dict(self._sif_options),
                "flir_level2_options": dict(self._flir_level2_options),
                "software_version": SOFTWARE_VERSION,
                "scans": {
                    source: self._scan_channel_snapshot(channel)
                    for source, channel in self._scan_channels.items()
                },
                "camera_scan_ready": (
                    self._scan_channels["camera"]["phase"] == "complete"
                    and not self._scan_channels["camera"]["cancelled"]
                    and self._scan_channels["camera"]["error"] is None
                ),
                "camera_coverage": self._camera_coverage_locked(),
            }

    # The order the remote-sensing products are scanned and presented in.
    CAMERA_SCAN_ORDER = ("gopro", "flir", "micasense")

    def _camera_coverage_locked(self) -> dict[str, object]:
        """UTC coverage of the remote-sensing products, on their own terms.

        The cameras are scanned separately from the flight instruments and are
        selected against their own coverage, so they have their own detected
        minimum and maximum and their own common overlap. Routing them through
        the flight Time Filter meant a camera-only project had no interval to
        offer at all once the cameras were taken out of the flight global.

        Assumes ``self._lock`` is held.
        """
        channel = self._scan_channels["camera"]
        ready = (
            channel["phase"] == "complete"
            and not channel["cancelled"]
            and channel["error"] is None
        )
        products: list[dict[str, object]] = []
        starts: list[datetime] = []
        ends: list[datetime] = []
        for instrument_id in self.CAMERA_SCAN_ORDER:
            state = self._instruments.get(instrument_id)
            if state is None:
                continue
            detected = state.detection_status is not DetectionStatus.NOT_DETECTED
            start, end = state.utc_start_time, state.utc_end_time
            if detected and start is not None and end is not None:
                starts.append(start)
                ends.append(end)
            products.append({
                "instrument_id": instrument_id,
                "display_name": state.display_name,
                "detected": detected,
                "detection_status": getattr(
                    state.detection_status, "value", state.detection_status
                ),
                "file_count": state.file_count,
                "utc_start": _iso_or_none(start),
                "utc_end": _iso_or_none(end),
                # Without a clock there is nothing to select against, so the
                # dialog must not offer it as though there were.
                "selectable": bool(detected and start is not None and end is not None),
                "warnings": list(state.warnings),
            })
        overlap_start = max(starts) if starts else None
        overlap_end = min(ends) if ends else None
        if overlap_start is not None and overlap_end is not None:
            if overlap_start >= overlap_end:
                overlap_start = overlap_end = None
        return {
            "ready": ready,
            "scanning": bool(channel["running"]),
            "products": products,
            "detected_global_start": _iso_or_none(min(starts) if starts else None),
            "detected_global_end": _iso_or_none(max(ends) if ends else None),
            "common_overlap_start": _iso_or_none(overlap_start),
            "common_overlap_end": _iso_or_none(overlap_end),
            "selected_start": _iso_or_none(self._camera_selected_start),
            "selected_end": _iso_or_none(self._camera_selected_end),
        }

    # ---------------------------------------------------------------- hybrid
    def hybrid_state(self) -> dict[str, object]:
        """Whether this flight can be split, and what it would be split into.

        A worker machine reports the package it is running under instead, so the
        interface can show that its configuration is fixed.
        """
        with self._lock:
            project = self._flight_project
            time_state = self._time_state
            processable = [
                instrument_id
                for instrument_id in INSTRUMENT_ORDER
                if instrument_id in self._instruments
                and _instrument_is_processable(self._instruments[instrument_id])
            ]
            worker = self._work_package
            # Sealed into every package, so it has to survive JSON round-trip.
            options = {
                key: json.loads(json.dumps(dict(value), default=str))
                for key, value in (
                    ("sif", self._sif_options), ("flir", self._flir_level2_options)
                )
            }
        missing: list[str] = []
        if project is None:
            missing.append("Scan a Flight Folder")
        if not processable:
            missing.append("No instrument is ready to process")
        if time_state.selected_analysis_start is None or (
            time_state.selected_analysis_end is None
        ):
            missing.append("Set the processing time range")
        if self._selected_output_folder is None:
            missing.append("Select an Output Folder")
        return {
            "available": not missing and worker is None,
            "blocked_reasons": missing,
            "is_worker": worker is not None,
            "worker": None if worker is None else {
                "worker_name": worker.worker_name,
                "worker_id": worker.worker_id,
                "flight_id": worker.flight_id,
                "campaign": worker.payload.get("campaign", ""),
                "project_id": worker.project_id,
                "assigned_instruments": list(worker.assigned_instruments),
                "analysis_start": worker.payload.get("analysis_start"),
                "analysis_end": worker.payload.get("analysis_end"),
                "package_file": str(worker.path),
                "created_utc": worker.header.get("created_utc"),
                "software_version": worker.header.get("software_version"),
            },
            "project_id": None if project is None else str(project.project_id),
            "flight_id": None if project is None else project.flight_id,
            "campaign": CAMPAIGN_NAME,
            "analysis_start": _iso_or_none(time_state.selected_analysis_start),
            "analysis_end": _iso_or_none(time_state.selected_analysis_end),
            "instruments": [
                {
                    "instrument_id": instrument_id,
                    "display_name": self._instruments[instrument_id].display_name,
                }
                for instrument_id in processable
            ],
            "instrument_options": options,
            "maximum_packages": MAXIMUM_FUSION_PACKAGES,
        }

    def create_hybrid_packages(self, request: dict[str, object]) -> dict[str, object]:
        """Write one sealed work package per worker."""
        state = self.hybrid_state()
        if state["is_worker"]:
            raise ValueError(
                "This computer is running a work package and cannot hand out "
                "further ones."
            )
        if not state["available"]:
            raise ValueError(
                "Hybrid processing is not ready: "
                + "; ".join(state["blocked_reasons"])
            )
        passphrase = str(request.get("passphrase", ""))
        if len(passphrase.strip()) < 8:
            raise ValueError(
                "Use a passphrase of at least 8 characters. Every worker needs "
                "it to open their package."
            )
        raw_workers = request.get("workers")
        if not isinstance(raw_workers, list) or not raw_workers:
            raise ValueError("Define at least one worker package")
        available = [item["instrument_id"] for item in state["instruments"]]
        assignments = []
        for entry in raw_workers:
            if not isinstance(entry, dict):
                raise ValueError("Each worker package must be an object")
            instruments = entry.get("instruments") or []
            if not isinstance(instruments, list):
                raise ValueError("A worker package's instruments must be a list")
            assignments.append(WorkerAssignment(
                worker_id=str(entry.get("worker_id") or uuid4()),
                worker_name=str(entry.get("worker_name", "")).strip(),
                instruments=tuple(str(value) for value in instruments),
            ))
        primary = tuple(
            str(value) for value in (request.get("primary_instruments") or [])
        )
        plan = HybridPlan(
            project_id=str(state["project_id"]),
            flight_id=str(state["flight_id"]),
            campaign=str(state["campaign"]),
            analysis_start=state["analysis_start"],
            analysis_end=state["analysis_end"],
            available_instruments=tuple(available),
            assignments=tuple(assignments),
            primary_instruments=primary,
            instrument_options=state["instrument_options"],
        )
        destination = Path(
            str(request.get("destination") or self._selected_output_folder)
        )
        written = create_work_packages(
            plan, destination, passphrase, software_version=SOFTWARE_VERSION
        )
        for path, assignment in zip(written, plan.assignments):
            self.logger.log(
                LogLevel.SUCCESS, "hybrid",
                f"Work package for {assignment.worker_name} covering "
                + ", ".join(assignment.instruments),
                file_path=path, processing_step="hybrid-plan",
            )
        if plan.unassigned:
            self.logger.log(
                LogLevel.WARNING, "hybrid",
                "No computer was given: " + ", ".join(plan.unassigned)
                + ". Those instruments will not be processed by anyone.",
                processing_step="hybrid-plan",
            )
        self._checkpoint_project()
        return {
            "created": [str(path) for path in written],
            "unassigned": list(plan.unassigned),
            "primary_instruments": list(primary),
        }

    def load_work_package(self, path: Path, passphrase: str) -> dict[str, object]:
        """Adopt a work package. Everything scientific becomes read-only."""
        package = load_work_package_file(Path(path), passphrase)
        with self._lock:
            self._work_package = package
            for key, value in package.payload.get("instrument_options", {}).items():
                if key == "sif":
                    self._sif_options = {**DEFAULT_SIF_OPTIONS, **value}
                elif key == "flir":
                    self._flir_level2_options = {
                        **DEFAULT_FLIR_LEVEL2_OPTIONS, **value
                    }
        start = package.payload.get("analysis_start")
        end = package.payload.get("analysis_end")
        if start and end:
            self.update_time_filter({
                "action": "set", "start": start, "end": end,
                "_from_work_package": True,
            })
        self.logger.log(
            LogLevel.SUCCESS, "hybrid",
            f"Work package adopted: {package.worker_name} processing "
            + ", ".join(package.assigned_instruments)
            + f" for {package.flight_id}. The scientific configuration is fixed.",
            file_path=Path(path), processing_step="hybrid-worker",
        )
        return self.hybrid_state()

    def export_hybrid_results(self, request: dict[str, object]) -> dict[str, object]:
        """Seal this worker's processed project for return to the primary."""
        with self._lock:
            package = self._work_package
            project = self._flight_project
            processed = [
                instrument_id for instrument_id, state in self._instruments.items()
                if state.processing_status in {
                    ProcessingStatus.COMPLETE, ProcessingStatus.WARNING
                }
            ]
        if package is None:
            raise ValueError(
                "This computer is not running a work package, so there is "
                "nothing to hand back."
            )
        if project is None:
            raise ValueError("Scan and process the assigned instruments first")
        passphrase = str(request.get("passphrase", ""))
        authorised = [
            instrument_id for instrument_id in processed
            if package.authorises(instrument_id)
        ]
        if not authorised:
            raise ValueError(
                "None of the assigned instruments has been processed yet: "
                + ", ".join(package.assigned_instruments)
            )
        project_file = self.save_project()
        destination = Path(
            str(request.get("destination") or self._selected_output_folder)
        )
        exported = export_result_package(
            package, project_file, destination, passphrase,
            software_version=SOFTWARE_VERSION,
            processed_instruments=authorised,
            log_records=self.visible_logs(),
        )
        self.logger.log(
            LogLevel.SUCCESS, "hybrid",
            f"Results sealed for {package.worker_name}: " + ", ".join(authorised),
            file_path=exported, processing_step="hybrid-worker",
        )
        return {
            "package": str(exported),
            "processed_instruments": authorised,
            "skipped": sorted(set(processed) - set(authorised)),
        }

    def review_hybrid_fusion(self, request: dict[str, object]) -> dict[str, object]:
        """Check result packages belong together, without merging anything."""
        packages = self._load_result_packages(request)
        report = review_fusion(packages)
        return {
            "ok": report.ok,
            "reasons": list(report.reasons),
            "packages": [dict(item) for item in report.packages],
            "instruments": list(report.instruments),
            "project_id": report.project_id,
            "flight_id": report.flight_id,
        }

    def fuse_hybrid_results(self, request: dict[str, object]) -> dict[str, object]:
        packages = self._load_result_packages(request)
        destination = Path(
            str(request.get("destination") or self._selected_output_folder or ".")
        )
        flight = packages[0].payload.get("flight_id", "fused")
        manifest, report = fuse(
            packages, destination / f"{flight}_fused",
            software_version=SOFTWARE_VERSION,
        )
        self.logger.log(
            LogLevel.SUCCESS, "hybrid",
            f"Fused {len(packages)} result package(s) covering "
            + ", ".join(report.instruments),
            file_path=manifest, processing_step="hybrid-fusion",
        )
        return {
            "manifest": str(manifest),
            "folder": str(manifest.parent),
            "instruments": list(report.instruments),
            "packages": [dict(item) for item in report.packages],
        }

    def _load_result_packages(self, request: dict[str, object]) -> list:
        raw = request.get("packages")
        if not isinstance(raw, list) or not raw:
            raise ValueError("Select the result packages to fuse")
        passphrase = str(request.get("passphrase", ""))
        loaded = []
        for value in raw:
            path = Path(str(value))
            if not path.is_file():
                raise ValueError(f"No such result package: {path}")
            loaded.append(load_result_package(path, passphrase))
        return loaded

    def camera_coverage(self) -> dict[str, object]:
        with self._lock:
            return self._camera_coverage_locked()

    REMOTE_SENSING_TIME_MODES = ("global", "overlap", "custom", "current")

    def _select_remote_sensing_interval(
        self, time_mode: str, request: dict[str, object]
    ) -> None:
        """Set the camera analysis interval from the requested mode."""
        if time_mode not in self.REMOTE_SENSING_TIME_MODES:
            raise ValueError(
                "Remote-sensing time mode must be one of: "
                + ", ".join(self.REMOTE_SENSING_TIME_MODES)
            )
        with self._lock:
            coverage = self._camera_coverage_locked()
            if time_mode == "current":
                if self._camera_selected_start is not None:
                    return
                time_mode = "global"      # nothing chosen yet; use everything
            if time_mode == "global":
                start = coverage["detected_global_start"]
                end = coverage["detected_global_end"]
                label = "detected global minimum and maximum"
            elif time_mode == "overlap":
                start = coverage["common_overlap_start"]
                end = coverage["common_overlap_end"]
                label = "common overlap"
            else:
                start, end = request.get("start"), request.get("end")
                label = "custom interval"
            if start is None or end is None:
                raise ValueError(
                    f"No {label} is available for the selected remote-sensing "
                    "products. Choose a different interval."
                )
            repair = repair_interval(
                parse_dashboard_datetime(start),
                parse_dashboard_datetime(end),
                available_start=_optional_dashboard_datetime(
                    coverage["detected_global_start"]
                ),
                available_end=_optional_dashboard_datetime(
                    coverage["detected_global_end"]
                ),
            )
            if not repair.usable:
                raise ValueError(
                    "A valid remote-sensing interval could not be derived. "
                    + " ".join(repair.warnings)
                )
            self._camera_selected_start = repair.start
            self._camera_selected_end = repair.end
        for warning in repair.warnings:
            self.logger.log(
                LogLevel.WARNING, "remote-sensing", warning,
                processing_step="time-selection",
            )
        self.logger.log(
            LogLevel.INFO, "remote-sensing",
            f"Remote-sensing interval set from the {label}: "
            f"{repair.start.isoformat()} to {repair.end.isoformat()}",
            processing_step="time-selection",
        )

    def preview_remote_sensing(
        self, request: dict[str, object] | None = None
    ) -> dict[str, object]:
        """What a request would do, without starting anything.

        The dialog shows this back to the operator for confirmation, so the
        interval and the product list are settled before any work begins.
        """
        request = request or {}
        with self._lock:
            coverage = self._camera_coverage_locked()
        if not coverage["ready"]:
            raise ValueError(
                "Camera scanning must finish before remote-sensing products "
                "can be selected."
            )
        requested = request.get("instruments")
        selectable = {
            item["instrument_id"] for item in coverage["products"] if item["selectable"]
        }
        if requested is None:
            wanted = set(selectable)
        else:
            if not isinstance(requested, list):
                raise ValueError("instruments must be a list of instrument IDs")
            wanted = {str(value).strip() for value in requested if str(value).strip()}
        if not wanted:
            raise ValueError("Select at least one remote-sensing product")
        unusable = sorted(wanted - selectable)
        wanted &= selectable
        if not wanted:
            raise ValueError(
                "None of the selected products has usable UTC coverage: "
                + ", ".join(unusable)
            )
        mode = str(request.get("time_mode", "global"))
        if mode == "custom":
            start, end = request.get("start"), request.get("end")
        elif mode == "overlap":
            start = coverage["common_overlap_start"]
            end = coverage["common_overlap_end"]
        else:
            start = coverage["detected_global_start"]
            end = coverage["detected_global_end"]
        if start is None or end is None:
            raise ValueError(
                "No interval is available for that choice. Pick another option."
            )
        parsed_start = parse_dashboard_datetime(start)
        parsed_end = parse_dashboard_datetime(end)
        repair = repair_interval(
            parsed_start, parsed_end,
            available_start=_optional_dashboard_datetime(
                coverage["detected_global_start"]
            ),
            available_end=_optional_dashboard_datetime(
                coverage["detected_global_end"]
            ),
        )
        if not repair.usable:
            raise ValueError(
                "A valid interval could not be derived. " + " ".join(repair.warnings)
            )
        products = []
        for item in coverage["products"]:
            if item["instrument_id"] not in wanted:
                continue
            covered = (
                parse_dashboard_datetime(item["utc_start"]) < repair.end
                and parse_dashboard_datetime(item["utc_end"]) > repair.start
            )
            products.append({
                **item,
                "covers_interval": covered,
            })
        return {
            "time_mode": mode,
            "start": repair.start.isoformat(),
            "end": repair.end.isoformat(),
            "duration_seconds": (repair.end - repair.start).total_seconds(),
            "warnings": list(repair.warnings),
            "products": products,
            "ignored": unusable,
            "ready_to_start": bool(
                products and any(item["covers_interval"] for item in products)
            ),
        }

    def visible_logs(self) -> list[dict[str, object]]:
        return [record.to_dict() for record in self.logger.records()]

    def clear_visible_logs(self) -> None:
        self.logger.clear_visible()

    def save_project(self) -> Path:
        with self._lock:
            if self._flight_project is None or self._selected_folder is None:
                raise ValueError("Select and scan a Flight Folder before saving")
            if self._selected_output_folder is None:
                raise ValueError("Select an Output Folder before saving")
            self._flight_project.output_folder_path = self._selected_output_folder
            self._sync_project_queue_state()
            self._sync_project_time_state()
            self._flight_project.raw_file_fingerprints = (
                self._project_store.capture_raw_file_fingerprints(
                    self._flight_project
                )
            )
            self._persist_project_logs()
            project_file = self._project_store.save_project(
                self._flight_project, overwrite=True
            )
        # Say what the file carries. The Output Folder holds a working tree
        # beside it, and an operator archiving only the .ccflux needs to know
        # whether anything was left behind rather than assuming either way.
        carried, left = self._project_contents_report(project_file)
        self.logger.log(
            LogLevel.SUCCESS,
            "project",
            f"Flight Project saved with {carried} product(s) inside it"
            + (
                f"; {len(left)} file(s) stayed in the Output Folder: "
                + ", ".join(left[:5])
                + (" ..." if len(left) > 5 else "")
                if left
                else ". Everything in the Output Folder is inside this file."
            ),
            file_path=project_file,
            processing_step="manual-save",
        )
        self._persist_project_logs()
        return project_file

    def _project_contents_report(self, project_file: Path) -> tuple[int, list[str]]:
        """How many products the file carries, and what it does not."""
        import zipfile

        project = self._flight_project
        if project is None or not zipfile.is_zipfile(project_file):
            return 0, []
        try:
            with zipfile.ZipFile(project_file) as archive:
                bundled = {
                    name[len("products/"):]
                    for name in archive.namelist()
                    if name.startswith("products/")
                }
        except (OSError, zipfile.BadZipFile):
            return 0, []
        root = project.flight_output_root
        if not root.is_dir():
            return len(bundled), []
        left = sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and str(path.relative_to(root)) not in bundled
        )
        return len(bundled), left

    def select_project_folder(self) -> dict[str, object]:
        chooser = getattr(self.folder_dialog, "choose_project_folder", None)
        if not callable(chooser):
            raise RuntimeError("Saved-project folder selection is not available")
        folder = self._choose_folder_once("project-folder", chooser)
        if folder is None:
            self.logger.log(
                LogLevel.INFO, "project", "Saved-project folder selection cancelled"
            )
            return {"cancelled": True}
        return self.discover_saved_projects(Path(folder))

    def discover_saved_projects(self, folder: Path) -> dict[str, object]:
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Saved-project search folder does not exist: {root}")
        ignored = {
            ".git", ".pytest_cache", "__pycache__", "backups", "verification",
            "node_modules",
        }
        project_files: list[Path] = []
        inaccessible: list[str] = []

        def record_walk_error(error: OSError) -> None:
            inaccessible.append(str(getattr(error, "filename", "") or error))

        root_depth = len(root.parts)
        for current, directories, filenames in os.walk(
            root, topdown=True, onerror=record_walk_error, followlinks=False
        ):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            directories[:] = [
                name for name in directories
                if name.casefold() not in ignored
                and not name.startswith(".")
                and depth < 8
            ]
            # Projects are named after their flight, so discovery has to match
            # the suffix rather than one fixed name. The legacy JSON name is
            # still accepted, but only when no .ccflux sits beside it — that
            # pair is one project written twice, not two projects.
            found = sorted(
                name for name in filenames
                if name.casefold().endswith(PROJECT_SUFFIX)
                and not name.startswith(".")
            )
            if not found and LEGACY_PROJECT_FILENAME in filenames:
                found = [LEGACY_PROJECT_FILENAME]
            for name in found:
                project_files.append(current_path / name)
            if len(project_files) >= 500:
                del project_files[500:]
                break

        projects: list[dict[str, object]] = []
        invalid = 0
        for project_file in project_files:
            try:
                if project_file.stat().st_size > PROJECT_DISCOVERY_BYTE_LIMIT:
                    raise ValueError("project file is larger than the discovery limit")
                # Compressed projects are archives, not text. Reading the
                # manifest through the store keeps discovery working for both
                # the compressed form and older plain-JSON projects.
                payload = self._project_store.read_manifest(project_file)
                flight_id = str(payload.get("flight_id") or "").strip()
                if not flight_id:
                    raise ValueError("flight_id is missing")
                projects.append(
                    {
                        "flight_id": flight_id,
                        "project_file": str(project_file.resolve()),
                        "relative_path": str(project_file.relative_to(root)),
                        "updated_at_utc": payload.get("updated_at_utc"),
                        "created_at_utc": payload.get("created_at_utc"),
                        "flight_folder_path": payload.get("flight_folder_path"),
                        "output_folder_path": payload.get("output_folder_path"),
                    }
                )
            except (
                OSError,
                UnicodeError,
                ValueError,
                KeyError,
                zipfile.BadZipFile,
                json.JSONDecodeError,
            ) as exc:
                invalid += 1
                self.logger.log(
                    LogLevel.WARNING,
                    "project",
                    f"Ignoring invalid saved-project candidate: {exc}",
                    file_path=project_file,
                    processing_step="project-discovery",
                )
        projects.sort(
            key=lambda item: (
                str(item.get("updated_at_utc") or ""),
                str(item.get("flight_id") or "").casefold(),
            ),
            reverse=True,
        )
        self.logger.log(
            LogLevel.INFO,
            "project",
            (
                f"Saved-project search found {len(projects)} valid project(s) "
                f"below {root}"
            ),
            file_path=root,
            processing_step="project-discovery",
        )
        return {
            "cancelled": False,
            "folder": str(root),
            "projects": projects,
            "valid_count": len(projects),
            "invalid_count": invalid,
            "inaccessible_count": len(inaccessible),
            "truncated": len(project_files) >= 500,
        }

    def open_project(self, project_file: Path | None = None) -> dict[str, object]:
        if project_file is None:
            self.logger.log(
                LogLevel.INFO, "project", "Opening Flight Project file chooser"
            )
            chooser = getattr(self.folder_dialog, "choose_project_file", None)
            project_file = chooser() if callable(chooser) else None
        if project_file is None:
            self.logger.log(LogLevel.INFO, "project", "Open Project cancelled")
            return {"cancelled": True}
        opened = self._project_store.open_project(Path(project_file))
        project = opened.project
        restored_queue = create_default_priority_queue()
        saved_priority = list(project.processing_priority)
        if (
            saved_priority
            and len(saved_priority) == len(set(saved_priority))
            and set(saved_priority)
            == {job.job_id for job in restored_queue.ordered()}
        ):
            restored_queue.reorder(saved_priority)
        completed_instruments: set[str] = set()
        for snapshot in restored_queue.ordered():
            job = restored_queue.get(snapshot.job_id)
            completed = (
                job.job_id in project.completed_jobs
                or job.instrument_id in project.completed_jobs
            )
            if completed:
                job.status = ProcessingStatus.COMPLETE
                job.enabled = False
                job.progress = 100.0
                job.current_step = "Previously processed — skipped by default"
                completed_instruments.add(job.instrument_id)
            elif job.job_id in project.failed_jobs:
                job.status = ProcessingStatus.FAILED
                job.enabled = False
                job.current_step = "Previous processing failed"
            elif job.job_id in project.cancelled_jobs:
                job.status = ProcessingStatus.CANCELLED
                job.enabled = False
                job.current_step = "Previous processing was cancelled"
            elif (
                not job.detailed
                and job.instrument_id in project.enabled_instruments
            ):
                restored_queue.set_enabled(job.job_id, True)
        instruments = self._new_instrument_states()
        for instrument_id, saved in project.detected_instruments.items():
            state = instruments[instrument_id]
            state.detection_status = DetectionStatus.READY
            state.file_count = len(saved.selected_source_files)
            state.confidence = saved.detection_confidence
            restored_candidates = (
                saved.selected_source_folders or saved.selected_source_files
            )
            state.candidate_paths = [str(value) for value in restored_candidates]
            state.ambiguous = bool(saved.ambiguous_candidates)
            state.timestamp_warnings = list(saved.timestamp_warnings)
            state.utc_start_time = saved.utc_start_time
            state.utc_end_time = saved.utc_end_time
            state.processing_status = (
                "complete" if instrument_id in completed_instruments else "idle"
            )
            if instrument_id in completed_instruments:
                state.processing_progress = 100.0
                state.processing_step = "Previously processed — skipped by default"
            state.output_files = [str(value) for value in saved.output_locations]
        quicklook_file = project.output_locations.get("noseboom_quicklook")
        if quicklook_file and Path(quicklook_file).is_file():
            try:
                instruments["noseboom"].quicklook = json.loads(
                    Path(quicklook_file).read_text(encoding="utf-8")
                )
                instruments["noseboom"].processing_status = "complete"
                self._noseboom_straight_settings = dict(
                    instruments["noseboom"].quicklook.get("straight_settings", {})
                )
            except (OSError, json.JSONDecodeError) as exc:
                instruments["noseboom"].warnings.append(
                    f"Saved Noseboom browser state could not be loaded: {exc}"
                )
        gopro_quicklook = project.output_locations.get("gopro_quicklook")
        if gopro_quicklook and Path(gopro_quicklook).is_file():
            try:
                instruments["gopro"].quicklook = json.loads(
                    Path(gopro_quicklook).read_text(encoding="utf-8")
                )
                instruments["gopro"].processing_status = "complete"
            except (OSError, json.JSONDecodeError) as exc:
                instruments["gopro"].warnings.append(
                    f"Saved GoPro browser state could not be loaded: {exc}"
                )
        flir_browser = project.output_locations.get("flir_browser")
        if flir_browser and Path(flir_browser).is_file():
            try:
                instruments["flir"].quicklook = json.loads(
                    Path(flir_browser).read_text(encoding="utf-8")
                )
                instruments["flir"].processing_status = "complete"
            except (OSError, json.JSONDecodeError) as exc:
                instruments["flir"].warnings.append(
                    f"Saved FLIR browser state could not be loaded: {exc}"
                )
        sif_browser = project.output_locations.get("sif_browser")
        if sif_browser and Path(sif_browser).is_file():
            try:
                instruments["sif"].quicklook = json.loads(
                    Path(sif_browser).read_text(encoding="utf-8")
                )
                instruments["sif"].processing_status = "complete"
            except (OSError, json.JSONDecodeError) as exc:
                instruments["sif"].warnings.append(
                    f"Saved SIF browser state could not be loaded: {exc}"
                )
        ranges = {
            instrument_id: (
                saved.utc_start_time,
                saved.utc_end_time,
                saved.timestamp_warnings,
            )
            for instrument_id, saved in project.detected_instruments.items()
        }
        time_state = DashboardTimeState.from_instrument_ranges(
            ranges, analysis_anchor_id="noseboom"
        )
        time_state.display_timezone = project.display_timezone
        if project.selected_analysis_start and project.selected_analysis_end:
            time_state.selected_analysis_start = project.selected_analysis_start
            time_state.selected_analysis_end = project.selected_analysis_end
        restored_report = None
        if opened.reused_saved_scan:
            restored_candidates: list[InstrumentCandidate] = []
            for instrument_id, saved in project.detected_instruments.items():
                files = tuple(Path(value) for value in saved.selected_source_files)
                folders = tuple(Path(value) for value in saved.selected_source_folders)
                candidate_path = (
                    folders[0] if folders else
                    files[0].parent if files else
                    project.flight_folder_path
                )
                restored_candidates.append(
                    InstrumentCandidate(
                        instrument_id=instrument_id,
                        candidate_path=candidate_path,
                        matched_rules=("restored_project",),
                        confidence_score=saved.detection_confidence or 1.0,
                        matching_file_count=len(files),
                        sample_matching_files=files[:20],
                        warnings=tuple(saved.timestamp_warnings),
                        errors=(),
                        ambiguous=bool(saved.ambiguous_candidates),
                        matching_files=files,
                    )
                )
            restored_report = ScanReport(
                root=project.flight_folder_path,
                candidates=tuple(restored_candidates),
                files_scanned=sum(
                    len(saved.selected_source_files)
                    for saved in project.detected_instruments.values()
                ),
                folders_scanned=0,
                inaccessible_path_count=0,
                malformed_file_count=0,
                warnings=(),
                errors=(),
                cancelled=False,
            )
        with self._lock:
            self._flight_project = project
            self._sif_options = {
                **DEFAULT_SIF_OPTIONS,
                **project.instrument_options.get("sif", {}),
            }
            self._flir_level2_options = {
                **DEFAULT_FLIR_LEVEL2_OPTIONS,
                **project.instrument_options.get("flir_level2", {}),
            }
            self.processing_queue = restored_queue
            self._scheduler = None
            self._selected_folder = project.flight_folder_path
            self._selected_camera_folder = project.camera_folder_path
            self._selected_output_folder = project.output_folder_path
            self._instruments = instruments
            self._detected = tuple(project.detected_instruments)
            self._time_state = time_state
            self._phase = "complete" if opened.reused_saved_scan else "project_loaded"
            self._report = restored_report
            self._files_scanned = restored_report.files_scanned if restored_report else 0
            self._messages = [
                "Saved Flight Project loaded.",
                *(
                    [] if opened.reused_saved_scan else
                    ["Raw files changed or are missing; rescan before new processing."]
                ),
            ]
            self._scan_channels = {
                "flight": self._new_scan_channel("flight", project.flight_folder_path),
                "camera": self._new_scan_channel("camera", project.camera_folder_path),
            }
            self._scan_channels["flight"]["phase"] = "complete"
            self._scan_channels["camera"]["phase"] = (
                "complete" if project.camera_folder_path else "not_selected"
            )
        self.logger.log(
            LogLevel.SUCCESS,
            "project",
            "Saved Flight Project loaded",
            file_path=Path(project_file),
            processing_step="project-open",
        )
        return {
            "cancelled": False,
            "project_file": str(project_file),
            "reused_saved_scan": opened.reused_saved_scan,
            "rescan_required": opened.rescan_required,
            "state": self.snapshot(),
        }

    def noseboom_view(self) -> dict[str, object]:
        with self._lock:
            state = self._instruments["noseboom"]
            project = self._flight_project
            return {
                "ready": bool(state.quicklook.get("available")),
                "flight_id": project.flight_id if project else None,
                "project_file": str(project.project_file) if project else None,
                "data": dict(state.quicklook),
                "exports": list(state.output_files),
                "processing_status": state.processing_status,
                "processing_step": state.processing_step,
                "statistics_export": self._noseboom_statistics_export.snapshot(),
                "statistics_exports": [
                    {
                        "name": Path(value).name,
                        "url": (
                            "/api/noseboom/statistics/export/download/"
                            + Path(value).name
                        ),
                    }
                    for value in state.output_files
                    if Path(value).suffix.casefold() in {".pdf", ".svg", ".png"}
                ],
            }

    def gopro_view(self) -> dict[str, object]:
        """Return the time-corrected GoPro capture map for the active project."""
        with self._lock:
            state = self._instruments["gopro"]
            project = self._flight_project
            payload = dict(state.quicklook)
            processing_status = state.processing_status
            processing_step = state.processing_step
            noseboom_points = tuple(
                self._instruments["noseboom"].quicklook.get("points", ())
            )
        if (
            not payload.get("available")
            and payload.get("inventory")
            and noseboom_points
        ):
            captures = georeference_captures(payload["inventory"], noseboom_points)
            if captures:
                payload["captures"] = captures
                payload["available"] = True
                payload["reason"] = None
                payload["matched_count"] = len(captures)
                payload["unmatched_count"] = max(
                    0, int(payload.get("image_count", 0)) - len(captures)
                )
                with self._lock:
                    state.quicklook = payload
                    state.processing_status = "complete"
                    state.processing_progress = 100.0
                    state.processing_step = (
                        f"GoPro map ready with {len(captures)} capture locations"
                    )
                    stale_warning = (
                        "No GoPro image timestamp could be matched to processed "
                        "Noseboom navigation within 2.5 seconds."
                    )
                    state.warnings = [
                        warning for warning in state.warnings
                        if warning != stale_warning
                    ]
                    job = self.processing_queue.get("gopro_quick")
                    if job.status is ProcessingStatus.WARNING:
                        job.status = ProcessingStatus.COMPLETE
                        job.progress = 100.0
                        job.current_step = state.processing_step
                        job.error = None
                    self._sync_project_queue_state()
                if project is not None:
                    quicklook_path = project.output_locations.get("gopro_quicklook")
                    if quicklook_path:
                        Path(quicklook_path).write_text(
                            json.dumps(
                                _gopro_project_payload(payload),
                                ensure_ascii=False, indent=2, allow_nan=False,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
        captures = [
            public_capture(item)
            for item in payload.get("captures", [])
            if isinstance(item, dict)
        ]
        return {
            "ready": bool(payload.get("available")),
            "flight_id": project.flight_id if project else None,
            "project_file": str(project.project_file) if project else None,
            "processing_status": processing_status,
            "processing_step": processing_step,
            "data": {
                key: value for key, value in payload.items()
                if key not in {"captures", "inventory"}
            } | {"captures": captures},
        }

    def gopro_media_root(self) -> Path | None:
        """The folder currently holding the GoPro media, if one is reachable."""
        with self._lock:
            reconnected = self._gopro_media_root
            project = self._flight_project
        if reconnected is not None and reconnected.is_dir():
            return reconnected
        if project is None or project.camera_folder_path is None:
            return None
        camera_root = Path(project.camera_folder_path)
        return camera_root if camera_root.is_dir() else None

    def _gopro_source_index(self, root: Path) -> dict[str, Path]:
        """Map each image file name below ``root`` to its location, once.

        A saved project stores image identity only — never a disk path — so a
        project opened on another machine, or after the drive was remounted
        somewhere else, has to find the media by name. Building the index once
        keeps that off the per-image request path.
        """
        resolved = root.resolve()
        with self._lock:
            cached = self._gopro_index_cache
            if cached is not None and cached[0] == resolved:
                return cached[1]
        index: dict[str, Path] = {}
        for path in resolved.rglob("*"):
            if path.is_file() and path.suffix.casefold() in GOPRO_IMAGE_SUFFIXES:
                index.setdefault(path.name.casefold(), path)
        with self._lock:
            self._gopro_index_cache = (resolved, index)
        return index

    def gopro_image_file(self, capture_id: str) -> Path:
        """Resolve a capture image by identity, below the reachable media root."""
        with self._lock:
            captures = tuple(self._instruments["gopro"].quicklook.get("captures", ()))
        capture = next(
            (
                item for item in captures
                if isinstance(item, dict)
                and str(item.get("capture_id")) == str(capture_id)
            ),
            None,
        )
        if capture is None:
            raise ValueError("Unknown GoPro capture")
        root = self.gopro_media_root()
        if root is None:
            raise ValueError(GOPRO_MEDIA_UNAVAILABLE)
        name = str(capture.get("file_name") or "")
        if not name:
            source = str(capture.get("source_file") or "")
            name = Path(source).name if source else ""
        path = self._gopro_source_index(root).get(name.casefold())
        if (
            not name
            or path is None
            or not path.is_relative_to(root.resolve())
            or path.suffix.casefold() not in GOPRO_IMAGE_SUFFIXES
            or not path.is_file()
        ):
            raise ValueError(GOPRO_MEDIA_UNAVAILABLE)
        return path

    def flir_view(self) -> dict[str, object]:
        """Return saved FLIR acquisition and temperature-map products."""
        with self._lock:
            state = self._instruments["flir"]
            project = self._flight_project
            payload = dict(state.quicklook)
            coverage = self._time_state.instruments.get("flir")
            selected_start = self._time_state.selected_analysis_start
            selected_end = self._time_state.selected_analysis_end
            if payload.get("available"):
                message = (
                    "FLIR acquisition plots, gap diagnostics, and thermal "
                    "gallery are ready. Configure FLIR Level 2 in the Main GUI "
                    "to create temperature plots and the Noseboom-matched map."
                    if not payload.get("temperature_available")
                    else
                    "FLIR acquisition plots, radiometric temperature plots, "
                    "thermal gallery, and Noseboom-matched map are ready."
                )
            elif coverage is not None and coverage.outside_selected_range:
                message = (
                    "FLIR UTC data "
                    f"{_iso(coverage.raw_start)} — {_iso(coverage.raw_end)} "
                    "do not overlap the selected Noseboom flight interval "
                    f"{_iso(selected_start)} — {_iso(selected_end)}. "
                    "Select the FLIR export from this flight."
                )
            elif _instrument_is_processable(state):
                message = (
                    "FLIR scan is ready. Select FLIR metadata quick check "
                    "and Start Processing in the Main GUI."
                )
            else:
                message = "Complete FLIR Initial Check in the Main GUI first."
            return {
                "ready": bool(payload.get("available")),
                "temperature_ready": bool(
                    payload.get("temperature_available")
                ),
                "flight_id": project.flight_id if project else None,
                "project_file": str(project.project_file) if project else None,
                "processing_status": state.processing_status,
                "processing_step": state.processing_step,
                "message": message,
                "data": payload,
            }

    def flir_asset_file(self, name: str) -> Path:
        """Resolve a generated FLIR browser asset below the active output root."""
        if not name or Path(name).name != name:
            raise ValueError("Invalid FLIR asset name")
        with self._lock:
            state = self._instruments["flir"]
            project = self._flight_project
            files = tuple(Path(value) for value in state.output_files)
        if project is None:
            raise ValueError("No active Flight Project")
        output_root = project.flight_output_root.resolve()
        match = next(
            (
                path.resolve()
                for path in files
                if path.name == name and path.is_file()
            ),
            None,
        )
        if (
            match is None
            or not match.is_relative_to(output_root)
            or match.suffix.casefold() not in {".png", ".csv", ".json"}
        ):
            raise ValueError("FLIR asset is unavailable")
        return match

    def log_flir_view_event(self, message: str) -> None:
        self.logger.log(
            LogLevel.INFO,
            "flir-view",
            message or "FLIR browser interaction",
            instrument="flir",
            processing_step="browser-interaction",
        )
        self._persist_project_logs()

    def gopro_media_status(self) -> dict[str, object]:
        """Whether the GoPro media is reachable, for the image viewer."""
        with self._lock:
            captures = tuple(self._instruments["gopro"].quicklook.get("captures", ()))
        root = self.gopro_media_root()
        available = root is not None and bool(
            self._gopro_source_index(root)
        ) if root is not None else False
        return {
            "media_available": bool(available),
            "media_root": str(root) if root else None,
            "capture_count": len(captures),
            "prompt": None if available else GOPRO_RECONNECT_PROMPT,
            "folder_requirement": GOPRO_FOLDER_NAME,
            "contact_message": GOPRO_NO_DISK_MESSAGE,
        }

    def reconnect_gopro_media(
        self, request: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        """Re-link saved captures to GoPro media on a reattached disk.

        A saved project carries image identity but no image data, so opening it
        elsewhere leaves the map working and the pictures missing. The operator
        is asked whether the drive is available; if it is, they point at it and
        the captures are matched back to files by name.
        """
        request = request or {}
        has_disk = request.get("has_hard_disk")
        if has_disk is None:
            raise ValueError("Answer whether the GoPro hard disk is available")
        if has_disk is not True:
            self.logger.log(
                LogLevel.WARNING,
                "gopro-media",
                "Operator reported the GoPro hard disk is not available",
                instrument="gopro",
                processing_step="media-reconnect",
            )
            return {
                "reconnected": False,
                "media_available": False,
                "message": GOPRO_NO_DISK_MESSAGE,
            }

        directory = request.get("directory")
        if not directory:
            raise ValueError(
                "Select the folder that contains the GoPro media. "
                + GOPRO_FOLDER_REQUIREMENT
            )
        root = Path(str(directory)).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Folder does not exist: {root}")
        # Accept either the GoPro folder itself or a parent holding one, so the
        # operator can point at the disk without hunting for the exact level.
        if root.name.casefold() != GOPRO_FOLDER_NAME.casefold():
            nested = [
                path for path in root.iterdir()
                if path.is_dir() and path.name.casefold() == GOPRO_FOLDER_NAME.casefold()
            ]
            if not nested:
                raise ValueError(GOPRO_FOLDER_REQUIREMENT)
            root = nested[0]

        with self._lock:
            self._gopro_index_cache = None
            captures = tuple(self._instruments["gopro"].quicklook.get("captures", ()))
        index = self._gopro_source_index(root)
        wanted = {
            str(item.get("file_name") or Path(str(item.get("source_file") or "")).name).casefold()
            for item in captures
            if isinstance(item, dict)
        }
        wanted.discard("")
        matched = sorted(name for name in wanted if name in index)
        missing = sorted(wanted - set(matched))
        if not matched:
            self._gopro_index_cache = None
            raise ValueError(
                f"No saved GoPro capture was found in {root}. Check that this is "
                "the media folder for this flight."
            )
        with self._lock:
            self._gopro_media_root = root
        self.logger.log(
            LogLevel.SUCCESS if not missing else LogLevel.WARNING,
            "gopro-media",
            (
                f"GoPro media reconnected at {root}: {len(matched)} of "
                f"{len(wanted)} saved capture(s) matched"
            ),
            instrument="gopro",
            file_path=root,
            processing_step="media-reconnect",
        )
        self._persist_project_logs()
        return {
            "reconnected": True,
            "media_available": True,
            "media_root": str(root),
            "images_found": len(index),
            "matched_captures": len(matched),
            "missing_captures": len(missing),
            "message": (
                f"Synchronised {len(matched)} of {len(wanted)} saved capture(s) "
                f"from {root}."
                + (f" {len(missing)} could not be found." if missing else "")
            ),
        }

    def log_gopro_view_event(self, message: str) -> None:
        self.logger.log(
            LogLevel.INFO,
            "gopro-view",
            message or "GoPro browser interaction",
            instrument="gopro",
            processing_step="browser-interaction",
        )
        self._persist_project_logs()

    def hatchbox_view(self, page: str) -> dict[str, object]:
        """Return a saved, precomputed Hatchbox instrument browser payload."""
        normalized = page.casefold()
        if normalized not in {"opc", "partector", "ins_gimbal", "sif"}:
            raise ValueError(f"Unknown Hatchbox page: {page}")
        key = {
            "opc": "opc_browser",
            "partector": "partector_browser",
            "ins_gimbal": "ins_gimbal_browser",
            "sif": "sif_browser",
        }[normalized]
        with self._lock:
            project = self._flight_project
            path = project.output_locations.get(key) if project else None
            instrument_ids = {
                "opc": ("opc_hbx4", "opc_hbx5"),
                "partector": ("partector",),
                "ins_gimbal": ("ins_gimbal",),
                "sif": ("sif",),
            }[normalized]
            exports = [
                value
                for instrument_id in instrument_ids
                for value in self._instruments[instrument_id].output_files
            ]
            status = {
                instrument_id: {
                    "processing_status": self._instruments[instrument_id].processing_status,
                    "processing_step": self._instruments[instrument_id].processing_step,
                    "processing_progress": self._instruments[instrument_id].processing_progress,
                }
                for instrument_id in instrument_ids
            }
            sif_state = self._instruments["sif"]
            sif_coverage = self._time_state.instruments.get("sif")
            sif_scan_ready = (
                normalized == "sif"
                and _instrument_is_processable(sif_state)
                and sif_coverage is not None
                and not sif_coverage.outside_selected_range
                and (sif_coverage.availability_percentage or 0) > 0
            )
        if path is None or not Path(path).is_file():
            return {
                "ready": False,
                "flight_id": project.flight_id if project else None,
                "message": {
                    "opc": "Process both OPC HBX-4 and HBX-5 from the Main GUI first.",
                    "partector": "Process Partector Pro from the Main GUI first.",
                    "ins_gimbal": "Process INS Gimbal from the Main GUI first.",
                    "sif": (
                        "SIF / FLOX scan is ready. Select Include and Start "
                        "Processing in the Main GUI."
                        if sif_scan_ready else
                        "Configure and process SIF / FLOX from the Main GUI first."
                    ),
                }[normalized],
                "status": status,
                "exports": exports,
                "options": dict(self._sif_options) if normalized == "sif" else None,
            }
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.capture_exception(
                "hatchbox-view", f"Saved {normalized} browser payload could not be read",
                exc, processing_step="browser-load", file_path=Path(path),
            )
            return {
                "ready": False,
                "flight_id": project.flight_id if project else None,
                "message": f"Saved {normalized} browser data is unreadable: {exc}",
                "status": status,
                "exports": exports,
            }
        return {
            "ready": bool(data.get("available")),
            "flight_id": project.flight_id if project else data.get("flight_id"),
            "project_file": str(project.project_file) if project else None,
            "data": data,
            "status": status,
            "exports": exports,
            "options": dict(self._sif_options) if normalized == "sif" else None,
        }

    def log_hatchbox_view_event(self, page: str, message: str) -> None:
        self.logger.log(
            LogLevel.INFO, "hatchbox-view", message,
            instrument=(page.casefold() if page.casefold() in {"partector", "ins_gimbal", "sif"} else None),
            processing_step="browser-interaction",
        )
        self._persist_project_logs()

    def _publish_hatchbox_browser(
        self, project: FlightProject, instrument_id: str, source: Path
    ) -> Path:
        from instruments.hatchbox_payload import write_json_atomic

        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        name = (
            "partector_browser.json"
            if instrument_id == "partector"
            else f"{instrument_id}_browser.json"
        )
        target = project.flight_output_root / "quicklooks" / name
        write_json_atomic(target, payload)
        key = "partector_browser" if instrument_id == "partector" else f"{instrument_id}_browser"
        with self._lock:
            project.output_locations[key] = target
            state = self._instruments[instrument_id]
            if str(target) not in state.output_files:
                state.output_files.append(str(target))
            saved = project.detected_instruments.get(instrument_id)
            if saved is not None and target not in saved.output_locations:
                saved.output_locations.append(target)
            if instrument_id == "partector":
                project.output_locations["partector_browser"] = target
        self.logger.log(
            LogLevel.SUCCESS, "hatchbox-view",
            f"{instrument_id} responsive browser payload saved",
            instrument=instrument_id, file_path=target,
            processing_step="browser-payload",
        )
        return target

    SIF_ESSENTIAL_KEYS = ("calibration_full", "calibration_fluo", "indices_file")

    def _validated_sif_essentials(
        self, request: dict[str, object]
    ) -> dict[str, object]:
        """Resolve operator-supplied calibration and index files.

        A missing key keeps whatever is configured; an empty string clears the
        override back to the bundled file. The file is checked here rather than
        at processing time, so a wrong pick is reported while the dialog is open
        instead of failing an hour into a run.
        """
        resolved: dict[str, object] = {}
        labels = {
            "calibration_full": "FULL calibration",
            "calibration_fluo": "FLUO calibration",
            "indices_file": "vegetation-index",
        }
        for key in self.SIF_ESSENTIAL_KEYS:
            if key not in request:
                resolved[key] = self._sif_options.get(key)
                continue
            value = request.get(key)
            if value in (None, ""):
                resolved[key] = None
                continue
            path = Path(str(value)).expanduser()
            if not path.is_file():
                raise ValueError(f"The {labels[key]} file does not exist: {path}")
            try:
                with path.open("rb") as handle:
                    handle.read(1)
            except OSError as exc:
                raise ValueError(
                    f"The {labels[key]} file cannot be read: {path}"
                ) from exc
            resolved[key] = str(path.resolve(strict=False))
        return resolved

    def select_sif_essential_file(self, kind: str) -> dict[str, object]:
        """Open the native chooser for one calibration or index file."""
        if kind not in self.SIF_ESSENTIAL_KEYS:
            raise ValueError(f"Unsupported SIF file selection: {kind}")
        chooser = getattr(self.folder_dialog, "choose_sif_essential_file", None)
        if not callable(chooser):
            raise RuntimeError("SIF file selection is not available")
        selected = self._choose_folder_once(
            "sif-options", lambda: chooser(kind)
        )
        if selected is None:
            self.logger.log(
                LogLevel.INFO, "sif-options", f"{kind} selection cancelled",
                instrument="sif",
            )
            return {"cancelled": True}
        options = self.update_sif_options({kind: str(selected)})
        return {"cancelled": False, "path": str(selected), "options": options}

    def sif_progress(self) -> dict[str, object]:
        """The SIF stage checklist, derived from the reported percentage."""
        with self._lock:
            state = self._instruments.get("sif")
            percent = float(getattr(state, "processing_progress", 0.0) or 0.0)
            step = str(getattr(state, "processing_step", "") or "")
            status = getattr(state, "processing_status", None)
            elapsed = float(getattr(state, "processing_elapsed_seconds", 0.0) or 0.0)
            options = dict(self._sif_options)
        status_value = getattr(status, "value", status)
        running = str(status_value or "").lower() == "processing"
        finished = str(status_value or "").lower() in {"complete", "warning"}
        stages = []
        for stage in SIF_PROGRESS_STAGES:
            threshold = float(stage["percent"])
            if finished or percent >= threshold:
                stage_status = "done"
            elif running and percent > 0:
                stage_status = "active"
            else:
                stage_status = "pending"
            # Only the first not-yet-reached stage is the active one.
            if stage_status == "active" and any(
                item["status"] == "active" for item in stages
            ):
                stage_status = "pending"
            stages.append({
                "key": stage["key"],
                "label": stage["label"],
                "percent": threshold,
                "status": stage_status,
            })
        return {
            "percent": round(percent, 1),
            "step": step,
            "status": status_value,
            "running": running,
            "elapsed_seconds": round(elapsed, 1),
            "stages": stages,
            "calibration": {
                key: options.get(key) for key in self.SIF_ESSENTIAL_KEYS
            },
        }

    def _refuse_if_worker(self, what: str) -> None:
        """A work package fixes the science; only folders stay local.

        Without this the read-only rule would be a label on the interface
        rather than a property of the software - a worker could change the
        settings, process, and hand back results that no longer match the plan
        every other computer used.
        """
        if self._work_package is not None:
            raise ValueError(
                f"{what} is fixed by the work package for "
                f"{self._work_package.worker_name} and cannot be changed on this "
                "computer. Only the data, camera and output folders are local."
            )

    def update_sif_options(
        self, request: Mapping[str, object] | None
    ) -> dict[str, object]:
        """Validate and persist operator-controlled SIF scientific options."""
        self._refuse_if_worker("The SIF configuration")
        request = request or {}
        modes = [
            str(value).upper()
            for value in request.get("modes", self._sif_options["modes"])
        ]
        modes = list(dict.fromkeys(mode for mode in modes if mode in {"FULL", "FLUO"}))
        if not modes:
            raise ValueError("Select at least one SIF mode: FULL or FLUO")
        position_mode = str(
            request.get("position_mode", self._sif_options["position_mode"])
        )
        if position_mode not in {"uav_airship", "tower"}:
            raise ValueError("SIF position mode must be UAV/Airship or Tower")
        gap = float(
            request.get(
                "max_position_gap_seconds",
                self._sif_options["max_position_gap_seconds"],
            )
        )
        if not 0.01 <= gap <= 10:
            raise ValueError("SIF maximum position gap must be 0.01–10 seconds")
        raw_min_kb = float(
            request.get("raw_min_kb", self._sif_options["raw_min_kb"])
        )
        if not 0 <= raw_min_kb <= 1_000_000:
            raise ValueError(
                "SIF raw-file minimum size must be between 0 and 1,000,000 KB"
            )
        options = {
            "modes": modes,
            "position_mode": position_mode,
            "raw_min_kb": raw_min_kb,
            "apply_nonlinearity_correction": request.get(
                "apply_nonlinearity_correction",
                self._sif_options["apply_nonlinearity_correction"],
            ) is True,
            "spectral_shift_correction": request.get(
                "spectral_shift_correction",
                self._sif_options["spectral_shift_correction"],
            ) is True,
            "drop_unmatched_telemetry": request.get(
                "drop_unmatched_telemetry",
                self._sif_options["drop_unmatched_telemetry"],
            ) is True,
            "drop_invalid_spectral_rows": request.get(
                "drop_invalid_spectral_rows",
                self._sif_options["drop_invalid_spectral_rows"],
            ) is True,
            "altitude_filter": request.get(
                "altitude_filter", self._sif_options["altitude_filter"]
            ) is True,
            "max_position_gap_seconds": gap,
            "static_lat": _optional_float(request.get("static_lat")),
            "static_lon": _optional_float(request.get("static_lon")),
            "static_alt": _optional_float(request.get("static_alt")),
            **self._validated_sif_essentials(request),
        }
        if position_mode == "tower":
            lat, lon = options["static_lat"], options["static_lon"]
            if lat is not None and not -90 <= lat <= 90:
                raise ValueError("SIF static latitude must be between -90 and 90")
            if lon is not None and not -180 <= lon <= 180:
                raise ValueError("SIF static longitude must be between -180 and 180")
        with self._lock:
            self._sif_options = options
            if self._flight_project is not None:
                self._flight_project.instrument_options["sif"] = dict(options)
        self.logger.log(
            LogLevel.INFO,
            "sif-options",
            "SIF processing options saved",
            instrument="sif",
            processing_step="configuration",
        )
        self._checkpoint_project()
        return dict(options)

    def _refresh_opc_combined_browser(self, project: FlightProject) -> Path | None:
        """Build the shared OPC page after both sensor payloads are available."""
        from instruments.hatchbox_payload import combine_opc_payloads, write_json_atomic

        with self._hatchbox_view_lock:
            with self._lock:
                paths = {
                    instrument_id: [Path(value) for value in self._instruments[instrument_id].output_files]
                    for instrument_id in ("opc_hbx4", "opc_hbx5")
                }
            selected: dict[str, Path] = {}
            for instrument_id, candidates in paths.items():
                browser = next(
                    (
                        value for value in reversed(candidates)
                        if value.name == f"{instrument_id}_browser.json"
                        and "quicklooks" not in value.parts
                    ),
                    None,
                )
                if browser is None or not browser.is_file():
                    return None
                selected[instrument_id] = browser
            payload = combine_opc_payloads(
                json.loads(selected["opc_hbx4"].read_text(encoding="utf-8")),
                json.loads(selected["opc_hbx5"].read_text(encoding="utf-8")),
                flight_id=project.flight_id,
            )
            target = project.flight_output_root / "quicklooks" / "opc_browser.json"
            write_json_atomic(target, payload)
            with self._lock:
                project.output_locations["opc_browser"] = target
                for instrument_id in ("opc_hbx4", "opc_hbx5"):
                    state = self._instruments[instrument_id]
                    if str(target) not in state.output_files:
                        state.output_files.append(str(target))
                    saved = project.detected_instruments.get(instrument_id)
                    if saved is not None and target not in saved.output_locations:
                        saved.output_locations.append(target)
            self.logger.log(
                LogLevel.SUCCESS,
                "hatchbox-view",
                "Combined OPC browser payload saved with independent HBX-4/HBX-5 axes",
                file_path=target,
                processing_step="browser-payload",
            )
            return target
    def start_noseboom_straight_recalculation(
        self, settings: dict[str, object]
    ) -> dict[str, object]:
        """Start a non-blocking straight-flight preview with live progress."""
        with self._lock:
            if self._noseboom_recalculation.get("running"):
                raise RuntimeError("A straight-flight recalculation is already running")
            job_id = uuid4().hex
            self._noseboom_recalculation = {
                "job_id": job_id,
                "running": True,
                "ready": False,
                "progress": 0.0,
                "message": "Preparing straight-flight recalculation.",
                "error": None,
                "result": None,
                "started_monotonic": time.monotonic(),
            }
            worker = threading.Thread(
                target=self._run_noseboom_straight_recalculation,
                args=(job_id, dict(settings or {})),
                name="ccflux-noseboom-straight-recalculation",
                daemon=True,
            )
            self._noseboom_recalculation_thread = worker
            worker.start()
        return self.noseboom_straight_recalculation_progress()

    def noseboom_straight_recalculation_progress(self) -> dict[str, object]:
        """Return a lock-safe progress snapshot for browser polling."""
        with self._lock:
            state = dict(self._noseboom_recalculation)
        started = state.pop("started_monotonic", None)
        state["elapsed_seconds"] = (
            max(0.0, time.monotonic() - float(started)) if started else 0.0
        )
        return state

    def _run_noseboom_straight_recalculation(
        self, job_id: str, settings: dict[str, object]
    ) -> None:
        def report(progress: float, message: str) -> None:
            with self._lock:
                if self._noseboom_recalculation.get("job_id") != job_id:
                    return
                self._noseboom_recalculation["progress"] = max(
                    0.0, min(100.0, float(progress))
                )
                self._noseboom_recalculation["message"] = str(message)

        try:
            result = self.preview_noseboom_straight_settings(
                settings, progress_callback=report
            )
        except Exception as exc:
            self.logger.capture_exception(
                "noseboom-settings",
                "Straight-flight recalculation failed",
                exc,
                instrument="noseboom",
                processing_step="straight-flight-recalculation",
            )
            with self._lock:
                if self._noseboom_recalculation.get("job_id") == job_id:
                    self._noseboom_recalculation.update({
                        "running": False,
                        "ready": False,
                        "error": str(exc),
                        "message": "Straight-flight recalculation failed.",
                    })
            self._persist_project_logs()
            return
        with self._lock:
            if self._noseboom_recalculation.get("job_id") == job_id:
                self._noseboom_recalculation.update({
                    "running": False,
                    "ready": True,
                    "progress": 100.0,
                    "message": "Straight-flight recalculation complete.",
                    "error": None,
                    "result": result,
                })
    def preview_noseboom_straight_settings(
        self, settings: dict[str, object], *,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> dict[str, object]:
        """Recalculate straight legs without changing the saved Flight Project."""
        from instruments.noseboom.adapter import _map_payload
        from instruments.noseboom.legacy_bridge import LegacyNoseboomBridge

        def report_progress(percent: float, message: str) -> None:
            if progress_callback is not None:
                progress_callback(max(0.0, min(100.0, float(percent))), str(message))

        report_progress(2.0, "Validating straight-flight settings.")

        allowed = {
            "min_speed_mps", "max_turn_rate_dps", "max_roll_deg",
            "heading_window_s", "max_heading_range_deg", "min_leg_seconds",
            "min_leg_distance_m", "target_leg_distance_m",
            "max_leg_heading_drift_deg", "max_cross_track_m",
            "max_altitude_deviation_m",
        }
        if not isinstance(settings, dict) or not settings:
            raise ValueError("Straight-flight settings must be a non-empty object")
        parsed: dict[str, float] = {}
        for key, value in settings.items():
            if key not in allowed:
                raise ValueError(f"Unsupported straight-flight setting: {key}")
            number = float(value)
            if number <= 0:
                raise ValueError(f"{key} must be greater than zero")
            parsed[key] = number
        if parsed.get("target_leg_distance_m", 0) < parsed.get("min_leg_distance_m", 0):
            raise ValueError("Target leg distance must be at least the minimum leg distance")
        report_progress(7.0, "Straight-flight settings validated.")

        with self._lock:
            report = self._report
            project = self._flight_project
            selected_start = self._time_state.selected_analysis_start
            selected_end = self._time_state.selected_analysis_end
            current_view = dict(self._instruments["noseboom"].quicklook)
        if report is None or project is None or selected_start is None or selected_end is None:
            raise RuntimeError("Complete Initial Check, apply the Time Filter, and process Noseboom first")
        paths = tuple(dict.fromkeys(
            path
            for candidate in report.candidates
            if candidate.instrument_id == "noseboom"
            for path in candidate.all_matching_files
        ))
        if not paths:
            raise RuntimeError("No selected Noseboom source files are available")
        report_progress(10.0, "Preparing the selected Noseboom source interval.")

        self.logger.log(
            LogLevel.INFO,
            "noseboom-settings",
            "Recalculating straight-flight legs for temporary visualization",
            instrument="noseboom",
            processing_step="straight-flight-recalculation",
        )
        bridge = LegacyNoseboomBridge()
        data = bridge.load_csv_window(
            paths,
            int(selected_start.timestamp() * 1_000_000_000),
            int(selected_end.timestamp() * 1_000_000_000),
            progress=lambda percent, message: report_progress(
                min(58.0, 12.0 + 1.15 * float(percent)), message
            ),
        )
        report_progress(60.0, f"Loaded {len(data):,} selected Noseboom rows.")
        report_progress(65.0, "Resampling the selected interval to validated 1 Hz navigation.")
        one_hz = bridge.module.one_hz(data)
        report_progress(75.0, "Detecting candidate straight-flight legs.")
        straight = bridge.module.detect_straight(one_hz, parsed)
        report_progress(88.0, "Preparing recalculated leg geometry and statistics.")
        recalculated = _map_payload(straight)
        report_progress(92.0, "Updating the temporary Straight Flight visualization.")
        for key in (
            "hist", "frequency", "altitude_profile", "spectra", "time_bounds",
            "browser_limits", "source",
        ):
            if key in current_view:
                recalculated[key] = current_view[key]
        recalculated["straight_settings"] = dict(
            straight.attrs.get("straight_params", parsed)
        )
        with self._lock:
            self._noseboom_preview_quicklook = dict(recalculated)
            self._noseboom_preview_settings = dict(recalculated["straight_settings"])
        report_progress(98.0, "Finalizing the temporary straight-flight preview.")
        self.logger.log(
            LogLevel.SUCCESS,
            "noseboom-settings",
            f"Temporary straight-flight recalculation complete: {len(recalculated.get('straight_legs', []))} legs",
            instrument="noseboom",
            processing_step="straight-flight-recalculation",
        )
        self._persist_project_logs()
        report_progress(100.0, "Straight-flight recalculation complete.")
        return {
            "saved": False,
            "temporary": True,
            "settings": dict(recalculated["straight_settings"]),
            "data": recalculated,
        }

    def save_noseboom_straight_preview(self) -> dict[str, object]:
        """Commit the latest straight-leg preview into the active Flight Project."""
        with self._lock:
            preview = getattr(self, "_noseboom_preview_quicklook", None)
            settings = getattr(self, "_noseboom_preview_settings", None)
            project = self._flight_project
            if not preview or not settings or project is None:
                raise RuntimeError("No recalculated straight-flight preview is available to save")
            state = self._instruments["noseboom"]
            state.quicklook = dict(preview)
            self._noseboom_straight_settings = dict(settings)
            quicklook_path = project.flight_output_root / "quicklooks" / "noseboom_browser.json"
            project.output_locations["noseboom_quicklook"] = quicklook_path
        quicklook_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = quicklook_path.with_suffix(".json.temporary")
        temporary.write_text(
            json.dumps(preview, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(quicklook_path)
        project_file = self.save_project()
        self.logger.log(
            LogLevel.SUCCESS,
            "noseboom-settings",
            "Straight-flight settings and recalculated legs saved in the Flight Project",
            instrument="noseboom",
            file_path=project_file,
            processing_step="straight-flight-settings-save",
        )
        self._persist_project_logs()
        return {
            "saved": True,
            "temporary": False,
            "settings": dict(settings),
            "data": dict(preview),
            "project_file": str(project_file),
        }

    def export_noseboom_data(self, options: dict[str, object]) -> Path:
        """Create a user-selected scientific table without altering processed data."""
        from instruments.noseboom.legacy_bridge import LegacyNoseboomBridge

        format_name = str(options.get("format", "csv")).strip().casefold()
        if format_name not in {"csv", "xlsx", "txt"}:
            raise ValueError("Download format must be CSV, XLSX, or TXT")
        frequency_hz = float(options.get("frequency_hz", 1.0))
        if not 1.0 <= frequency_hz <= 100.0:
            raise ValueError("Download frequency must be between 1 and 100 Hz")
        with self._lock:
            report = self._report
            project = self._flight_project
            selected_start = self._time_state.selected_analysis_start
            selected_end = self._time_state.selected_analysis_end
        if report is None or project is None or selected_start is None or selected_end is None:
            raise RuntimeError("Complete Initial Check and apply the Time Filter before downloading")
        paths = tuple(dict.fromkeys(
            path
            for candidate in report.candidates
            if candidate.instrument_id == "noseboom"
            for path in candidate.all_matching_files
        ))
        if not paths:
            raise RuntimeError("No selected Noseboom source files are available")
        bridge = LegacyNoseboomBridge()
        data = bridge.load_csv_window(
            paths,
            int(selected_start.timestamp() * 1_000_000_000),
            int(selected_end.timestamp() * 1_000_000_000),
        )
        source = bridge.module.make_export_source(data)
        table = bridge.module.resample_export_data(source, frequency_hz)
        destination = project.flight_output_root / "exports" / "noseboom"
        destination.mkdir(parents=True, exist_ok=True)
        safe_flight = "".join(
            value if value.isalnum() or value in "-_" else "_"
            for value in (project.flight_id or "Flight")
        )
        frequency_label = f"{frequency_hz:g}Hz".replace(".", "p")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = destination / f"{safe_flight}_noseboom_{frequency_label}_{stamp}.{format_name}"
        if format_name == "csv":
            table.to_csv(target, index=False, encoding="utf-8-sig")
        elif format_name == "txt":
            table.to_csv(target, index=False, sep="\t", encoding="utf-8")
        else:
            try:
                table.to_excel(target, index=False, engine="openpyxl")
            except ImportError as exc:
                raise RuntimeError(
                    "XLSX download requires the openpyxl library, which is missing "
                    "from this environment. Restart CC-FLUX with the launcher so it "
                    "can install the dependency, or download CSV or TXT instead."
                ) from exc
        self.logger.log(
            LogLevel.SUCCESS,
            "noseboom-export",
            f"Noseboom {format_name.upper()} download prepared at {frequency_hz:g} Hz ({len(table):,} rows)",
            instrument="noseboom",
            file_path=target,
            processing_step="interactive-data-export",
        )
        self._persist_project_logs()
        return target
    def update_noseboom_straight_settings(self, settings: dict[str, object]) -> dict[str, float]:
        allowed = {
            "min_speed_mps", "max_turn_rate_dps", "max_roll_deg",
            "heading_window_s", "max_heading_range_deg", "min_leg_seconds",
            "min_leg_distance_m", "target_leg_distance_m",
            "max_leg_heading_drift_deg", "max_cross_track_m",
            "max_altitude_deviation_m",
        }
        if not isinstance(settings, dict) or not settings:
            raise ValueError("Straight-flight settings must be a non-empty object")
        parsed: dict[str, float] = {}
        for key, value in settings.items():
            if key not in allowed:
                raise ValueError(f"Unsupported straight-flight setting: {key}")
            number = float(value)
            if number <= 0:
                raise ValueError(f"{key} must be greater than zero")
            parsed[key] = number
        if parsed.get("target_leg_distance_m", 0) and parsed.get("min_leg_distance_m", 0) and parsed["target_leg_distance_m"] < parsed["min_leg_distance_m"]:
            raise ValueError("Target leg distance must be at least the minimum leg distance")
        with self._lock:
            state = self._instruments["noseboom"]
            current = dict(state.quicklook.get("straight_settings", {}))
            current.update(parsed)
            if (
                current.get("target_leg_distance_m", 0)
                and current.get("min_leg_distance_m", 0)
                and current["target_leg_distance_m"] < current["min_leg_distance_m"]
            ):
                raise ValueError("Target leg distance must be at least the minimum leg distance")
            self._noseboom_straight_settings = current
            state.quicklook["straight_settings"] = dict(current)
            project = self._flight_project
            quicklook_file = (
                project.output_locations.get("noseboom_quicklook") if project else None
            )
        if quicklook_file:
            try:
                quicklook_path = Path(quicklook_file)
                quicklook_path.write_text(
                    json.dumps(state.quicklook, indent=2, allow_nan=False),
                    encoding="utf-8",
                )
            except (OSError, TypeError, ValueError) as exc:
                self.logger.log(
                    LogLevel.WARNING,
                    "noseboom-settings",
                    f"Straight-flight settings were kept in memory but could not be saved: {exc}",
                    instrument="noseboom",
                    processing_step="straight-flight-settings",
                )
        self.logger.log(
            LogLevel.INFO,
            "noseboom-settings",
            "Straight-flight thresholds saved for the next Noseboom processing run",
            instrument="noseboom",
            processing_step="straight-flight-settings",
        )
        self._persist_project_logs()
        return dict(current)
    def log_noseboom_view_event(self, message: str) -> None:
        self.logger.log(
            LogLevel.INFO,
            "noseboom-browser",
            message.strip() or "Noseboom browser interaction",
            instrument="noseboom",
            processing_step="browser-view",
        )
        self._persist_project_logs()

    def noseboom_export_file(self) -> Path:
        with self._lock:
            project = self._flight_project
            candidates = [
                Path(value) for value in self._instruments["noseboom"].output_files
                if Path(value).suffix.casefold() in {".csv", ".txt", ".h5"}
            ]
            if project is not None:
                saved = project.detected_instruments.get("noseboom")
                if saved is not None:
                    candidates.extend(
                        Path(value) for value in saved.output_locations
                        if Path(value).suffix.casefold() in {".csv", ".txt", ".h5"}
                    )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        if project is not None:
            run_root = project.flight_output_root / "processed" / "noseboom" / "runs"
            if run_root.is_dir():
                discovered = sorted(
                    run_root.glob("*/exports/*_noseboom_export_1Hz.csv"),
                    key=lambda value: value.stat().st_mtime,
                    reverse=True,
                )
                if discovered:
                    return discovered[0]
        raise ValueError(
            "No processed Noseboom navigation export is available. Process Noseboom first."
        )

    def start_noseboom_statistics_export(
        self, options: dict[str, object]
    ) -> dict[str, object]:
        with self._lock:
            project = self._flight_project
            payload = dict(self._instruments["noseboom"].quicklook)
            if project is None:
                raise ValueError("Load a Flight Project before exporting figures")
            if not payload.get("available"):
                raise ValueError("Process Noseboom before exporting figures")
            destination = (
                project.flight_output_root / "reports" / "noseboom_statistics"
            )
            flight_name = project.flight_id
        raw_formats = options.get("formats", ["pdf"])
        if not isinstance(raw_formats, list):
            raise ValueError("Export formats must be a list")
        formats = tuple(str(value).lower() for value in raw_formats)
        dpi = int(options.get("dpi", 300))
        return self._noseboom_statistics_export.start(
            payload, destination, flight_name, formats, dpi
        )

    def noseboom_statistics_export_progress(self) -> dict[str, object]:
        return self._noseboom_statistics_export.snapshot()

    def noseboom_statistics_export_file(self, name: str) -> Path:
        safe_name = Path(name).name
        if safe_name != name:
            raise ValueError("Invalid Noseboom export filename")
        try:
            return self._noseboom_statistics_export.file(name)
        except ValueError:
            with self._lock:
                project = self._flight_project
                candidates = [
                    Path(value)
                    for value in self._instruments["noseboom"].output_files
                ]
                if project is not None:
                    saved = project.detected_instruments.get("noseboom")
                    if saved is not None:
                        candidates.extend(saved.output_locations)
            for candidate in candidates:
                if (
                    candidate.name == safe_name
                    and candidate.suffix.casefold() in {".pdf", ".svg", ".png"}
                    and candidate.is_file()
                ):
                    return candidate
        raise ValueError("Requested Noseboom publication export is unavailable")

    def _save_noseboom_statistics_exports(self, outputs: list[Path]) -> None:
        with self._lock:
            project = self._flight_project
            state = self._instruments["noseboom"]
            for output in outputs:
                value = str(output)
                if value not in state.output_files:
                    state.output_files.append(value)
            if project is not None:
                saved = project.detected_instruments.get("noseboom")
                if saved is not None:
                    for output in outputs:
                        if output not in saved.output_locations:
                            saved.output_locations.append(output)
                if outputs:
                    project.output_locations[
                        "noseboom_statistics_export_directory"
                    ] = outputs[0].parent
                self._project_store.save_project(project, overwrite=True)
        self._persist_project_logs()
    def confirm_candidate(self, instrument_id: str, candidate_path: Path) -> None:
        selected = str(Path(candidate_path))
        with self._lock:
            if instrument_id not in self._instruments:
                raise ValueError(f"Unknown instrument ID: {instrument_id}")
            state = self._instruments[instrument_id]
            if not state.ambiguous:
                raise ValueError(f"{instrument_id} has no ambiguous candidates")
            if selected not in state.candidate_paths:
                raise ValueError("Selected path is not a registered candidate")
            state.candidate_paths = [selected]
            state.ambiguous = False
            state.warnings = [
                warning
                for warning in state.warnings
                if "multiple candidate" not in warning.casefold()
            ]
            if state.errors:
                state.detection_status = DetectionStatus.FAILED
            elif state.warnings:
                state.detection_status = DetectionStatus.WARNING
            else:
                state.detection_status = DetectionStatus.READY
            self._messages.append(
                f"Candidate confirmed for {state.display_name}: {selected}"
            )
        self.logger.log(
            LogLevel.SUCCESS,
            "flight-scanner",
            "Ambiguous instrument candidate confirmed",
            instrument=instrument_id,
            job_id=self._scan_id,
            file_path=Path(selected),
            processing_step="candidate-confirmation",
        )

    # Camera work and flight science run in separate scheduler pools with
    # reserved capacity, so neither can starve the other. Treating them as one
    # "system" was a property of this check alone, and it meant a long camera
    # run blocked every flight instrument for its whole duration.
    CAMERA_WORKER_GROUPS = frozenset({
        WorkerGroup.CAMERA_METADATA, WorkerGroup.CAMERA_DETAILED,
    })

    @classmethod
    def _job_domain(cls, job) -> str:
        return "camera" if job.worker_group in cls.CAMERA_WORKER_GROUPS else "flight"

    def _busy_domains(self) -> set[str]:
        """Which halves of the workflow still have a dispatched job."""
        return {
            self._job_domain(job)
            for job in (
                self.processing_queue.get(snapshot.job_id)
                for snapshot in self.processing_queue.ordered()
            )
            if job.enabled
            and job.task is not None
            and job.status in {ProcessingStatus.QUEUED, ProcessingStatus.PROCESSING}
        }

    def _processing_configuration_is_busy(self, domain: str | None = None) -> bool:
        """Whether a dispatched integrated job still owns the processing workflow.

        Without *domain* this answers for the whole workflow, which is what a
        genuinely global change - the Time Filter, the worker allocation - has to
        respect. A change confined to one half asks about that half only.
        """
        busy = self._busy_domains()
        return bool(busy) if domain is None else domain in busy

    def _require_processing_configuration_idle(self, domain: str | None = None) -> None:
        if self._processing_configuration_is_busy(domain):
            if domain is None:
                raise ValueError("Please wait! System is busy now!")
            side = "Camera" if domain == "camera" else "Flight-data"
            raise ValueError(
                f"Please wait! {side} processing is still running. "
                "The other half of the workflow can be used meanwhile."
            )

    TIME_FILTER_REQUEST_KEYS = frozenset({
        "action", "start", "end", "display_timezone",
    })

    def update_time_filter(self, request: dict[str, object]) -> None:
        # Adopting a package sets the range from the plan; that one call is
        # allowed through, and every other attempt on a worker is refused.
        if not request.pop("_from_work_package", False):
            self._refuse_if_worker("The processing time range")
        action = str(request.get("action", "set"))
        # An unrecognised key used to be ignored, and because a half-empty
        # interval is deliberately repaired to the available range, a misspelled
        # "start" silently widened the selection to the whole flight instead of
        # reporting anything. Name the mistake instead.
        unknown = sorted(set(request) - self.TIME_FILTER_REQUEST_KEYS)
        if unknown:
            raise ValueError(
                "Unsupported time-filter field(s): "
                + ", ".join(unknown)
                + ". The interval is set with 'start' and 'end'."
            )
        interval_warnings: tuple[str, ...] = ()
        with self._lock:
            self._require_processing_configuration_idle()
            if action in {"full", "reset"}:
                if action == "reset":
                    self._time_state.reset_to_detected_limits()
                else:
                    self._time_state.use_full_detected_interval()
            elif action == "common":
                self._time_state.use_common_overlap()
            elif action == "display":
                display_timezone = request.get("display_timezone")
                if not isinstance(display_timezone, str) or not display_timezone:
                    raise ValueError("display_timezone must be a non-empty IANA timezone")
                try:
                    ZoneInfo(display_timezone)
                except ZoneInfoNotFoundError as exc:
                    raise ValueError(
                        f"Unknown display timezone: {display_timezone}"
                    ) from exc
                self._time_state.display_timezone = display_timezone
            elif action == "set":
                # An interval can arrive reversed, half-empty, or reaching past
                # the data. Repair what is repairable and record every change,
                # rather than rejecting the whole request.
                repair = repair_interval(
                    parse_dashboard_datetime(request.get("start"))
                    if request.get("start") else None,
                    parse_dashboard_datetime(request.get("end"))
                    if request.get("end") else None,
                    available_start=self._time_state.detected_global_start,
                    available_end=self._time_state.detected_global_end,
                )
                if not repair.usable:
                    raise ValueError(
                        "A valid analysis interval could not be derived from the "
                        "requested times. "
                        + " ".join(repair.warnings)
                    )
                self._time_state.set_selected_interval(repair.start, repair.end)
                interval_warnings = repair.warnings
            else:
                raise ValueError(f"Unknown time-filter action: {action}")
            self._sync_project_time_state()
            start = self._time_state.selected_analysis_start
            end = self._time_state.selected_analysis_end
        self.logger.log(
            LogLevel.INFO,
            "time-filter",
            (
                f"Dashboard time filter changed ({action}): "
                f"{_iso(start) or 'not set'} to {_iso(end) or 'not set'}"
            ),
            job_id=self._scan_id,
            processing_step="time-filter",
        )
        for warning in interval_warnings:
            self.logger.log(
                LogLevel.WARNING,
                "time-filter",
                f"Analysis interval repaired automatically: {warning}",
                job_id=self._scan_id,
                processing_step="time-filter-repair",
            )

    def update_instrument_time_override(
        self,
        instrument_id: str,
        start: object = None,
        end: object = None,
    ) -> None:
        """Reject deprecated per-instrument intervals in favour of one global filter."""
        raise ValueError(
            "Instrument-specific intervals are no longer used. Set the global Time Filter instead."
        )
    def update_resources(
        self, *, worker_count: object, memory_bytes: object
    ) -> None:
        with self._lock:
            self._require_processing_configuration_idle()
        limits = self.resource_manager.create_limits(worker_count, memory_bytes)
        with self._lock:
            self._resource_limits = limits
            self._resources_auto_selected = False
            # A scheduler fixes its per-group capacities at construction, so a
            # cached one would keep the previous allocation and silently ignore
            # this change. Processing is idle here, so it is safe to retire it
            # and let the next dispatch rebuild it with the new limits.
            retired_scheduler = self._scheduler
            self._scheduler = None
            if self._flight_project is not None:
                self._flight_project.cpu_allocation = limits.worker_count
                self._flight_project.ram_allocation_bytes = limits.memory_bytes
        if retired_scheduler is not None:
            retired_scheduler.shutdown(wait=False)
        self.logger.log(
            LogLevel.INFO,
            "resource-manager",
            (
                f"Resource limits changed: {limits.worker_count} worker(s), "
                f"{limits.memory_bytes} bytes RAM"
            ),
            job_id=self._scan_id,
            processing_step="resource-allocation",
        )

    def update_queue(self, request: dict[str, object]) -> None:
        action = str(request.get("action", ""))
        job_id = str(request.get("job_id", ""))
        # Only the half this touches has to be idle. Selecting flight
        # instruments while the cameras run is the whole point of the split.
        if action == "reorder":
            self._require_processing_configuration_idle()
        else:
            try:
                affected = self.processing_queue.get(job_id)
            except (KeyError, ValueError):
                affected = None
            self._require_processing_configuration_idle(
                self._job_domain(affected) if affected is not None else None
            )
        if action == "reorder":
            job_ids = request.get("job_ids")
            if not isinstance(job_ids, list) or not all(
                isinstance(value, str) for value in job_ids
            ):
                raise ValueError("job_ids must be a list of strings")
            self.processing_queue.reorder(job_ids)
        elif action == "enable":
            job = self.processing_queue.get(job_id)
            state = self._instruments[job.instrument_id]
            if job_id not in INTEGRATED_PROCESSING_JOB_IDS:
                raise ValueError(
                    f"{job.display_name} processing is not integrated in the main queue"
                )
            if not _instrument_is_processable(state):
                raise ValueError(
                    f"{job.display_name} is not ready for selection; review its scan health first"
                )
            self.processing_queue.set_enabled(job_id, True)
        elif action == "disable":
            self.processing_queue.set_enabled(job_id, False)
        elif action == "pause":
            self.processing_queue.pause(job_id)
        elif action == "resume":
            self.processing_queue.resume(job_id)
        elif action == "cancel":
            self.processing_queue.cancel(job_id)
        elif action == "retry":
            self.processing_queue.retry(job_id)
        elif action == "reprocess":
            if request.get("confirmed") is not True:
                raise ValueError(
                    "Explicit confirmation is required before reprocessing "
                    "previously completed data"
                )
            job = self.processing_queue.get(job_id)
            state = self._instruments[job.instrument_id]
            if job.detailed or job_id not in INTEGRATED_PROCESSING_JOB_IDS:
                raise ValueError(
                    "Only integrated Level 1 instrument jobs can be reprocessed here"
                )
            if job.status not in {
                ProcessingStatus.COMPLETE,
                ProcessingStatus.WARNING,
            }:
                raise ValueError(
                    f"{job.display_name} has no completed result to reprocess"
                )
            if not _instrument_is_processable(state):
                raise ValueError(
                    f"{job.display_name} source data is not ready for reprocessing"
                )
            job.enabled = True
            job.status = ProcessingStatus.QUEUED
            job.progress = 0.0
            job.current_step = "Queued for explicitly confirmed reprocessing"
            job.elapsed_time = timedelta(0)
            job.error = None
            job.task = None
        else:
            raise ValueError(f"Unknown queue action: {action}")
        with self._lock:
            self._sync_project_queue_state()
            scheduler = self._scheduler
            if job_id:
                self._on_job_update(self.processing_queue.get(job_id).snapshot())
        if action in {"retry", "resume"} and scheduler is not None:
            scheduler.dispatch()
        self.logger.log(
            LogLevel.INFO,
            "processing-queue",
            f"Queue action {action} applied",
            instrument=(
                self.processing_queue.get(job_id).instrument_id
                if job_id
                else None
            ),
            job_id=job_id or None,
            processing_step="queue-management",
        )

    def log_remote_sensing_workflow(self, request: dict[str, object]) -> None:
        """Persist operator decisions from the guided remote-sensing workflow."""
        message = str(request.get("message", "")).strip()
        step = str(request.get("step", "workflow")).strip() or "workflow"
        if not message:
            raise ValueError("Remote-sensing workflow log message is required")
        if len(message) > 1000:
            raise ValueError("Remote-sensing workflow log message is too long")
        if len(step) > 64:
            raise ValueError("Remote-sensing workflow log step is too long")
        self.logger.log(
            LogLevel.INFO,
            "remote-sensing-workflow",
            message,
            job_id=self._scan_id,
            processing_step=step,
        )
        self._checkpoint_project()
    def start_remote_sensing(
        self, request: dict[str, object] | None = None
    ) -> list[str]:
        """Dispatch only camera-product jobs in their isolated worker pool."""
        request = request or {}
        # The whole selection is validated before any interval work, so a bad
        # product list is reported as a bad product list rather than surfacing
        # later as an unrelated complaint about the time frame.
        requested_check = request.get("instruments")
        if requested_check is not None:
            if not isinstance(requested_check, list) or not all(
                isinstance(value, str) for value in requested_check
            ):
                raise ValueError("instruments must be a list of instrument IDs")
            named = {str(value).strip() for value in requested_check if str(value).strip()}
            if not named:
                raise ValueError(
                    "Select at least one camera instrument for remote sensing"
                )
            known = {
                self.processing_queue.get(job_id).instrument_id
                for job_id in ("micasense_quick", "flir_quick", "gopro_quick")
            }
            unknown = named - known
            if unknown:
                raise ValueError(
                    "Unknown remote-sensing instrument(s): " + ", ".join(sorted(unknown))
                )
        # Chosen against camera coverage. The cameras take no part in the flight
        # Time Filter, so a camera-only project still has an interval to offer.
        time_mode = str(request.get("time_mode", "current"))
        self._select_remote_sensing_interval(time_mode, request)

        camera_tasks = {
            "micasense_quick": self._micasense_quick_task,
            "flir_quick": self._flir_quick_task,
            "gopro_quick": self._gopro_quick_task,
        }
        # Remote Sensing used to enable all three camera products
        # unconditionally, so an operator who wanted, say, everything except
        # MicaSense had no way to say so. An explicit selection is honoured;
        # omitting it keeps the previous behaviour of running whatever is ready.
        requested = request.get("instruments")
        if requested is not None:
            if not isinstance(requested, list) or not all(
                isinstance(value, str) for value in requested
            ):
                raise ValueError("instruments must be a list of instrument IDs")
            wanted = {str(value).strip() for value in requested if str(value).strip()}
            if not wanted:
                raise ValueError(
                    "Select at least one camera instrument for remote sensing"
                )
            unknown = wanted - {
                self.processing_queue.get(job_id).instrument_id
                for job_id in camera_tasks
            }
            if unknown:
                raise ValueError(
                    "Unknown remote-sensing instrument(s): " + ", ".join(sorted(unknown))
                )
            camera_tasks = {
                job_id: task
                for job_id, task in camera_tasks.items()
                if self.processing_queue.get(job_id).instrument_id in wanted
            }
        with self._lock:
            camera_channel = self._scan_channels["camera"]
            if (
                camera_channel["phase"] != "complete"
                or camera_channel["cancelled"]
                or camera_channel["error"] is not None
            ):
                raise ValueError(
                    "Camera scanning must finish successfully before remote-sensing processing"
                )
            if self._selected_output_folder is None or self._flight_project is None:
                raise ValueError(
                    "Select an Output Folder before remote-sensing processing"
                )
            # Camera products run in their own pool. Dispatching into a pool
            # with no capacity would leave every job queued forever in silence.
            worker_count = self._resource_limits.worker_count
            if worker_group_capacities(worker_count)[WorkerGroup.CAMERA_METADATA] < 1:
                # No capacity at all means the jobs would queue for ever in
                # silence, which is worth stopping for. A small allocation is
                # not: it is slower, and the operator is told so.
                raise ValueError(
                    "Remote-sensing camera products have no worker capacity with "
                    f"{worker_count} CPU worker(s). Raise the CPU allocation to "
                    "at least 2 workers in Resources, then start again."
                )
            if worker_count < 4:
                self.logger.log(
                    LogLevel.WARNING, "remote-sensing",
                    f"Remote sensing is starting with {worker_count} worker(s); "
                    "camera products share the machine with the flight "
                    "instruments and will take longer. Raise the CPU allocation "
                    "in Resources if that matters.",
                    processing_step="resources",
                )
            # The camera interval, not the flight Time Filter: the products are
            # selected against their own coverage.
            start = self._camera_selected_start
            end = self._camera_selected_end
            if start is None or end is None or start >= end:
                raise ValueError(
                    "Select a valid current or custom time frame before remote-sensing processing"
                )
            self._flight_project.output_folder_path = self._selected_output_folder
            registered: list[str] = []
            skipped: list[str] = []
            for job_id, task in camera_tasks.items():
                job = self.processing_queue.get(job_id)
                state = self._instruments[job.instrument_id]
                if (
                    state.detection_status in {
                        DetectionStatus.NOT_DETECTED,
                        DetectionStatus.FAILED,
                    }
                    or state.ambiguous
                ):
                    skipped.append(state.display_name)
                    continue
                if job.status in {
                    ProcessingStatus.PROCESSING,
                    ProcessingStatus.COMPLETE,
                    ProcessingStatus.WARNING,
                }:
                    continue
                if job.status is ProcessingStatus.CANCELLED:
                    skipped.append(f"{state.display_name} (cancelled)")
                    continue
                self.processing_queue.set_enabled(job_id, True)
                job.task = task
                registered.append(job_id)
            if not registered:
                raise ValueError(
                    "No detected camera product is ready for remote-sensing processing"
                )
            if self._scheduler is None:
                self._scheduler = ProcessingScheduler(
                    self.processing_queue,
                    total_workers=self._resource_limits.worker_count,
                    logger=self.logger,
                    result_callback=self._on_job_update,
                )
            scheduler = self._scheduler
            self._sync_project_queue_state()
            self._checkpoint_project()
        self.logger.log(
            LogLevel.INFO,
            "remote-sensing",
            (
                "Remote-sensing jobs dispatched independently using "
                f"{time_mode} time frame: " + ", ".join(registered)
            ),
            processing_step="remote-sensing",
        )
        if skipped:
            self.logger.log(
                LogLevel.WARNING,
                "remote-sensing",
                "Skipped unavailable camera products: " + ", ".join(skipped),
                processing_step="remote-sensing",
            )
        scheduler.dispatch()
        return registered
    def start_processing(self, *, confirmed_limited_coverage: bool = False) -> None:
        with self._lock:
            # No blanket idle requirement. Registration below already skips a
            # job that is COMPLETE, PROCESSING or CANCELLED, so a second start
            # cannot disturb work in flight; it only adds what is not running.
            # Refusing outright meant a camera run blocked every flight
            # instrument until it finished.
            integrated_tasks = {
                "noseboom": self._noseboom_task,
                "miro": self._miro_task,
                "picarro": self._picarro_task,
                "opc_hbx4": lambda context: self._opc_task("opc_hbx4", context),
                "opc_hbx5": lambda context: self._opc_task("opc_hbx5", context),
                "partector": self._partector_task,
                "ins_gimbal": self._ins_gimbal_task,
                "sif": self._sif_task,
                "micasense_quick": self._micasense_quick_task,
                "flir_quick": self._flir_quick_task,
                "gopro_quick": self._gopro_quick_task,
            }
            self._validate_processing_preflight(integrated_tasks)
            limited_coverage = self._limited_coverage_messages(integrated_tasks)
            if limited_coverage and not confirmed_limited_coverage:
                raise ValueError(
                    "Selected instruments have limited coverage in the global Time Filter: "
                    + "; ".join(limited_coverage)
                    + ". Review availability and confirm processing."
                )
            missing_navigation = self._noseboom_dependency_messages(integrated_tasks)
            if missing_navigation and not confirmed_limited_coverage:
                raise ValueError(
                    "Noseboom is not selected, and it is the navigation reference: "
                    + "; ".join(missing_navigation)
                    + ". Select Noseboom, or confirm to process without positions."
                )
            registered = []
            skipped = []
            for job_id, task in integrated_tasks.items():
                job = self.processing_queue.get(job_id)
                instrument_id = job.instrument_id
                state = self._instruments[instrument_id]
                if not _instrument_is_processable(state):
                    if job.enabled and not job.detailed:
                        skipped.append(
                            f"{state.display_name} ({state.detection_status.value})"
                        )
                    continue
                if job.status in {
                    ProcessingStatus.COMPLETE,
                    ProcessingStatus.WARNING,
                    ProcessingStatus.PROCESSING,
                    ProcessingStatus.CANCELLED,
                }:
                    continue
                job.task = task
                if job.status is ProcessingStatus.PAUSED and job.enabled:
                    self.processing_queue.resume(job_id)
                registered.append(job_id)
            if not registered:
                raise ValueError(
                    "No detected, confirmed, integrated instrument is ready to process"
                )
            if self._scheduler is None:
                self._scheduler = ProcessingScheduler(
                    self.processing_queue,
                    total_workers=self._resource_limits.worker_count,
                    logger=self.logger,
                    result_callback=self._on_job_update,
                )
            self._scheduler.dispatch()
            self._checkpoint_project()
        self.logger.log(
            LogLevel.INFO,
            "processing-queue",
            "Integrated adapter jobs dispatched: " + ", ".join(registered),
        )
        if skipped:
            self.logger.log(
                LogLevel.WARNING,
                "processing-queue",
                "Skipped instruments that are not READY: " + ", ".join(skipped),
            )

    def _limited_coverage_messages(self, tasks) -> list[str]:
        """Describe selected ready inputs that cover only part of the global interval."""
        messages: list[str] = []
        for job in self.processing_queue.ordered():
            if (
                not job.enabled
                or job.detailed
                or job.job_id not in tasks
                or not _instrument_is_processable(self._instruments[job.instrument_id])
            ):
                continue
            coverage = self._time_state.instruments.get(job.instrument_id)
            percentage = None if coverage is None else coverage.availability_percentage
            if percentage is not None and 0 < percentage < 100:
                messages.append(f"{job.display_name} ({percentage:.1f}% available)")
        return messages
    # What each of these needs from the Noseboom, in the operator's terms.
    NOSEBOOM_DEPENDENTS = {
        "gopro": "capture positions",
        "flir": "frame georeferencing",
    }

    def _noseboom_dependency_messages(self, tasks) -> list[str]:
        """Selected products that would come out without positions.

        GoPro and the detailed FLIR conversion both read the Noseboom's
        *processed* 1 Hz navigation. Leaving the Noseboom unselected used to be
        silent: the run finished, and the camera products came out with no
        positions and nothing said why. The Noseboom is the campaign's UTC and
        navigation reference, so this is worth stopping for.
        """
        noseboom = self.processing_queue.get("noseboom")
        already_processed = bool(
            self._instruments["noseboom"].quicklook.get("points")
        )
        if noseboom.enabled or already_processed:
            return []
        messages: list[str] = []
        for job in self.processing_queue.ordered():
            if not job.enabled or job.job_id not in tasks:
                continue
            needs = self.NOSEBOOM_DEPENDENTS.get(job.instrument_id)
            if needs and _instrument_is_processable(
                self._instruments[job.instrument_id]
            ):
                messages.append(f"{job.display_name} would have no {needs}")
        return messages

    def _validate_processing_preflight(self, tasks) -> None:
        if self._selected_folder is None:
            raise ValueError("Select a Flight Folder before processing")
        if self._selected_output_folder is None:
            raise ValueError("Select an Output Folder before processing")
        if self._report is None or self._phase not in {
            "complete",
            "scanning_camera",
        }:
            raise ValueError(
                "Flight Folder discovery and validation must complete before processing"
            )
        start, end = self._time_state.selected_analysis_start, self._time_state.selected_analysis_end
        if start is None or end is None or start >= end:
            raise ValueError("Select a valid analysis time interval before processing")
        self.resource_manager.create_limits(
            self._resource_limits.worker_count, self._resource_limits.memory_bytes
        )
        if self._resource_limits.worker_count < 1:
            raise ValueError("At least one CPU worker is required")
        jobs = self.processing_queue.ordered()
        if len({job.job_id for job in jobs}) != len(jobs) or any(job.priority not in {1, 2, 3} for job in jobs):
            raise ValueError("Processing priority queue is invalid")
        enabled = [
            job for job in jobs if job.enabled and not job.detailed and job.job_id in tasks
            and _instrument_is_processable(self._instruments[job.instrument_id])
        ]
        if not enabled:
            raise ValueError("Select at least one scientifically usable instrument for processing")
        unavailable = []
        for job in enabled:
            coverage = self._time_state.instruments.get(job.instrument_id)
            percentage = None if coverage is None else coverage.availability_percentage
            if percentage is None or percentage <= 0:
                unavailable.append(job.display_name)
        if unavailable:
            raise ValueError(
                "The global Time Filter has no available data for: "
                + ", ".join(unavailable)
                + ". Change the Time Filter or deselect these instruments."
            )
        if self._flight_project:
            self._flight_project.output_folder_path = self._selected_output_folder
            self._flight_project.validate()

    def _persist_project_logs(self) -> Path | None:
        with self._lock:
            project = self._flight_project
            output = self._selected_output_folder
        if project is None or output is None:
            return None
        destination = project.flight_output_root / "logs" / "processing.jsonl"
        project.output_locations["processing_log"] = destination
        try:
            return self.logger.export_logs(destination, overwrite=True)
        except Exception as exc:
            self.logger.capture_exception(
                "project",
                "Project diagnostics log export failed",
                exc,
                processing_step="log-checkpoint",
            )
            return None
    def _checkpoint_project(self) -> None:
        with self._lock:
            if self._flight_project is None or self._selected_output_folder is None:
                return
            self._flight_project.output_folder_path = self._selected_output_folder
            self._sync_project_queue_state()
            self._sync_project_time_state()
            project = self._flight_project
        try:
            self._persist_project_logs()
            self._project_store.save_project(project, overwrite=True)
        except Exception as exc:
            self.logger.capture_exception(
                "project", "Processing project checkpoint failed", exc,
                job_id=self._scan_id, processing_step="checkpoint",
            )

    def shutdown(self) -> None:
        with self._lock:
            scheduler = self._scheduler
            tokens = tuple(self._scan_tokens.values())
            worker = self._worker
            self._checkpoint_project()
        for token in tokens:
            token.cancel()
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        if scheduler is not None:
            scheduler.shutdown(wait=True, cancel_pending=True)
            with self._lock:
                self._sync_project_queue_state()
                self._checkpoint_project()

    def _default_scanner(self) -> FlightFolderScanner:
        configuration = load_detection_configuration(
            self.application_root / "configs" / "instrument_detection.yaml",
            self.application_root / "configs" / "file_patterns.yaml",
        )
        return FlightFolderScanner(configuration)

    def _run_scan(
        self,
        root: Path,
        camera_root: Path | None,
        flight_token: ScanCancellationToken,
        camera_token: ScanCancellationToken | None,
    ) -> None:
        roots = {"flight": root}
        tokens = {"flight": flight_token}
        if camera_root is not None and camera_token is not None:
            roots["camera"] = camera_root
            tokens["camera"] = camera_token
        reports: dict[str, ScanReport] = {}

        def merged_successful_report() -> ScanReport | None:
            values = list(reports.values())
            if not values:
                return None
            combined = values[0]
            for value in values[1:]:
                combined = _merge_scan_reports(combined, value)
            return replace(combined, cancelled=False)

        try:
            # One bounded discovery worker keeps the browser, disk, and scientific
            # applications responsive even when a camera tree contains many files.
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ccflux-discovery"
            ) as executor:
                futures = {
                    executor.submit(
                        self._scanner_factory().scan,
                        scan_root,
                        cancellation=tokens[source],
                        progress_callback=(
                            lambda update, scan_source=source:
                            self._on_progress(update, scan_source)
                        ),
                        # The camera folder is read GoPro, then FLIR, then
                        # MicaSense, rather than in whatever order the
                        # filesystem happens to hand back.
                        top_level_order=(
                            self.CAMERA_SCAN_ORDER if source == "camera" else ()
                        ),
                    ): source
                    for source, scan_root in roots.items()
                }
                for future in as_completed(futures):
                    source = futures[future]
                    scan_root = roots[source]
                    try:
                        report = future.result()
                    except Exception as exc:
                        with self._lock:
                            channel = self._scan_channels[source]
                            channel["running"] = False
                            channel["phase"] = "failed"
                            channel["error"] = str(exc)
                            channel["message"] = f"{source.title()} scanning failed: {exc}"
                            self._error = str(exc)
                            self._messages.append(str(channel["message"]))
                        self.logger.capture_exception(
                            f"{source}-scanner",
                            f"{source.title()} data scan failed",
                            exc,
                            job_id=f"{self._scan_id}-{source}",
                            file_path=scan_root,
                            processing_step="discovery",
                        )
                        continue

                    with self._lock:
                        channel = self._scan_channels[source]
                        channel["files_scanned"] = report.files_scanned
                        channel["cancelled"] = report.cancelled
                        channel["phase"] = (
                            "cancelled" if report.cancelled else "post_scan_checks"
                        )
                        channel["progress"] = (
                            channel["progress"] if report.cancelled else None
                        )
                        channel["message"] = (
                            f"{source.title()} scan cancelled safely."
                            if report.cancelled
                            else (
                                "File inventory complete. Validating detected "
                                "instruments and time coverage..."
                            )
                        )

                    if report.cancelled:
                        self.logger.log(
                            LogLevel.WARNING,
                            f"{source}-scanner",
                            (
                                f"{source.title()} scan cancelled after "
                                f"{report.files_scanned} files"
                            ),
                            job_id=f"{self._scan_id}-{source}",
                            processing_step="discovery",
                        )
                    else:
                        reports[source] = replace(report, cancelled=False)
                        combined = merged_successful_report()
                        if combined is not None:
                            with self._lock:
                                self._report = combined
                                other_running = any(
                                    bool(item["running"])
                                    for item_source, item in self._scan_channels.items()
                                    if item_source != source
                                )
                                channel = self._scan_channels[source]
                                channel["phase"] = "post_scan_checks"
                                channel["progress"] = None
                                channel["message"] = (
                                    "Building the instrument inventory and checking "
                                    "timestamp coverage..."
                                )
                            self._apply_report(combined, final=not other_running)
                        self.logger.log(
                            LogLevel.SUCCESS,
                            f"{source}-scanner",
                            (
                                f"{source.title()} scan complete: "
                                f"{report.files_scanned} files, "
                                f"{len(report.detected_instrument_ids)} "
                                "instruments detected"
                            ),
                            job_id=f"{self._scan_id}-{source}",
                            processing_step="discovery",
                        )

                    with self._lock:
                        channel = self._scan_channels[source]
                        channel["running"] = False
                        if not report.cancelled:
                            channel["phase"] = "complete"
                            channel["progress"] = 100.0
                            channel["message"] = (
                                "Processing is done! close the window! "
                                f"{source.title()} scan inspected "
                                f"{report.files_scanned} files and completed all checks."
                            )
                            self._messages.append(str(channel["message"]))
                        if not any(
                            bool(item["running"])
                            for item in self._scan_channels.values()
                        ):
                            if reports:
                                self._phase = "complete"
                                self._progress = 100.0
                                self._cancelled = False
                            elif any(
                                item["error"] for item in self._scan_channels.values()
                            ):
                                self._phase = "failed"
                            else:
                                self._phase = "cancelled"
                                self._cancelled = True
        except Exception as exc:
            with self._lock:
                self._phase = "failed"
                self._error = str(exc)
                self._messages.append(f"Scanning failed: {exc}")
            self.logger.capture_exception(
                "flight-scanner",
                "Independent scan coordinator failed",
                exc,
                job_id=self._scan_id,
                file_path=root,
                processing_step="discovery",
            )
        with self._lock:
            scan_completed = self._phase == "complete" and self._report is not None
        if scan_completed:
            # Covers the opposite workflow order: Output Folder selected first,
            # then Initial Check. Save the validated scan when it completes.
            self._checkpoint_project()

    def _on_progress(self, update: ScanProgress, source: str = "flight") -> None:
        with self._lock:
            channel = self._scan_channels[source]
            old_ids = set(channel["detected_instruments"])
            new_ids = set(update.detected_candidate_instruments) - old_ids
            is_post_scan_check = update.phase == "complete" and not update.cancelled
            channel["phase"] = "post_scan_checks" if is_post_scan_check else update.phase
            channel["running"] = True
            channel["current_folder"] = (
                update.current_folder or channel["current_folder"]
            )
            channel["current_file"] = update.current_file or channel["current_file"]
            # Tally per top-level folder. A camera delivery is 3,651 GoPro
            # frames, 536 MicaSense files and one 36 GB FLIR export, so the
            # single "current file" line sits inside GoPro for almost the whole
            # scan and it looks as though nothing else is being read. The delta
            # is attributed rather than counted per file, which stays correct if
            # the scanner reports in batches.
            delta = max(0, int(update.files_scanned) - int(channel["files_scanned"]))
            if delta:
                group = self._scan_group_name(channel["root"], update.current_file)
                if group is None:
                    # An update without a current file still counts files; they
                    # belong to whatever folder the walk was last in, not to a
                    # bucket of their own.
                    group = channel["last_group"] or "(counting)"
                channel["last_group"] = group
                channel["folder_counts"][group] += delta
            channel["files_scanned"] = update.files_scanned
            channel["progress"] = (
                None if update.phase in {"inventory", "complete"} else update.progress
            )
            channel["detected_instruments"] = update.detected_candidate_instruments
            channel["message"] = (
                f"Counting files in {update.current_folder or channel['root']}"
                if update.phase == "inventory"
                else (
                    "File inventory complete. Preparing validation checks..."
                    if is_post_scan_check
                    else f"Scanning {update.current_file or update.current_folder or channel['root']}"
                )
            )
            all_detected = {
                instrument_id
                for item in self._scan_channels.values()
                for instrument_id in item["detected_instruments"]
            }
            self._phase = f"scanning_{source}"
            self._current_folder = update.current_folder
            self._current_file = update.current_file
            self._files_scanned = sum(
                int(item["files_scanned"])
                for item in self._scan_channels.values()
            )
            self._progress = channel["progress"]
            display_ids = update.detected_candidate_instruments
            channel["current_instrument"] = (
                ", ".join(
                    self._instruments[item].display_name
                    for item in display_ids
                    if item in self._instruments
                )
                or "Identifying instrument"
            )
            self._current_instrument = str(channel["current_instrument"])
            self._detected = tuple(sorted(all_detected))
            if update.files_scanned == 1 or (
                update.files_scanned > 0 and update.files_scanned % 100 == 0
            ):
                self.logger.log(
                    LogLevel.INFO,
                    f"{source}-scanner",
                    (
                        f"Live scan checkpoint: {update.files_scanned} files, "
                        f"{round(update.progress or 0.0, 1)}%"
                    ),
                    job_id=f"{self._scan_id}-{source}",
                    file_path=update.current_file or update.current_folder,
                    processing_step=update.phase,
                )
            for instrument_id in sorted(new_ids):
                self._instruments[instrument_id].detection_status = (
                    DetectionStatus.DETECTED
                )
                message = f"{source.title()} candidate detected: {instrument_id}"
                self._messages.append(message)
                self.logger.log(
                    LogLevel.INFO,
                    f"{source}-scanner",
                    message,
                    instrument=instrument_id,
                    job_id=f"{self._scan_id}-{source}",
                    file_path=update.current_file,
                    processing_step="detection",
                )
    def _apply_report(self, report: ScanReport, *, final: bool = True) -> None:
        grouped: dict[str, list[InstrumentCandidate]] = {}
        for candidate in report.candidates:
            grouped.setdefault(candidate.instrument_id, []).append(candidate)
        expanded_source_files: dict[str, tuple[Path, ...]] = {}
        opc_files = {
            instrument_id: {
                path.resolve()
                for candidate in grouped.get(instrument_id, ())
                for path in candidate.all_matching_files
            }
            for instrument_id in ("opc_hbx4", "opc_hbx5")
        }
        ambiguous_opc_files = opc_files["opc_hbx4"] & opc_files["opc_hbx5"]
        extractor = TimestampExtractor()
        validation_items = tuple(grouped.items())
        validation_total = len(validation_items)
        for validation_index, (instrument_id, candidates) in enumerate(
            validation_items, start=1
        ):
            with self._lock:
                display_name = self._instruments[instrument_id].display_name
                self._phase = "validating"
                self._current_folder = None
                self._current_file = None
                self._current_instrument = display_name
                self._progress = (
                    100.0 * (validation_index - 1) / validation_total
                    if validation_total
                    else 100.0
                )
                self._messages.append(
                    f"Validating {display_name} "
                    f"({validation_index}/{validation_total})..."
                )
                self._instruments[instrument_id].detection_status = (
                    DetectionStatus.VALIDATING
                )
            unique_source_files = self._validation_source_files(
                instrument_id, candidates
            )
            expanded_source_files[instrument_id] = unique_source_files
            file_count = len(unique_source_files)
            confidence = max(item.confidence_score for item in candidates)
            candidate_paths = [str(item.candidate_path) for item in candidates]
            ambiguous = any(item.ambiguous for item in candidates)
            warnings = list(
                dict.fromkeys(
                    warning for item in candidates for warning in item.warnings
                )
            )
            errors = list(
                dict.fromkeys(error for item in candidates for error in item.errors)
            )
            if instrument_id in opc_files and ambiguous_opc_files:
                ambiguous = True
                warnings.append(
                    "One or more CSV files match both OPC identities; confirm "
                    "the HBX-4/HBX-5 assignment before processing."
                )
                candidate_paths.extend(
                    str(path) for path in sorted(ambiguous_opc_files)
                    if str(path) not in candidate_paths
                )
            time_result = extractor.extract_instrument(
                instrument_id, unique_source_files
            )
            timestamp_warnings = list(time_result.timestamp_quality_warnings)
            card_timestamp_warnings = timestamp_warnings
            if instrument_id == "miro":
                # MIRO text deliveries can contain a few blank boundary rows or
                # repeated timestamps where logger segments meet. They remain
                # recorded in timestamp diagnostics, but a valid TXT dataset
                # must not be presented as waiting for TDMS or blocked from
                # processing. Invalid/non-monotonic time remains a card warning.
                card_timestamp_warnings = [
                    warning
                    for warning in timestamp_warnings
                    if "duplicated timestamp" not in warning
                    and "missing timestamp value" not in warning
                ]
            warnings.extend(card_timestamp_warnings)
            with self._lock:
                state = self._instruments[instrument_id]
                state.file_count = file_count
                state.confidence = confidence
                state.candidate_paths = candidate_paths
                state.ambiguous = ambiguous
                state.warnings = list(dict.fromkeys(warnings))
                state.timestamp_warnings = timestamp_warnings
                state.errors = errors
                state.utc_start_time = time_result.utc_start_time
                state.utc_end_time = time_result.utc_end_time
                state.original_start_time = time_result.original_min_timestamp
                state.original_end_time = time_result.original_max_timestamp
                if instrument_id == "gopro" and time_result.utc_start_time:
                    state.processing_step = (
                        "Time corrected during detection: Europe/Berlin → UTC"
                    )
                    state.quicklook = {
                        **state.quicklook,
                        "time_correction_complete": True,
                        "camera_timezone": "Europe/Berlin (CET/CEST)",
                        "navigation_timezone": "UTC",
                        "corrected_start_utc": _iso(time_result.utc_start_time),
                        "corrected_end_utc": _iso(time_result.utc_end_time),
                    }
                if state.errors:
                    state.detection_status = DetectionStatus.FAILED
                elif state.ambiguous or state.warnings:
                    state.detection_status = DetectionStatus.WARNING
                else:
                    state.detection_status = DetectionStatus.READY
                self._progress = (
                    100.0 * validation_index / validation_total
                    if validation_total
                    else 100.0
                )
                self._messages.append(
                    f"{display_name} validation finished: "
                    f"{state.detection_status.value}."
                )
                if instrument_id == "gopro" and time_result.utc_start_time:
                    self._messages.append(
                        "GoPro camera time corrected from Europe/Berlin "
                        "(CET/CEST) to UTC during detection."
                    )
        with self._lock:
            self._time_state = DashboardTimeState.from_instrument_ranges(
                {
                    key: (
                        value.utc_start_time,
                        value.utc_end_time,
                        value.timestamp_warnings,
                    )
                    for key, value in self._instruments.items()
                    if value.detection_status is not DetectionStatus.NOT_DETECTED
                },
                analysis_anchor_id="noseboom",
            )
            self._sync_project_from_report(
                report, expanded_source_files=expanded_source_files
            )
            if final:
                self._phase = "complete"
                self._current_instrument = None
                self._progress = 100.0
                self._messages.append(
                    f"Scan complete: {report.files_scanned} files inspected."
                )
            else:
                self._phase = "scanning_camera"
                self._current_instrument = "Camera System"
                self._progress = None
            if any(item.ambiguous for item in report.candidates):
                self._messages.append(
                    "Ambiguous candidates require user confirmation."
                )

    @staticmethod
    def _validation_source_files(
        instrument_id: str, candidates: Sequence[InstrumentCandidate]
    ) -> tuple[Path, ...]:
        """Expand GoPro candidates before time validation.

        Discovery deliberately retains only a small camera sample. GoPro time
        correction, availability, and the user's Time Filter must instead use
        every media file in the detected GoPro folder.
        """
        retained = [
            path
            for candidate in candidates
            for path in candidate.all_matching_files
        ]
        if instrument_id != "gopro":
            return tuple(dict.fromkeys(retained))
        supported = {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
        expanded: list[Path] = []
        for candidate in candidates:
            root = candidate.candidate_path
            if root.is_dir():
                expanded.extend(
                    path
                    for path in root.rglob("*")
                    if path.is_file() and path.suffix.casefold() in supported
                )
            elif root.is_file() and root.suffix.casefold() in supported:
                expanded.append(root)
        expanded.extend(
            path
            for path in retained
            if path.is_file() and path.suffix.casefold() in supported
        )
        return tuple(dict.fromkeys(expanded))

    def _sync_project_from_report(
        self,
        report: ScanReport,
        *,
        expanded_source_files: Mapping[str, tuple[Path, ...]] | None = None,
    ) -> None:
        if self._flight_project is None:
            return
        previous_instruments = dict(
            self._flight_project.detected_instruments
        )
        completed_instruments = {
            job.instrument_id
            for job in self.processing_queue.ordered()
            if job.status in {
                ProcessingStatus.COMPLETE,
                ProcessingStatus.WARNING,
            }
        }
        grouped: dict[str, list[InstrumentCandidate]] = {}
        for candidate in report.candidates:
            grouped.setdefault(candidate.instrument_id, []).append(candidate)
        project_instruments: dict[str, InstrumentProjectState] = {}
        for instrument_id, candidates in grouped.items():
            scan_state = self._instruments[instrument_id]
            previous = previous_instruments.get(instrument_id)
            project_instruments[instrument_id] = InstrumentProjectState(
                instrument_id=instrument_id,
                selected_source_files=list(
                    (expanded_source_files or {}).get(instrument_id)
                    or dict.fromkeys(
                        path
                        for candidate in candidates
                        for path in candidate.all_matching_files
                    )
                ),
                selected_source_folders=[
                    Path(value) for value in scan_state.candidate_paths
                ],
                detection_confidence=scan_state.confidence,
                ambiguous_candidates=(
                    [Path(value) for value in scan_state.candidate_paths]
                    if scan_state.ambiguous
                    else []
                ),
                utc_start_time=scan_state.utc_start_time,
                utc_end_time=scan_state.utc_end_time,
                timestamp_warnings=list(scan_state.timestamp_warnings),
                processing_priority=self._registry.find_by_id(
                    instrument_id
                ).effective_priority,
                enabled=(
                    previous.enabled
                    if previous is not None
                    else self._registry.find_by_id(
                        instrument_id
                    ).effective_enabled
                ),
                output_locations=(
                    list(previous.output_locations)
                    if previous is not None
                    else []
                ),
            )
        for instrument_id, previous in previous_instruments.items():
            if (
                instrument_id not in project_instruments
                and instrument_id in completed_instruments
            ):
                project_instruments[instrument_id] = previous
        self._flight_project.detected_instruments = project_instruments
        self._flight_project.original_start_time = (
            self._time_state.detected_global_start
        )
        self._flight_project.original_end_time = self._time_state.detected_global_end
        self._flight_project.utc_start_time = self._time_state.detected_global_start
        self._flight_project.utc_end_time = self._time_state.detected_global_end
        self._sync_project_time_state()

    def _sync_project_time_state(self) -> None:
        if self._flight_project is None:
            return
        self._flight_project.selected_analysis_start = (
            self._time_state.selected_analysis_start
        )
        self._flight_project.selected_analysis_end = (
            self._time_state.selected_analysis_end
        )
        self._flight_project.display_timezone = self._time_state.display_timezone
        for instrument_id, selection in self._time_state.instruments.items():
            project_state = self._flight_project.detected_instruments.get(
                instrument_id
            )
            if project_state is not None:
                project_state.analysis_start_time = selection.override_start
                project_state.analysis_end_time = selection.override_end

    def _new_instrument_states(self) -> dict[str, InstrumentScanState]:
        return {
            item.instrument_id: InstrumentScanState(
                instrument_id=item.instrument_id,
                display_name=item.display_name,
                physical_group=item.physical_group.value,
            )
            for item in self._registry.list_all()
        }

    def _summary(self) -> dict[str, object]:
        states = tuple(self._instruments.values())
        detected = tuple(
            item
            for item in states
            if item.detection_status is not DetectionStatus.NOT_DETECTED
        )
        starts = [item.utc_start_time for item in detected if item.utc_start_time]
        ends = [item.utc_end_time for item in detected if item.utc_end_time]
        return {
            "detected_count": len(detected),
            "ready_count": sum(
                item.detection_status is DetectionStatus.READY for item in states
            ),
            "warning_count": sum(
                item.detection_status is DetectionStatus.WARNING for item in states
            ),
            "failed_count": sum(
                item.detection_status is DetectionStatus.FAILED for item in states
            ),
            "global_start_time": _iso(min(starts)) if starts else None,
            "global_end_time": _iso(max(ends)) if ends else None,
        }

    def _resource_snapshot(self) -> dict[str, object]:
        system = self.resource_manager.system
        minimum_workers = 1
        maximum_workers = system.safely_available_workers
        recommended_workers = _balanced_worker_count(system)
        minimum_ram = min(GIB, system.safely_available_ram_bytes)
        recommended_ram_target = min(
            system.safely_available_ram_bytes,
            max(minimum_ram, int(system.total_ram_bytes * 0.25)),
        )
        ram_options = [
            value * GIB
            for value in (1, 2, 4, 8, 16, 32, 64, 128)
            if minimum_ram <= value * GIB <= system.safely_available_ram_bytes
        ]
        if minimum_ram not in ram_options:
            ram_options.append(minimum_ram)
        if system.safely_available_ram_bytes not in ram_options:
            ram_options.append(system.safely_available_ram_bytes)
        ram_options = sorted(set(ram_options))
        recommended_ram = max(
            (value for value in ram_options if value <= recommended_ram_target),
            default=minimum_ram,
        )
        return {
            "total_logical_cores": system.total_logical_cores,
            "reserved_gui_cores": system.reserved_gui_cores,
            "safe_worker_count": maximum_workers,
            "minimum_worker_count": minimum_workers,
            "maximum_worker_count": maximum_workers,
            "recommended_worker_count": recommended_workers,
            "selected_worker_count": self._resource_limits.worker_count,
            "total_ram_bytes": system.total_ram_bytes,
            "safe_ram_bytes": system.safely_available_ram_bytes,
            "minimum_ram_bytes": minimum_ram,
            "maximum_ram_bytes": system.safely_available_ram_bytes,
            "recommended_ram_bytes": recommended_ram,
            "selected_ram_bytes": self._resource_limits.memory_bytes,
            "selection_mode": (
                "automatic" if self._resources_auto_selected else "operator"
            ),
            "worker_options": list(range(minimum_workers, maximum_workers + 1)),
            "ram_options": ram_options,
            "worker_environment": self.resource_manager.worker_environment(
                self._resource_limits
            ),
        }
    def _queue_snapshot(self) -> dict[str, object]:
        jobs = self.processing_queue.ordered()
        job_payload: list[dict[str, object]] = []
        selectable_job_ids: set[str] = set()
        for job in jobs:
            payload = _job_dict(job)
            state = self._instruments[job.instrument_id]
            integrated = job.job_id in INTEGRATED_PROCESSING_JOB_IDS
            coverage = self._time_state.instruments.get(job.instrument_id)
            interval_selected = (
                self._time_state.selected_analysis_start is not None
                and self._time_state.selected_analysis_end is not None
            )
            noseboom_range = self._time_state.instruments.get("noseboom")
            noseboom_anchor_active = (
                noseboom_range is not None
                and noseboom_range.available_start is not None
                and noseboom_range.available_end is not None
                and interval_selected
            )
            has_selected_time = (
                not noseboom_anchor_active
                or (
                    coverage is not None
                    and coverage.availability_percentage is not None
                    and coverage.availability_percentage > 0
                    and not coverage.outside_selected_range
                )
            )
            previously_completed = job.status in {
                ProcessingStatus.COMPLETE,
                ProcessingStatus.WARNING,
            }
            ready = (
                integrated
                and _instrument_is_processable(state)
                and has_selected_time
                and not previously_completed
            )
            if ready:
                selectable_job_ids.add(job.job_id)
            payload["available_for_selection"] = ready
            payload["previously_completed"] = previously_completed
            payload["skip_by_default"] = previously_completed
            payload["selection_reason"] = (
                "Previously processed; skipped by default. Use Reprocess only "
                "when you intentionally want to replace this result."
                if previously_completed else
                "Ready for processing" if ready and state.detection_status is DetectionStatus.READY else
                "Available with scan warning; review during health check" if ready else

                "Configure the available FLIR Level 2 routines to create "
                "temperature plots and the Noseboom-matched map"
                if job.job_id == "flir_detailed"
                and _instrument_is_processable(state)
                and has_selected_time else
                "Scientific adapter is not integrated" if not integrated else
                "Resolve ambiguous input data" if state.ambiguous else
                "UTC data do not overlap the selected Noseboom flight interval"
                if _instrument_is_processable(state) and not has_selected_time else
                f"Scan health: {state.detection_status.value.replace('_', ' ')}"
            )
            job_payload.append(payload)
        selected = [
            job for job in jobs
            if job.enabled and not job.detailed and job.job_id in selectable_job_ids
        ]
        busy_domains = self._busy_domains()
        busy = bool(busy_domains)
        # What matters for starting is whether the half being started is busy,
        # not whether anything anywhere is running.
        selected_domains = {self._job_domain(job) for job in selected}
        start_blocked = bool(selected_domains & busy_domains)
        interval_ready = (
            self._time_state.selected_analysis_start is not None
            and self._time_state.selected_analysis_end is not None
        )
        scan_ready = self._report is not None and self._phase in {"complete", "scanning_camera"}
        can_start = bool(
            selected
            and not start_blocked
            and interval_ready
            and scan_ready
        )
        return {
            "jobs": job_payload,
            "busy": busy,
            "busy_domains": sorted(busy_domains),
            "camera_busy": "camera" in busy_domains,
            "flight_busy": "flight" in busy_domains,
            "start_blocked": start_blocked,
            "selected_count": len(selected),
            "can_start": can_start,
            "workflow": {
                "scan_ready": scan_ready,
                "interval_ready": interval_ready,
                "selection_required": not selected,
                "next_step": (
                    "Wait for the selected instruments to finish" if start_blocked else
                    "Select at least one instrument" if not selected else
                    "Finish flight scanning and choose the global Time Filter" if not (scan_ready and interval_ready) else
                    "Ready to start; an Output Folder will be requested" if self._selected_output_folder is None else
                    "Ready to start processing"
                ),
            },
            "worker_groups": {
                group: [job.job_id for job in jobs if job.worker_group.value == group]
                for group in (
                    "fast_science",
                    "camera_metadata",
                    "camera_detailed",
                )
            },
        }

    def _sync_project_queue_state(self) -> None:
        if self._flight_project is None:
            return
        jobs = self.processing_queue.ordered()
        self._flight_project.processing_priority = [job.job_id for job in jobs]
        self._flight_project.enabled_instruments = list(
            dict.fromkeys(job.instrument_id for job in jobs if job.enabled)
        )
        self._flight_project.completed_jobs = [
            job.job_id for job in jobs if job.status.value in {"complete", "warning"}
        ]
        self._flight_project.failed_jobs = [
            job.job_id for job in jobs if job.status.value == "failed"
        ]
        self._flight_project.cancelled_jobs = [
            job.job_id for job in jobs if job.status.value == "cancelled"
        ]

    def _instrument_processing_interval(
        self, instrument_id: str
    ) -> tuple[datetime | None, datetime | None]:
        """Intersect the governing interval with an instrument's availability.

        Flight instruments follow the dashboard Time Filter. The remote-sensing
        products follow the interval chosen in the Remote Sensing dialog against
        camera coverage, which is a separate selection: the cameras cover a
        different span from the flight instruments and are run on their own.
        """
        if instrument_id in CAMERA_INSTRUMENTS and self._camera_selected_start:
            selected_start = self._camera_selected_start
            selected_end = self._camera_selected_end
        else:
            selected_start = self._time_state.selected_analysis_start
            selected_end = self._time_state.selected_analysis_end
        selection = self._time_state.instruments.get(instrument_id)
        if selection is not None:
            if selection.available_start is not None:
                selected_start = max(selected_start, selection.available_start) if selected_start else selection.available_start
            if selection.available_end is not None:
                selected_end = min(selected_end, selection.available_end) if selected_end else selection.available_end
        if selected_start is not None and selected_end is not None and selected_start >= selected_end:
            # Naming the two intervals turns an unexplained refusal into
            # something the operator can act on — most often the delivery is
            # from a different day than the flight.
            display_name = self._instruments[instrument_id].display_name
            raise RuntimeError(
                f"The selected Time Filter "
                f"({_iso(self._time_state.selected_analysis_start)} to "
                f"{_iso(self._time_state.selected_analysis_end)} UTC) does not "
                f"overlap the {display_name} data "
                f"({_iso(selection.raw_start) if selection else None} to "
                f"{_iso(selection.raw_end) if selection else None} UTC). "
                f"Change the Time Filter, or select the {display_name} delivery "
                "recorded during this flight."
            )
        return selected_start, selected_end

    @staticmethod
    def _run_output_root(project: FlightProject, instrument_id: str) -> Path:
        """Return an immutable per-attempt output folder so retries never collide."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        return project.flight_output_root / "processed" / instrument_id / "runs" / stamp
    def _noseboom_task(self, context: ProcessingContext) -> JobOutcome | None:
        from instruments.noseboom import NoseboomAdapter

        with self._lock:
            report = self._report
            project = self._flight_project
            selected_start = self._time_state.selected_analysis_start
            selected_end = self._time_state.selected_analysis_end
            rack_bridge = self._miro_rack_bridge
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        candidates = [
            candidate for candidate in report.candidates
            if candidate.instrument_id == "noseboom"
        ]
        paths = tuple(
            dict.fromkeys(
                path
                for candidate in candidates
                for path in candidate.all_matching_files
            )
        )
        if not paths:
            raise RuntimeError("No selected Noseboom source files are available")
        if selected_start is None or selected_end is None:
            raise RuntimeError("A selected Noseboom analysis interval is required")
        estimated_peak = 256 * 1024 * 1024
        self.resource_manager.admit_task(
            estimated_peak,
            self._resource_limits,
            task_name="Noseboom selected-interval streaming load",
        )
        adapter = NoseboomAdapter(
            output_root=self._run_output_root(project, "noseboom"),
            flight_name=project.flight_id,
            logger=self.logger,
        )
        adapter.report_progress(
            lambda update: context.report_progress(
                update.progress or 0.0, update.phase
            )
        )
        loaded = adapter.load_time_window(
            InputCandidate(
                instrument_id="noseboom",
                paths=paths,
                confidence=1.0,
                reason="Confirmed Flight Folder scan candidate",
            ),
            selected_start,
            selected_end,
        )
        result = adapter.process_quicklook(
            loaded,
            {
                "analysis_start": selected_start,
                "analysis_end": selected_end,
                "trim_minutes": 2.0,
                "terrain": True,
                "straight_settings": dict(self._noseboom_straight_settings),
            },
        )
        outputs = adapter.export_results(
            result, adapter.output_root, ("csv",)
        )
        quicklook = dict(result.metadata.get("map", {}))
        quicklook_path = project.flight_output_root / "quicklooks" / "noseboom_browser.json"
        quicklook_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = quicklook_path.with_suffix(".json.temporary")
        temporary.write_text(
            json.dumps(quicklook, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(quicklook_path)
        with self._lock:
            state = self._instruments["noseboom"]
            state.output_files = [str(value.path) for value in outputs]
            state.quicklook = quicklook
            saved_state = project.detected_instruments.get("noseboom")
            if saved_state is not None:
                saved_state.output_locations = [
                    *(Path(value.path) for value in outputs),
                    quicklook_path,
                ]
            project.output_locations["noseboom_quicklook"] = quicklook_path
            project.output_locations["processing_log"] = (
                project.flight_output_root / "logs" / "processing.jsonl"
            )
        self.logger.log(
            LogLevel.SUCCESS,
            "noseboom-browser",
            "Noseboom browser state saved for project reload",
            instrument="noseboom",
            file_path=quicklook_path,
            processing_step="browser-state-save",
        )
        if rack_bridge is not None and hasattr(
            rack_bridge, "prepare_map_during_main_processing"
        ):
            context.report_progress(
                99.0, "Preparing saved MIRO/Picarro Mapview navigation"
            )
            rack_bridge.prepare_map_during_main_processing()
        self._persist_project_logs()
        return JobOutcome(warning=result.warnings[0] if result.warnings else None)

    def _miro_task(self, context: ProcessingContext) -> JobOutcome | None:
        from instruments.miro import MiroAdapter

        with self._lock:
            report = self._report
            project = self._flight_project
            selected_start, selected_end = self._instrument_processing_interval("miro")
            rack_bridge = self._miro_rack_bridge
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        candidates = [
            candidate for candidate in report.candidates
            if candidate.instrument_id == "miro"
        ]
        paths = tuple(
            dict.fromkeys(
                path
                for candidate in candidates
                for path in candidate.all_matching_files
            )
        )
        if not paths:
            raise RuntimeError("No selected MIRO source files are available")
        paths = self._deliveries_for_interval(
            "miro", paths, selected_start, selected_end
        )
        estimated_peak = max(
            sum(path.stat().st_size for path in paths) * 8,
            256 * 1024 * 1024,
        )
        self.resource_manager.admit_task(
            estimated_peak,
            self._resource_limits,
            task_name="MIRO legacy text load and analysis",
        )
        output_root = self._run_output_root(project, "miro")
        adapter = MiroAdapter(
            output_root=output_root,
            flight_name=project.flight_id,
            logger=self.logger,
        )
        adapter.report_progress(
            lambda update: context.report_progress(
                min(78.0, (update.progress or 0.0) * 0.78), update.phase
            )
        )
        loaded = adapter.load(
            InputCandidate(
                instrument_id="miro",
                paths=paths,
                confidence=1.0,
                reason="Confirmed Flight Folder scan candidate",
            )
        )
        result = adapter.process_quicklook(
            loaded,
            {
                "analysis_start": selected_start,
                "analysis_end": selected_end,
                "gas": "NO2 wet",
                "smooth_seconds": 300.0,
                "remove_seconds": 30.0,
            },
        )
        figures = adapter.create_plots(result, adapter.output_root)
        rack_outputs: list[str] = []
        if rack_bridge is not None and hasattr(
            rack_bridge, "publish_main_processing_instrument"
        ):
            context.report_progress(
                80.0,
                "Preparing original-resolution MIRO Rack results and 1 Hz Mapview copy",
            )
            published = rack_bridge.publish_main_processing_instrument(
                instrument_id="miro",
                data=loaded.data,
                metadata=loaded.load_metadata,
                analysis=result.metadata.get("analysis"),
                source_paths=paths,
                selected_start=selected_start,
                selected_end=selected_end,
                output_root=output_root,
                progress_callback=lambda fraction, message: context.report_progress(
                    80.0 + min(1.0, max(0.0, float(fraction))) * 16.0,
                    message,
                ),
            )
            rack_outputs = [str(value) for value in published.get("outputs", ())]
            if published.get("project_saved"):
                context.report_progress(
                    99.0, "MIRO Rack project saved; finalizing MIRO processing"
                )
        with self._lock:
            state = self._instruments["miro"]
            state.output_files = [
                *(str(value.path) for value in figures),
                *rack_outputs,
            ]
        context.report_progress(100.0, "MIRO processing complete")
        return JobOutcome(warning=result.warnings[0] if result.warnings else None)

    def _picarro_task(self, context: ProcessingContext) -> JobOutcome | None:
        with self._lock:
            report = self._report
            project = self._flight_project
            selected_start, selected_end = self._instrument_processing_interval(
                "picarro"
            )
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        rack_bridge = self._require_miro_rack_bridge()
        if not hasattr(rack_bridge, "process_picarro_from_main"):
            raise RuntimeError(
                "The loaded MIRO Rack application does not expose Picarro "
                "processing; check legacy_integration/MIRO_Rack."
            )
        paths = tuple(
            dict.fromkeys(
                path
                for candidate in report.candidates
                if candidate.instrument_id == "picarro"
                for path in candidate.all_matching_files
            )
        )
        if not paths:
            raise RuntimeError("No selected Picarro source files are available")
        paths = self._deliveries_for_interval(
            "picarro", paths, selected_start, selected_end
        )
        output_root = self._run_output_root(project, "picarro")
        result = rack_bridge.process_picarro_from_main(
            source_paths=paths,
            selected_start=selected_start,
            selected_end=selected_end,
            output_root=output_root,
            progress_callback=lambda percent, message: context.report_progress(
                min(99.0, max(0.0, float(percent))), message
            ),
        )
        with self._lock:
            self._instruments["picarro"].output_files = [
                str(value) for value in result.get("outputs", ())
            ]
        context.report_progress(100.0, "Picarro processing complete")
        warnings = list(result.get("warnings", ()))
        return JobOutcome(warning=warnings[0] if warnings else None)
    def _opc_task(
        self, instrument_id: str, context: ProcessingContext
    ) -> JobOutcome | None:
        if instrument_id == "opc_hbx4":
            from instruments.opc_hbx4 import OpcHbx4Adapter as Adapter
        elif instrument_id == "opc_hbx5":
            from instruments.opc_hbx5 import OpcHbx5Adapter as Adapter
        else:
            raise ValueError(f"Unknown OPC instrument: {instrument_id}")

        with self._lock:
            report = self._report
            project = self._flight_project
            scan_state = self._instruments[instrument_id]
            assigned_candidates = tuple(scan_state.candidate_paths)
            selected_start, selected_end = self._instrument_processing_interval(instrument_id)
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        explicitly_assigned_files = tuple(
            Path(value) for value in assigned_candidates
            if Path(value).is_file()
        )
        if explicitly_assigned_files:
            paths = explicitly_assigned_files
        else:
            assigned_roots = {
                Path(value).resolve(strict=False) for value in assigned_candidates
            }
            paths = tuple(
                dict.fromkeys(
                    path
                    for candidate in report.candidates
                    if candidate.instrument_id == instrument_id
                    and (
                        not assigned_roots
                        or candidate.candidate_path.resolve(strict=False)
                        in assigned_roots
                    )
                    for path in candidate.all_matching_files
                )
            )
        if len(paths) != 1:
            raise RuntimeError(
                f"{instrument_id} requires exactly one confirmed source CSV"
            )
        self.resource_manager.admit_task(
            max(paths[0].stat().st_size * 12, 128 * 1024 * 1024),
            self._resource_limits,
            task_name=f"{instrument_id} legacy CSV evaluation",
        )
        adapter = Adapter(
            output_root=self._run_output_root(project, instrument_id),
            flight_name=project.flight_id,
            logger=self.logger,
        )
        adapter.report_progress(
            lambda update: context.report_progress(
                update.progress or 0.0, update.phase
            )
        )
        loaded = adapter.load(
            InputCandidate(
                instrument_id,
                paths,
                1.0,
                "Confirmed Flight Folder scan candidate",
            )
        )
        result = adapter.process_quicklook(
            loaded,
            {
                "analysis_start": selected_start,
                "analysis_end": selected_end,
                "gap_seconds": 10.0,
                "bin_units": "auto",
            },
        )
        figures = adapter.create_plots(result, adapter.output_root)
        adapter.export_results(result, adapter.output_root, ("csv", "json"))
        browser = adapter.export_browser_data(result, adapter.output_root)
        with self._lock:
            state = self._instruments[instrument_id]
            state.output_files = [
                str(value.path) for value in (*figures, *result.output_files)
            ]
            saved = project.detected_instruments.get(instrument_id)
            if saved is not None:
                saved.output_locations = [Path(value) for value in state.output_files]
        self._publish_hatchbox_browser(project, instrument_id, browser.path)
        self._refresh_opc_combined_browser(project)
        return JobOutcome(warning=result.warnings[0] if result.warnings else None)

    def _partector_task(
        self, context: ProcessingContext
    ) -> JobOutcome | None:
        from instruments.partector import PartectorAdapter

        with self._lock:
            report = self._report
            project = self._flight_project
            scan_state = self._instruments["partector"]
            assigned_candidates = tuple(scan_state.candidate_paths)
            selected_start, selected_end = self._instrument_processing_interval("partector")
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        assigned_roots = {
            Path(value).resolve(strict=False) for value in assigned_candidates
        }
        explicit_files = tuple(
            Path(value) for value in assigned_candidates if Path(value).is_file()
        )
        paths = explicit_files or tuple(
            dict.fromkeys(
                path
                for candidate in report.candidates
                if candidate.instrument_id == "partector"
                and (
                    not assigned_roots
                    or candidate.candidate_path.resolve(strict=False)
                    in assigned_roots
                )
                for path in candidate.all_matching_files
            )
        )
        if len(paths) != 1:
            raise RuntimeError(
                "Partector Pro requires exactly one confirmed source CSV"
            )
        self.resource_manager.admit_task(
            max(paths[0].stat().st_size * 15, 192 * 1024 * 1024),
            self._resource_limits,
            task_name="Partector Pro legacy CSV quicklook",
        )
        adapter = PartectorAdapter(
            output_root=self._run_output_root(project, "partector"),
            flight_name=project.flight_id,
            logger=self.logger,
        )
        adapter.report_progress(
            lambda update: context.report_progress(
                update.progress or 0.0, update.phase
            )
        )
        loaded = adapter.load(
            InputCandidate(
                "partector",
                paths,
                1.0,
                "Confirmed Flight Folder scan candidate",
            )
        )
        result = adapter.process_quicklook(
            loaded,
            {
                "analysis_start": selected_start,
                "analysis_end": selected_end,
                "session": "all",
                "session_gap_minutes": 10.0,
                "trim_start_minutes": 0.0,
                "flow_min_lpm": 0.45,
                "flow_max_lpm": 0.55,
            },
        )
        figures = adapter.create_plots(result, adapter.output_root)
        adapter.export_results(result, adapter.output_root, ("csv", "json", "md"))
        browser = adapter.export_browser_data(result, adapter.output_root)
        with self._lock:
            state = self._instruments["partector"]
            state.output_files = [
                str(value.path) for value in (*figures, *result.output_files)
            ]
            saved = project.detected_instruments.get("partector")
            if saved is not None:
                saved.output_locations = [Path(value) for value in state.output_files]
        self._publish_hatchbox_browser(project, "partector", browser.path)
        return JobOutcome(warning=result.warnings[0] if result.warnings else None)

    def _ins_gimbal_task(
        self, context: ProcessingContext
    ) -> JobOutcome | None:
        from instruments.ins_gimbal import InsGimbalAdapter

        with self._lock:
            report, project = self._report, self._flight_project
            start, end = self._instrument_processing_interval("ins_gimbal")
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        paths = tuple(dict.fromkeys(
            path for candidate in report.candidates
            if candidate.instrument_id == "ins_gimbal"
            for path in candidate.all_matching_files
        ))
        if len(paths) != 1:
            raise RuntimeError("INS Gimbal requires one confirmed source CSV")
        self.resource_manager.admit_task(
            max(paths[0].stat().st_size * 15, 192 * 1024 * 1024),
            self._resource_limits, task_name="INS Gimbal quicklook",
        )
        adapter = InsGimbalAdapter(
            output_root=self._run_output_root(project, "ins_gimbal"),
            flight_name=project.flight_id, logger=self.logger,
        )
        adapter.report_progress(lambda update: context.report_progress(
            update.progress or 0.0, update.phase
        ))
        loaded = adapter.load(InputCandidate(
            "ins_gimbal", paths, 1.0, "Confirmed scan candidate"
        ))
        result = adapter.process_quicklook(loaded, {
            "analysis_start": start, "analysis_end": end,
            "gap_seconds": 10.0, "rms_seconds": 30.0,
            "maneuver_threshold_dps": 10.0,
        })
        figures = adapter.create_plots(result, adapter.output_root)
        outputs = adapter.export_results(
            result, adapter.output_root, ("csv", "json", "md")
        )
        browser = adapter.export_browser_data(result, adapter.output_root)
        with self._lock:
            state = self._instruments["ins_gimbal"]
            state.output_files = [
                str(value.path) for value in (*figures, *outputs, browser)
            ]
            saved = project.detected_instruments.get("ins_gimbal")
            if saved is not None:
                saved.output_locations = [Path(value) for value in state.output_files]
        self._publish_hatchbox_browser(project, "ins_gimbal", browser.path)
        return JobOutcome()

    def _sif_task(self, context: ProcessingContext) -> JobOutcome | None:
        from instruments.sif import SifAdapter
        with self._lock:
            report, project = self._report, self._flight_project
            start, end = self._instrument_processing_interval("sif")
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        paths = tuple(dict.fromkeys(
            path for candidate in report.candidates if candidate.instrument_id == "sif"
            for path in candidate.all_matching_files
        ))
        if not paths: raise RuntimeError("No selected SIF raw files are available")
        paths = self._deliveries_for_interval("sif", paths, start, end)
        self.resource_manager.admit_task(
            max(sum(path.stat().st_size for path in paths) * 12, 512 * 1024 * 1024),
            self._resource_limits, task_name="AirFloX SIF spectral processing",
        )
        adapter = SifAdapter(
            output_root=self._run_output_root(project, "sif"),
            flight_name=project.flight_id, logger=self.logger,
        )
        adapter.report_progress(lambda update: context.report_progress(update.progress or 0.0, update.phase))
        loaded = adapter.load(InputCandidate("sif", paths, 1.0, "Confirmed scan candidate"))
        with self._lock:
            sif_options = dict(self._sif_options)
        result = adapter.process_quicklook(loaded, {
            "analysis_start": start, "analysis_end": end,
            "flight_root": project.flight_folder_path,
            **sif_options,
        })
        outputs = adapter.export_results(result, adapter.output_root, ("csv", "gis"))
        adapter.create_plots(result, adapter.output_root)
        browser = adapter.export_browser_data(result, adapter.output_root)
        with self._lock:
            state = self._instruments["sif"]
            state.output_files = [
                str(value.path) for value in (*outputs, browser)
            ]
            state.quicklook = json.loads(browser.path.read_text(encoding="utf-8"))
            saved = project.detected_instruments.get("sif")
            if saved is not None:
                saved.output_locations = [
                    Path(value) for value in state.output_files
                ]
            project.instrument_options["sif"] = dict(sif_options)
        self._publish_hatchbox_browser(project, "sif", browser.path)
        return JobOutcome(warning=result.warnings[0] if result.warnings else None)

    def _micasense_quick_task(self, context: ProcessingContext) -> JobOutcome | None:
        """Run the bounded Level 1 adapter in the camera-metadata worker group."""
        with self._lock:
            report, project = self._report, self._flight_project
            limits = self._resource_limits
            selected_start, selected_end = self._instrument_processing_interval(
                "micasense"
            )
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        candidates = [
            item for item in report.candidates if item.instrument_id == "micasense"
        ]
        roots = tuple(dict.fromkeys(item.candidate_path for item in candidates))
        paths: list[Path] = []
        archives: list[Path] = []
        for root in roots:
            context.check_cancelled()
            if root.is_dir():
                paths.extend(
                    path
                    for path in root.rglob("*")
                    if path.is_file() and path.suffix.casefold() in {".tif", ".tiff"}
                )
                archives.extend(
                    path for path in root.rglob("*.zip") if path.is_file()
                )
            elif root.suffix.casefold() in {".tif", ".tiff"}:
                paths.append(root)
            elif root.suffix.casefold() == ".zip":
                archives.append(root)
        for item in candidates:
            paths.extend(
                path
                for path in item.all_matching_files
                if path.suffix.casefold() in {".tif", ".tiff"} and path.is_file()
            )
            archives.extend(
                path for path in item.all_matching_files
                if path.suffix.casefold() == ".zip" and path.is_file()
            )
        paths = list(dict.fromkeys((*paths, *archives)))
        if not paths:
            raise RuntimeError("No MicaSense TIFF images are available")
        adapter = MicaSenseLevel1Adapter(
            output_root=project.flight_output_root / "processed" / "micasense",
            flight_name=project.flight_id,
            resource_limits=limits,
            batch_policy=CameraBatchPolicy(
                maximum_batch_files=max(1, min(32, len(paths))),
                maximum_thumbnail_count=24,
            ),
            logger=self.logger,
        )
        adapter.report_progress(
            lambda update: context.report_progress(
                update.progress or 0.0, update.phase
            )
        )
        loaded = adapter.load(
            InputCandidate(
                "micasense",
                tuple(paths),
                1.0,
                "Confirmed Flight Folder scan candidate",
            )
        )
        result = adapter.process_quicklook(
            loaded,
            {
                "analysis_start": selected_start,
                "analysis_end": selected_end,
            },
        )
        outputs = adapter.export_results(
            result, adapter.output_root, ("csv", "json")
        )
        adapter.create_plots(result, adapter.output_root)
        with self._lock:
            state = self._instruments["micasense"]
            state.output_files = [
                str(value.path) for value in outputs
            ] + [str(value.path) for value in result.figures]
        return JobOutcome(warning=result.warnings[0] if result.warnings else None)

    def _flir_quick_task(self, context: ProcessingContext) -> JobOutcome | None:
        """Run FLIR Level 1 without invoking any temperature conversion."""
        with self._lock:
            report, project = self._report, self._flight_project
            limits = self._resource_limits
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        selected_start, selected_end = self._instrument_processing_interval(
            "flir"
        )
        context.check_cancelled()
        context.report_progress(1, "Selecting the FLIR export covering the interval")
        paths = list(
            self._flir_exports_for_interval(
                report, selected_start, selected_end, level="level1"
            )
        )
        adapter = FlirLevel1Adapter(
            output_root=self._run_output_root(project, "flir"),
            flight_name=project.flight_id,
            resource_limits=limits,
            batch_policy=CameraBatchPolicy(
                maximum_batch_files=max(1, min(8, len(paths))),
                maximum_thumbnail_count=12,
            ),
            logger=self.logger,
        )
        adapter.report_progress(
            lambda update: context.report_progress(update.progress or 0.0, update.phase)
        )
        loaded = adapter.load(InputCandidate(
            "flir", tuple(paths), 1.0, "Confirmed Flight Folder scan candidate"
        ))
        result = adapter.process_quicklook(
            loaded,
            {
                "analysis_start": selected_start,
                "analysis_end": selected_end,
            },
        )
        outputs = adapter.export_results(result, adapter.output_root, ("csv", "json"))
        figures = adapter.create_plots(result, adapter.output_root)
        browser_payload = {
            "available": True,
            "temperature_available": False,
            "temperature_reason": (
                "Run confirmed FLIR Level 2 radiometric temperature conversion "
                "to activate the land-surface-temperature map."
            ),
            "temperature_interpretation": (
                "FLIR Planck apparent temperature; atmospheric, emissivity, "
                "distance, and external-optics corrections are not applied."
            ),
            "utc_start": _iso(result.utc_start_time),
            "utc_end": _iso(result.utc_end_time),
            "summary": dict(result.metadata),
            "acquisition_intervals_seconds": list(
                result.metadata.get("acquisition_intervals_seconds", ())
            ),
            "gaps": list(adapter.acquisition_gaps()),
            "samples": list(adapter.sample_records()),
            "thumbnails": [
                {
                    "name": value.path.name,
                    "url": f"/api/flir/asset/{value.path.name}",
                    "caption": value.title,
                }
                for value in figures
            ],
            "temperature_records": [],
            "map_points": [],
            "warnings": list(result.warnings),
        }
        quicklook_path = (
            project.flight_output_root / "quicklooks" / "flir_browser.json"
        )
        quicklook_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = quicklook_path.with_suffix(".json.temporary")
        temporary.write_text(
            json.dumps(
                browser_payload,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(quicklook_path)
        with self._lock:
            state = self._instruments["flir"]
            state.output_files = [
                str(value.path) for value in outputs
            ] + [str(value.path) for value in figures] + [str(quicklook_path)]
            state.quicklook = browser_payload
            saved = project.detected_instruments.get("flir")
            if saved is not None:
                saved.output_locations = [
                    Path(value) for value in state.output_files
                ]
            project.output_locations["flir_browser"] = quicklook_path
        # Metadata and georeferencing are one run. Splitting them left the map
        # view waiting on a second job the operator had to find and start, and a
        # FLIR delivery is no use without positions. A failure here is reported
        # as a warning rather than losing the metadata that already succeeded.
        warning = result.warnings[0] if result.warnings else None
        try:
            context.report_progress(
                55, "Converting temperature and matching Noseboom navigation"
            )
            self._flir_detailed_task(context, self._flir_level2_routines())
        except ProcessingCancelledError:
            raise
        except Exception as exc:
            self.logger.capture_exception(
                exc,
                component="flir",
                message=(
                    "FLIR temperature conversion and georeferencing did not "
                    "complete; the acquisition metadata was still written."
                ),
                instrument="flir",
                processing_step="georeferencing",
            )
            warning = (
                "Temperature conversion and georeferencing did not complete: "
                f"{exc}"
            )
        return JobOutcome(warning=warning)

    def _flir_level2_routines(self) -> tuple[str, ...]:
        """Every available routine. There is nothing useful to leave out."""
        from core.camera_level2 import level2_capability_snapshot

        return tuple(
            item["routine_id"]
            for item in level2_capability_snapshot().get("flir", ())
            if item.get("available")
        )

    # Per-file coverage is only worth measuring when a delivery is split across
    # files and the whole set is small enough to inspect cheaply. A single file
    # is used as-is, and a very large set is left alone rather than read twice.
    DELIVERY_SELECTION_MAX_FILES = 64
    DELIVERY_SELECTION_MAX_BYTES = 2 * 1024 * 1024 * 1024

    def _deliveries_for_interval(
        self,
        instrument_id: str,
        paths: Sequence[Path],
        selected_start: datetime | None,
        selected_end: datetime | None,
    ) -> tuple[Path, ...]:
        """Drop source files whose own UTC coverage misses the selected interval.

        A campaign folder can hold more than one delivery for the same
        instrument — an earlier flight, a bench run, a re-export. Every adapter
        already filters record by record, so this changes no science; it stops
        unrelated files being read at all and, more importantly, stops a
        wrong-day delivery quietly widening an instrument's reported coverage.

        Anything that cannot be judged is kept. Being unable to read a file's
        timestamps is not evidence that it belongs to a different flight.
        """
        paths = tuple(paths)
        if (
            len(paths) < 2
            or selected_start is None
            or selected_end is None
            or len(paths) > self.DELIVERY_SELECTION_MAX_FILES
        ):
            return paths
        try:
            total = sum(path.stat().st_size for path in paths)
        except OSError:
            return paths
        if total > self.DELIVERY_SELECTION_MAX_BYTES:
            return paths

        extractor = TimestampExtractor()
        kept: list[Path] = []
        dropped: list[str] = []
        for path in paths:
            coverage = extractor.extract_instrument(instrument_id, (path,))
            start, end = coverage.utc_start_time, coverage.utc_end_time
            if start is None or end is None:
                kept.append(path)
                continue
            if end < selected_start or start > selected_end:
                dropped.append(
                    f"{path.name} ({_iso(start)} to {_iso(end)} UTC)"
                )
                continue
            kept.append(path)
        if not kept:
            raise RuntimeError(
                f"No {instrument_id} source file covers the selected Time Filter "
                f"({_iso(selected_start)} to {_iso(selected_end)} UTC). "
                "Found: " + "; ".join(dropped)
            )
        if dropped:
            self.logger.log(
                LogLevel.WARNING,
                "delivery-selection",
                (
                    f"Excluded {len(dropped)} {instrument_id} source file(s) "
                    "outside the selected Time Filter: " + "; ".join(dropped)
                ),
                instrument=instrument_id,
                processing_step="delivery-selection",
            )
        return tuple(kept)

    def update_status(self, *, refresh: bool = False) -> dict[str, object]:
        """Report whether a newer release exists, using a cached answer.

        The check runs at most once per launch unless explicitly refreshed, so
        opening the dialog does not contact the server again. Nothing is ever
        downloaded; the operator decides when to update.
        """
        with self._lock:
            cached = self._update_status
        if cached is not None and not refresh:
            return cached.to_dict()
        status = check_for_update(current_version=SOFTWARE_VERSION)
        with self._lock:
            self._update_status = status
        if status.update_available:
            self.logger.log(
                LogLevel.INFO, "software-update",
                (
                    f"Version {status.latest_version} is available; "
                    f"this installation is {status.current_version}. "
                    f"Download: {status.download_url}"
                ),
                processing_step="update-check",
            )
        elif status.checked and not status.enabled:
            pass
        return status.to_dict()

    def start_background_update_check(self) -> None:
        """Look for a newer release without delaying startup.

        Runs on a daemon thread so a slow or unreachable network cannot hold up
        the dashboard, and swallows every failure — an update check is never a
        reason for the application not to work.
        """
        from core.update_check import update_check_enabled

        if not update_check_enabled():
            return

        def run() -> None:
            try:
                self.update_status()
            except Exception:
                pass

        threading.Thread(
            target=run, name="ccflux-update-check", daemon=True
        ).start()

    def flir_exports(self) -> list[dict[str, object]]:
        """Downloadable FLIR products, newest run first.

        Environment correction is applied downstream, so the export must carry
        everything that calculation needs: raw DN statistics, all ten
        calibration constants, the matched Noseboom position, and the mode and
        provenance under which the temperatures were produced.
        """
        with self._lock:
            files = [Path(value) for value in self._instruments["flir"].output_files]
        described = {
            "temperature_frames.csv": (
                "Per-frame temperature and raw DN statistics, calibration "
                "constants, matched Noseboom position — the post-processing table"
            ),
            "frame_health.csv": "Header health for every frame in the export",
            "acquisition_gaps.csv": "Acquisition gaps beyond the threshold",
            "timestamp_index.csv": "Byte-offset index for fast re-runs",
            "summary.json": "Acquisition and processing summary",
            "flir_browser.json": "Saved workspace for this page",
        }
        exports: list[dict[str, object]] = []
        seen: set[str] = set()
        for path in reversed(files):
            if path.name in seen or not path.is_file():
                continue
            if path.suffix.casefold() not in {".csv", ".json"}:
                continue
            seen.add(path.name)
            exports.append({
                "name": path.name,
                "description": described.get(path.name, "FLIR product"),
                "size_bytes": path.stat().st_size,
                "url": f"/api/flir/asset/{path.name}?download=1",
            })
        return exports

    def update_flir_level2_options(
        self, request: Mapping[str, object] | None
    ) -> dict[str, object]:
        """Validate the operator's radiometric correction inputs.

        Refused outright on a worker computer: the settings came with the work
        package and every machine must use the same ones.

        Apparent mode needs nothing. Corrected mode needs every environment
        measurement, and is only quantitative when the operator states the
        values were measured rather than assumed.
        """
        self._refuse_if_worker("The FLIR configuration")
        request = request or {}
        options = dict(DEFAULT_FLIR_LEVEL2_OPTIONS)
        mode = str(request.get("mode", options["mode"])).strip().lower()
        if mode not in {"apparent", "corrected"}:
            raise ValueError("FLIR temperature mode must be apparent or corrected")
        options["mode"] = mode
        provenance = str(
            request.get("environment_inputs_provenance",
                        options["environment_inputs_provenance"])
        ).strip().lower()
        if provenance not in {"measured", "assumed_for_testing"}:
            raise ValueError(
                "Environment input provenance must be measured or assumed_for_testing"
            )
        options["environment_inputs_provenance"] = provenance

        numeric = {
            "emissivity": (0.0, 1.0, False),
            "object_distance_m": (0.0, 100_000.0, True),
            "atmospheric_temperature_c": (-273.15, 200.0, False),
            "reflected_apparent_temperature_c": (-273.15, 200.0, False),
            "relative_humidity_percent": (0.0, 100.0, True),
            "external_optics_transmission": (0.0, 1.0, False),
            "external_optics_temperature_c": (-273.15, 200.0, False),
            "valid_temperature_min_c": (-273.15, 2000.0, True),
            "valid_temperature_max_c": (-273.15, 2000.0, True),
        }
        for name, (low, high, inclusive_low) in numeric.items():
            if request.get(name) is None:
                continue
            value = _optional_float(request.get(name))
            if value is None:
                continue
            within = low <= value <= high if inclusive_low else low < value <= high
            if not within:
                raise ValueError(
                    f"{name} must be between {low} and {high}"
                )
            options[name] = value
        options["save_temperature_npz"] = request.get("save_temperature_npz") is True

        if (options["valid_temperature_min_c"] is None) != (
            options["valid_temperature_max_c"] is None
        ):
            raise ValueError(
                "Provide both valid temperature limits, or neither"
            )
        if mode == "corrected":
            missing = [
                name for name in (
                    "emissivity", "object_distance_m", "atmospheric_temperature_c",
                    "reflected_apparent_temperature_c", "relative_humidity_percent",
                ) if options[name] is None
            ]
            if missing:
                raise ValueError(
                    "Environment-corrected temperature needs measured values for: "
                    + ", ".join(missing)
                )
        with self._lock:
            self._flir_level2_options = options
            if self._flight_project is not None:
                self._flight_project.instrument_options["flir_level2"] = dict(options)
        self.logger.log(
            LogLevel.INFO, "camera-level2",
            f"FLIR Level 2 options saved: mode={mode}, provenance={provenance}",
            instrument="flir", processing_step="level2-configuration",
        )
        self._checkpoint_project()
        return dict(options)

    def _flir_exports_for_interval(
        self,
        report: ScanReport,
        selected_start: datetime | None,
        selected_end: datetime | None,
        *,
        level: str,
    ) -> tuple[Path, ...]:
        """Choose FLIR exports by UTC coverage rather than by folder position.

        A campaign disk commonly holds several ``camera.FLIR_*.json`` files —
        a bench recording, an earlier flight, the flight under review — under
        the same name. Picking whichever one sits in the selected folder made
        the choice an accident of layout, so a stale export silently produced
        "does not overlap". Coverage is read from each file's first and last
        timestamps, which costs ~32 MiB per file regardless of its size.
        """
        roots: list[Path] = []
        for item in report.candidates:
            if item.instrument_id != "flir":
                continue
            roots.append(item.candidate_path)
            roots.extend(item.all_matching_files)
        exports = discover_flir_exports(
            dict.fromkeys(roots),
            max_workers=max(1, self._resource_limits.worker_count),
        )
        accepted, rejected = select_exports_for_interval(
            exports, selected_start, selected_end
        )
        summary = describe_selection(
            accepted, rejected, selected_start, selected_end
        )
        if not accepted:
            raise RuntimeError(summary)
        self.logger.log(
            LogLevel.WARNING if rejected else LogLevel.INFO,
            "flir-discovery",
            summary,
            instrument="flir",
            processing_step=f"{level}-export-selection",
        )
        return tuple(item.path for item in accepted)

    def _cached_flir_timestamp_index(
        self, index_path: Path, source_paths: list[Path], health_module
    ) -> list | None:
        """Reuse a previous frame index instead of rescanning the export.

        Indexing is a full read of the FLIR JSON stream - about 105 seconds over
        36 GB - and it was repeated on every Level 2 run even when nothing had
        changed. ``write_timestamp_index`` already records each source file's
        size and mtime and ``load_timestamp_index`` refuses an index whose
        sources have moved on, so staleness is handled by the science module.
        What is checked here is that the index covers exactly the export files
        selected now: a different FLIR delivery has its own frames, and reusing
        another one's byte offsets would read from the wrong place.

        Returns the cached entries, or None if the export must be scanned.
        """
        if not index_path.is_file():
            return None
        loader = getattr(health_module, "load_timestamp_index", None)
        if not callable(loader):
            return None
        try:
            entries = loader(index_path)
        except Exception as exc:
            self.logger.log(
                LogLevel.INFO, "camera-level2",
                f"The FLIR timestamp index was not reusable ({exc}); "
                "the export is being indexed again.",
                instrument="flir", processing_step="level2-index",
            )
            return None
        if not entries:
            return None
        wanted = {path.resolve(strict=False) for path in source_paths}
        covered = {Path(entry[2]).resolve(strict=False) for entry in entries}
        if covered != wanted:
            self.logger.log(
                LogLevel.INFO, "camera-level2",
                "The FLIR timestamp index covers a different export than the "
                "one selected; the export is being indexed again.",
                instrument="flir", processing_step="level2-index",
            )
            return None
        try:
            total_bytes = sum(path.stat().st_size for path in source_paths)
        except OSError:
            total_bytes = 0
        self.logger.log(
            LogLevel.INFO, "camera-level2",
            f"Reusing the FLIR timestamp index for {len(entries):,} frames; "
            f"{total_bytes / 1e9:.1f} GB did not have to be read again.",
            instrument="flir", processing_step="level2-index",
        )
        return entries

    def _flir_detailed_task(
        self, context: ProcessingContext, selected_routines: tuple[str, ...]
    ) -> JobOutcome | None:
        """Radiometric temperature plus Noseboom georeferencing for every frame.

        Runs Teledyne FLIR's reference counts2temp calculation over the frames
        inside the operator's Time Filter, then matches each frame to the
        processed Noseboom navigation so the result can be mapped and taken
        into post-processing. The science lives unchanged in
        legacy_integration/FLIR; this only selects, drives and georeferences it.
        """
        from instruments.flir.level2_bridge import (
            APPARENT, CORRECTED, PROVENANCE_ASSUMED, PROVENANCE_MEASURED,
            LegacyFlirLevel2Bridge,
        )

        with self._lock:
            report, project, limits = (
                self._report, self._flight_project, self._resource_limits
            )
            selected_start, selected_end = self._instrument_processing_interval(
                "flir"
            )
            noseboom_points = tuple(
                self._instruments["noseboom"].quicklook.get("points", ())
            )
            options = dict(self._flir_level2_options)
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")

        context.report_progress(1, "Selecting the FLIR export covering the interval")
        paths = list(
            self._flir_exports_for_interval(
                report, selected_start, selected_end, level="level2"
            )
        )
        bridge = LegacyFlirLevel2Bridge()
        health_module = bridge.health
        mode = str(options.get("mode", APPARENT))
        correction = bridge.correction_inputs(options)
        provenance = str(
            options.get("environment_inputs_provenance", PROVENANCE_ASSUMED)
        )
        if mode == CORRECTED and provenance != PROVENANCE_MEASURED:
            # The reference is explicit that guessed inputs are not quantitative.
            self.logger.log(
                LogLevel.WARNING, "camera-level2",
                "Environment-corrected FLIR temperature was requested with "
                "inputs that are not recorded as measured; the result is "
                "marked non-quantitative.",
                instrument="flir", processing_step="level2-provenance",
            )

        output_root = self._run_output_root(project, "flir") / "level2"
        output_root.mkdir(parents=True, exist_ok=True)

        index_path = output_root / "timestamp_index.csv"
        source_paths = [Path(value) for value in paths]
        entries = self._cached_flir_timestamp_index(
            index_path, source_paths, health_module
        )
        if entries is None:
            context.report_progress(4, "Indexing frame timestamps")
            entries, _ = health_module.scan_timestamps(
                source_paths,
                max(1, limits.worker_count),
                8,
            )
            if not entries:
                raise RuntimeError("No valid FLIR frame timestamps were found")
            health_module.write_timestamp_index(index_path, entries)
        else:
            context.report_progress(
                4, f"Reusing the timestamp index for {len(entries):,} frames"
            )
        context.check_cancelled()

        context.report_progress(18, f"Reading headers for {len(entries):,} frames")
        health, _ = health_module.inspect_all_headers(
            entries, max(1, limits.worker_count)
        )
        summary, gaps = health_module.acquisition_summary(health, None, None)
        health_module.write_csv(output_root / "frame_health.csv", health)
        health_module.write_csv(output_root / "acquisition_gaps.csv", gaps)
        context.check_cancelled()

        indices = health_module.select_indices(
            entries, health, selected_start, selected_end, 1, False
        )
        if not indices:
            raise RuntimeError(
                _flir_selection_failure_message(
                    len(entries),
                    self._time_state.instruments.get("flir"),
                    selected_start,
                    selected_end,
                )
            )
        spans = health_module.object_spans(entries)
        valid_range = None
        if options.get("valid_temperature_min_c") is not None:
            valid_range = (
                float(options["valid_temperature_min_c"]),
                float(options["valid_temperature_max_c"]),
            )
        save_directory = (
            output_root / "temperature_maps_npz"
            if options.get("save_temperature_npz")
            else None
        )

        context.report_progress(
            26, f"Converting {len(indices):,} frames to temperature"
        )
        rows: list[dict[str, object]] = []
        for position, index in enumerate(indices, start=1):
            context.check_cancelled()
            rows.append(
                health_module.process_one_temperature(
                    (index + 1, health[index], entries[index], spans[index]),
                    correction,
                    None,
                    save_directory,
                    valid_range,
                )
            )
            if position % 10 == 0 or position == len(indices):
                context.report_progress(
                    26 + 60 * position / len(indices),
                    f"Radiometric temperature {position}/{len(indices)}",
                )

        context.report_progress(88, "Matching temperature frames to Noseboom navigation")
        georeferenced = georeference_temperature_records(
            [
                {
                    "timestamp": row.get("timestamp_utc"),
                    "record_index_in_selected_scan": row.get("frame_index"),
                    "pixel_temperature.min_c": row.get("temperature_c_min"),
                    "pixel_temperature.max_c": row.get("temperature_c_max"),
                    "pixel_temperature.mean_c": row.get("temperature_c_mean"),
                    "pixel_temperature.median_c": row.get("temperature_c_median"),
                    "pixel_temperature.std_c": row.get("temperature_c_std"),
                    "pixel_temperature.valid_pixel_count": row.get(
                        "temperature_c_valid_pixel_count"
                    ),
                    "calculated_temperature.status": row.get("temperature_status"),
                }
                for row in rows
            ],
            noseboom_points,
        )
        position_by_frame = {
            str(item["frame_id"]): item for item in georeferenced
        }
        for row in rows:
            match = position_by_frame.get(str(row.get("frame_index")))
            # Every row carries the navigation columns so the CSV is directly
            # usable downstream; unmatched frames simply carry blanks.
            row["noseboom_time_utc"] = match["noseboom_time_utc"] if match else ""
            row["noseboom_time_delta_s"] = match["time_delta_seconds"] if match else ""
            row["latitude_deg"] = match["latitude"] if match else ""
            row["longitude_deg"] = match["longitude"] if match else ""
            row["altitude_m"] = match["altitude_m"] if match else ""
            row["georeference_status"] = "MATCHED" if match else "NO_NAVIGATION"
            row["georeference_method"] = (
                "nearest Noseboom UTC navigation sample within 2.5 s"
            )
            row["temperature_mode"] = mode
            row["environment_inputs_provenance"] = provenance
            row["quantitative"] = (
                mode == CORRECTED and provenance == PROVENANCE_MEASURED
            )
        health_module.write_csv(output_root / "temperature_frames.csv", rows)

        matched = sum(1 for row in rows if row["georeference_status"] == "MATCHED")
        converted = sum(1 for row in rows if row.get("temperature_status") == "PASS")
        summary_payload = {
            **summary,
            "processed_frames": len(rows),
            "temperature_frames_passed": converted,
            "georeferenced_frames": matched,
            "temperature_mode": mode,
            "environment_inputs_provenance": provenance,
            "quantitative": mode == CORRECTED and provenance == PROVENANCE_MEASURED,
            "time_filter_start_utc": _iso(selected_start),
            "time_filter_end_utc": _iso(selected_end),
            "selected_routines": list(selected_routines),
        }
        (output_root / "summary.json").write_text(
            json.dumps(summary_payload, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

        context.report_progress(94, "Publishing the FLIR temperature workspace")
        # A flight has far more frames than a plot has pixels or a map has
        # useful markers. Sending every one made the page parse tens of
        # megabytes and then build every trace and marker on the main thread,
        # which it cannot interrupt - so it sat behind "Preparing FLIR
        # workspace" with nothing moving. Bucketed decimation keeps the real
        # envelope; temperature_frames.csv keeps every frame.
        temperature_records, temperature_total = decimate_for_view(
            [
                {
                    "frame_id": str(row.get("frame_index")),
                    "timestamp_utc": row.get("timestamp_utc"),
                    "temperature_min_c": _finite_or_none(row.get("temperature_c_min")),
                    "temperature_max_c": _finite_or_none(row.get("temperature_c_max")),
                    "temperature_mean_c": _finite_or_none(row.get("temperature_c_mean")),
                    "temperature_median_c": _finite_or_none(row.get("temperature_c_median")),
                    "temperature_std_c": _finite_or_none(row.get("temperature_c_std")),
                    "valid_pixel_count": _integer_or_none(
                        row.get("temperature_c_valid_pixel_count")
                    ),
                    "status": row.get("temperature_status"),
                }
                for row in rows
            ],
            extreme_fields=("temperature_min_c", "temperature_max_c"),
        )
        map_points, map_total = decimate_for_view(
            georeferenced,
            extreme_fields=("temperature_max_c",),
        )
        with self._lock:
            browser_payload = dict(self._instruments["flir"].quicklook)
        browser_payload.update({
            "available": True,
            "temperature_available": bool(matched),
            "temperature_reason": (
                None if matched else
                "Temperature was calculated, but no frame matched processed "
                "Noseboom navigation within 2.5 seconds. Process Noseboom first."
            ),
            "temperature_interpretation": (
                "FLIR apparent temperature: factory calibration with emissivity "
                "1, no atmospheric, reflected or optics correction. A sensor "
                "sanity check, not a surface temperature."
                if mode == APPARENT else
                "FLIR environment-corrected surface temperature using measured "
                "emissivity, distance, atmospheric and reflected temperature, "
                "humidity and optics transmission."
                if provenance == PROVENANCE_MEASURED else
                "Environment-corrected temperature computed from assumed inputs. "
                "NOT QUANTITATIVE — for debugging only."
            ),
            "temperature_mode": mode,
            "quantitative": mode == CORRECTED and provenance == PROVENANCE_MEASURED,
            "temperature_records": temperature_records,
            "temperature_records_total": temperature_total,
            "map_points": map_points,
            "map_points_total": map_total,
            "processed_temperature_frames": len(rows),
            "georeferenced_temperature_frames": matched,
            "matching_method": (
                "Nearest Noseboom UTC navigation sample; maximum difference 2.5 seconds"
            ),
        })
        quicklook_path = (
            project.flight_output_root / "quicklooks" / "flir_browser.json"
        )
        quicklook_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = quicklook_path.with_suffix(".json.temporary")
        temporary.write_text(
            json.dumps(browser_payload, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(quicklook_path)

        produced = [
            output_root / name for name in (
                "temperature_frames.csv", "frame_health.csv",
                "acquisition_gaps.csv", "summary.json", "timestamp_index.csv",
            )
        ]
        with self._lock:
            state = self._instruments["flir"]
            for value in (*produced, quicklook_path):
                if str(value) not in state.output_files:
                    state.output_files.append(str(value))
            state.quicklook = browser_payload
            saved = project.detected_instruments.get("flir")
            if saved is not None:
                saved.output_locations = [Path(v) for v in state.output_files]
            project.output_locations["flir_browser"] = quicklook_path
        self.logger.log(
            LogLevel.SUCCESS, "camera-level2",
            (
                f"FLIR Level 2 complete: {converted}/{len(rows)} frame(s) converted "
                f"to {mode} temperature, {matched} georeferenced against Noseboom"
            ),
            instrument="flir", job_id="flir_detailed",
            file_path=output_root / "temperature_frames.csv",
            processing_step=",".join(selected_routines),
        )
        context.report_progress(100, "FLIR Level 2 and temperature map complete")
        return JobOutcome(
            warning=None if matched else
            "Temperature was calculated but no frame matched Noseboom navigation."
        )

    def _gopro_quick_task(self, context: ProcessingContext) -> JobOutcome | None:
        """Inventory GoPro media and match CET/CEST captures to Noseboom UTC."""
        with self._lock:
            report, project = self._report, self._flight_project
            limits = self._resource_limits
        if report is None or project is None:
            raise RuntimeError("Flight scan and project state are required")
        selected_start, selected_end = self._instrument_processing_interval(
            "gopro"
        )
        candidates = [item for item in report.candidates if item.instrument_id == "gopro"]
        roots = tuple(dict.fromkeys(item.candidate_path for item in candidates))
        supported = {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
        paths: list[Path] = []
        for root in roots:
            context.check_cancelled()
            if root.is_dir():
                paths.extend(path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in supported)
            elif root.is_file() and root.suffix.casefold() in supported:
                paths.append(root)
        for item in candidates:
            paths.extend(path for path in item.all_matching_files if path.is_file() and path.suffix.casefold() in supported)
        paths = list(dict.fromkeys(paths))
        if not paths:
            raise RuntimeError("No supported GoPro images or videos are available")
        adapter = GoProLevel1Adapter(
            output_root=self._run_output_root(project, "gopro"),
            flight_name=project.flight_id,
            resource_limits=limits,
            batch_policy=CameraBatchPolicy(
                maximum_batch_files=max(1, min(16, len(paths))),
                maximum_thumbnail_count=12,
            ),
            logger=self.logger,
        )
        adapter.report_progress(lambda update: context.report_progress(update.progress or 0.0, update.phase))
        loaded = adapter.load(InputCandidate("gopro", tuple(paths), 1.0, "Confirmed Flight Folder scan candidate"))
        result = adapter.process_quicklook(
            loaded,
            {
                "analysis_start": selected_start,
                "analysis_end": selected_end,
            },
        )
        outputs = adapter.export_results(result, adapter.output_root, ("csv", "json"))
        adapter.create_plots(result, adapter.output_root)
        context.report_progress(82, "Matching GoPro Europe/Berlin time to Noseboom UTC")
        camera_root = project.camera_folder_path.resolve() if project.camera_folder_path else None
        records = [
            {
                **item,
                "source_file": (
                    str(Path(str(item["source_file"])).resolve().relative_to(camera_root))
                    if camera_root is not None
                    and Path(str(item["source_file"])).resolve().is_relative_to(camera_root)
                    else Path(str(item["source_file"])).name
                ),
                "timestamp": (
                    item["timestamp"].isoformat()
                    if isinstance(item.get("timestamp"), datetime)
                    else item.get("timestamp")
                ),
            }
            for item in adapter.media_records()
        ]
        # Noseboom and camera metadata run in separate worker groups. Read the
        # navigation payload only after the slower camera inventory completes,
        # rather than retaining an empty snapshot from job start.
        with self._lock:
            noseboom_points = tuple(
                self._instruments["noseboom"].quicklook.get("points", ())
            )
        captures = georeference_captures(records, noseboom_points)
        image_count = sum(
            1 for item in adapter.media_records() if item.get("kind") == "image"
        )
        payload = {
            "available": bool(captures),
            "reason": (
                None if captures
                else "No GoPro image timestamp could be matched to processed Noseboom navigation within 2.5 seconds."
            ),
            "camera_timezone": "Europe/Berlin (CET/CEST)",
            "navigation_timezone": "UTC",
            "matching_method": "Nearest Noseboom 1 Hz sample; maximum difference 2.5 seconds",
            "image_count": image_count,
            "matched_count": len(captures),
            "unmatched_count": max(0, image_count - len(captures)),
            "inventory": records,
            "captures": captures,
        }
        quicklook_path = (
            project.flight_output_root / "quicklooks" / "gopro_browser.json"
        )
        quicklook_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = quicklook_path.with_suffix(".json.temporary")
        temporary.write_text(
            json.dumps(
                _gopro_project_payload(payload),
                ensure_ascii=False, indent=2, allow_nan=False,
            ) + "\n",
            encoding="utf-8",
        )
        temporary.replace(quicklook_path)
        with self._lock:
            state = self._instruments["gopro"]
            state.quicklook = payload
            state.output_files = [
                str(value.path) for value in outputs
            ] + [str(value.path) for value in result.figures] + [str(quicklook_path)]
            project.output_locations["gopro_quicklook"] = quicklook_path
            saved = project.detected_instruments.get("gopro")
            if saved is not None:
                saved.output_locations = [
                    *(Path(value.path) for value in outputs),
                    *(Path(value.path) for value in result.figures),
                    quicklook_path,
                ]
        context.report_progress(100, f"GoPro map ready with {len(captures)} capture locations")
        warning = result.warnings[0] if result.warnings else None
        if not captures:
            warning = str(payload["reason"])
        return JobOutcome(warning=warning)

    def _on_job_update(self, job: ProcessingJob) -> None:
        """Publish job state cheaply; persist to disk only when it is worth it.

        The scheduler calls this on every ``report_progress``, several times a
        second per running job. Checkpointing each one rewrote the whole project
        file and re-exported the whole diagnostics log while holding ``_lock``,
        which is the same lock every dashboard poll needs. Terminal states are
        still written immediately, so nothing recoverable is lost on a crash.
        """
        with self._lock:
            if job.status in {
                ProcessingStatus.COMPLETE,
                ProcessingStatus.WARNING,
            }:
                active_job = self.processing_queue.get(job.job_id)
                active_job.enabled = False
            state = self._instruments.get(job.instrument_id)
            if state is not None:
                state.processing_status = job.status.value
                state.processing_progress = job.progress
                state.processing_step = job.current_step
                state.processing_elapsed_seconds = job.elapsed_time.total_seconds()
                if job.error:
                    state.errors = list(dict.fromkeys(state.errors + [job.error]))
                if job.status is ProcessingStatus.WARNING and job.current_step:
                    state.warnings = list(
                        dict.fromkeys(state.warnings + [job.current_step])
                    )
            self._sync_project_queue_state()
            now = time.monotonic()
            checkpoint_due = job.status in TERMINAL_PROCESSING_STATUSES or (
                now - self._last_checkpoint_monotonic
                >= PROGRESS_CHECKPOINT_INTERVAL_SECONDS
            )
            if checkpoint_due:
                self._last_checkpoint_monotonic = now
        # Deliberately outside the lock: this writes the project file and copies
        # the diagnostics log, and must not block dashboard polling.
        if checkpoint_due:
            self._checkpoint_project()


def _flir_frame_utc(module, timestamp: str) -> datetime | None:
    """Parse a FLIR frame timestamp and normalise it to UTC for comparison."""
    parsed = module.parse_timestamp(timestamp)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_dashboard_datetime(value):
    """parse_dashboard_datetime, but None stays None rather than raising."""
    return parse_dashboard_datetime(value) if value else None


def _iso_or_none(value):
    return value.isoformat() if value is not None else None


def _flir_selection_failure_message(
    indexed_frame_count: int,
    coverage: object,
    selected_start: datetime | None,
    selected_end: datetime | None,
) -> str:
    """Explain an empty FLIR selection with the ranges the operator can act on."""
    available_start = getattr(coverage, "raw_start", None)
    available_end = getattr(coverage, "raw_end", None)
    detail = (
        f"The FLIR export covers {_iso(available_start)} to {_iso(available_end)} UTC"
        if available_start and available_end
        else f"{indexed_frame_count:,} FLIR frame(s) were indexed"
    )
    return (
        "No FLIR frame falls inside the selected Time Filter "
        f"({_iso(selected_start)} to {_iso(selected_end)} UTC). "
        f"{detail}. Change the Time Filter to an interval the FLIR export "
        "covers, or select the FLIR export recorded during this flight."
    )


def _balanced_worker_count(system) -> int:
    """Default CPU allocation that still staffs the camera-metadata pool.

    ``worker_group_capacities`` only gives the camera-metadata group a worker
    from two workers upwards. A one-worker default therefore let Remote Sensing
    enqueue camera jobs into a pool that could never run them, leaving them
    waiting forever with no error. Machines with two logical cores still end up
    with a single worker; ``start_remote_sensing`` reports that explicitly.
    """
    return max(
        1,
        min(
            system.safely_available_workers,
            max(2, system.total_logical_cores // 4),
        ),
    )


def _gopro_project_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Project copy of the GoPro result: identity and geometry, never pixels.

    Images stay on the campaign disk. What the project needs in order to draw
    the map and offer a download later is the capture identity plus its matched
    navigation, so the stored copy drops the full media inventory and the
    on-disk path of every capture. ``file_name`` is retained because that is
    what re-links a capture to its image when the disk is reattached.
    """
    saved = {
        key: value
        for key, value in payload.items()
        if key not in {"inventory", "captures"}
    }
    saved["captures"] = [
        {key: value for key, value in item.items() if key != "source_file"}
        for item in payload.get("captures", ())
        if isinstance(item, dict)
    ]
    if not payload.get("available"):
        # Only useful while georeferencing has not succeeded; gopro_view()
        # retries the match from it once Noseboom navigation exists.
        saved["inventory"] = [
            {
                **{k: v for k, v in item.items() if k != "source_file"},
                "source_file": Path(str(item.get("source_file", ""))).name,
            }
            for item in payload.get("inventory", ())
            if isinstance(item, dict)
        ]
    saved["images_stored_in_project"] = False
    return saved


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _integer_or_none(value: object) -> int | None:
    number = _finite_or_none(value)
    return int(number) if number is not None else None


def _optional_float(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    number = _finite_or_none(value)
    if number is None:
        raise ValueError(f"Expected a finite numeric value, received: {value}")
    return number


def _assert_directory_responsive(path: Path, timeout_seconds: float = 8.0) -> None:
    """Avoid starting an uncancellable scan on a stalled external mount."""
    probe = (
        "import os,sys; iterator=os.scandir(sys.argv[1]); "
        "next(iterator,None); iterator.close()"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            "Camera System Folder did not respond within "
            f"{timeout_seconds:g} seconds. Check the external disk or select "
            "the flight-specific camera subfolder."
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "directory enumeration failed"
        raise ValueError(f"Camera System Folder is not readable: {detail}")


def _merge_scan_reports(primary: ScanReport, camera: ScanReport) -> ScanReport:
    """Combine independent raw roots while retaining every candidate."""
    candidates = list(primary.candidates) + list(camera.candidates)
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.instrument_id] = counts.get(candidate.instrument_id, 0) + 1
    merged_candidates = tuple(
        replace(
            candidate,
            ambiguous=candidate.ambiguous or counts[candidate.instrument_id] > 1,
            warnings=tuple(
                dict.fromkeys(
                    (
                        *candidate.warnings,
                        *(
                            ("Multiple candidate paths matched this instrument across selected roots.",)
                            if counts[candidate.instrument_id] > 1
                            else ()
                        ),
                    )
                )
            ),
        )
        for candidate in candidates
    )
    return ScanReport(
        root=primary.root,
        candidates=merged_candidates,
        files_scanned=primary.files_scanned + camera.files_scanned,
        folders_scanned=primary.folders_scanned + camera.folders_scanned,
        inaccessible_path_count=primary.inaccessible_path_count + camera.inaccessible_path_count,
        malformed_file_count=primary.malformed_file_count + camera.malformed_file_count,
        warnings=tuple(dict.fromkeys((*primary.warnings, *camera.warnings))),
        errors=tuple(dict.fromkeys((*primary.errors, *camera.errors))),
        cancelled=primary.cancelled or camera.cancelled,
    )


def _job_dict(job: ProcessingJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "instrument_id": job.instrument_id,
        "display_name": job.display_name,
        "worker_group": job.worker_group.value,
        "priority": job.priority,
        "enabled": job.enabled,
        "status": job.status.value,
        "progress": job.progress,
        "current_step": job.current_step,
        "elapsed_seconds": job.elapsed_time.total_seconds(),
        "safely_cancellable": job.safely_cancellable,
        "detailed": job.detailed,
        "task_registered": job.task is not None,
        "error": job.error,
    }
