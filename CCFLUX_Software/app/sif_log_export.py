"""The Noseboom + Gimbal telemetry log, for SIF analysis done outside this tool.

AirFloX spectra carry no usable position on this campaign - the FLOX GPS
frequently never acquires a fix - so SIF processing builds its geometry by
matching Gremsy gimbal attitude to Noseboom latitude, longitude and altitude.
An analysis done elsewhere, in R, needs exactly that file: the same matching,
the same rows, and the same column names.

So this exports the file the SIF pipeline itself uses, produced by the same
validated routine (`prepare_sif_log_from_hatchbox`) with the same options.
Rebuilding it in R would be a second implementation of the campaign's
navigation, and the two would drift.
"""

from __future__ import annotations

import csv
import io
import shutil
import threading
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from core.logging_manager import LogLevel, ProcessingLogManager
from core.time_manager import SIF_RECORD_CLOCK_TIMEZONES

# What the log carries, in the order the original SIF log writes them. Kept here
# so the schema can be shown to the operator and asserted by a test without
# importing the legacy module.
SIF_LOG_COLUMNS = (
    "lat", "lon", "alt_above_ground_m", "date_time_utc", "pitch", "roll", "yaw",
)
LOG_FILENAME_SUFFIX = "_noseboom_gimbal_sif_log.csv"
TIME_COLUMN = "date_time_utc"

# Noseboom TIMESTAMP and the Gremsy _time are both UTC, so the log this routine
# writes is UTC. The AirFloX record clock is not: on this campaign it runs on
# campaign local time, so an analysis that reads the raw spectra alongside this
# log may want the two on the same clock. The choice is offered rather than
# guessed, and the same vocabulary the SIF record-clock question already uses.
#
# Shifting does not rename the column: the request is that the variable names
# match the original SIF log exactly, so date_time_utc keeps its name and then
# no longer holds UTC. That is a trap unless it is said loudly, so a shifted
# file is named for its clock, the panel says so, and the log records it.
OUTPUT_TIMEZONES = SIF_RECORD_CLOCK_TIMEZONES
DEFAULT_OUTPUT_TIMEZONE = "utc"
# Written by the legacy routine as seconds; milliseconds are accepted because a
# log reused from a delivery may carry them.
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def output_timezone_choice(key: object) -> tuple[str, float, str]:
    """Resolve a requested output clock to (key, offset seconds, label)."""
    normalized = str(key or DEFAULT_OUTPUT_TIMEZONE).strip().casefold()
    entry = OUTPUT_TIMEZONES.get(normalized)
    if entry is None:
        raise ValueError(
            "The SIF log clock must be one of: "
            + ", ".join(sorted(OUTPUT_TIMEZONES))
        )
    return normalized, float(entry["offset_seconds"]), str(entry["label"])


class SifLogExportManager:
    """Own one non-blocking SIF telemetry-log build."""

    def __init__(
        self,
        logger: ProcessingLogManager,
        on_complete: Callable[[list[Path]], None] | None = None,
    ) -> None:
        self._logger = logger
        self._on_complete = on_complete
        self._lock = threading.RLock()
        self._files: dict[str, Path] = {}
        self._state: dict[str, object] = self._idle()

    @staticmethod
    def _idle() -> dict[str, object]:
        return {
            "status": "idle",
            "progress": 0.0,
            "step": "No SIF log has been built.",
            "files": [],
            "columns": list(SIF_LOG_COLUMNS),
            "reused_existing": False,
            "rows_written": None,
            "notes": [],
            "output_timezone": DEFAULT_OUTPUT_TIMEZONE,
            "output_timezone_label": str(
                OUTPUT_TIMEZONES[DEFAULT_OUTPUT_TIMEZONE]["label"]
            ),
            "time_shift_seconds": 0.0,
            "choices": [
                {"key": key, "label": str(entry["label"]),
                 "offset_seconds": float(entry["offset_seconds"])}
                for key, entry in OUTPUT_TIMEZONES.items()
            ],
            "error": None,
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state)

    def file(self, name: str) -> Path:
        with self._lock:
            path = self._files.get(Path(str(name)).name)
        if path is None or not path.is_file():
            raise ValueError("That SIF log is not available; build it again.")
        return path

    def start(
        self,
        *,
        flight_root: Path,
        output_dir: Path,
        flight_name: str,
        altitude_filter: bool = False,
        max_position_gap_seconds: float = 0.2,
        output_timezone: str = DEFAULT_OUTPUT_TIMEZONE,
    ) -> dict[str, object]:
        key, offset, label = output_timezone_choice(output_timezone)
        with self._lock:
            if self._state["status"] == "running":
                raise RuntimeError("A SIF log is already being built")
            self._state = {
                **self._idle(),
                "status": "running",
                "progress": 2.0,
                "step": "Locating the Gimbal and Noseboom deliveries",
                "output_timezone": key,
                "output_timezone_label": label,
                "time_shift_seconds": offset,
            }
            self._files = {}
        self._logger.log(
            LogLevel.INFO, "sif-log",
            f"Building the Noseboom + Gimbal SIF log for {flight_name}; "
            f"Noseboom and Gimbal are UTC and the log is written on {label}",
            instrument="sif", processing_step="sif-log",
        )
        threading.Thread(
            target=self._run,
            args=(Path(flight_root), Path(output_dir), flight_name,
                  bool(altitude_filter), float(max_position_gap_seconds),
                  key, offset, label),
            daemon=True, name="ccflux-sif-log",
        ).start()
        return self.snapshot()

    def _progress(self, percent: float, step: str) -> None:
        with self._lock:
            if self._state["status"] != "running":
                return
            self._state["progress"] = max(0.0, min(100.0, float(percent)))
            self._state["step"] = str(step)

    def _run(
        self, flight_root: Path, output_dir: Path, flight_name: str,
        altitude_filter: bool, gap_seconds: float,
        timezone_key: str, offset_seconds: float, timezone_label: str,
    ) -> None:
        try:
            from instruments.sif.legacy_bridge import LegacySifBridge

            module = LegacySifBridge().module
            self._progress(
                8, "Reading Gimbal attitude and Noseboom position (this is the "
                   "whole flight record and takes a few minutes)",
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            # The routine reports what it did on stdout - rows written, rows
            # skipped, whichever files it had to look outside HATCH-BOX for.
            # None of that reaches an operator from a background thread, so it
            # is captured and logged instead of being lost.
            spoken = io.StringIO()
            with redirect_stdout(spoken):
                produced = Path(module.prepare_sif_log_from_hatchbox(
                    flight_root, output_dir,
                    custom_log=None,
                    altitude_filter=altitude_filter,
                    max_position_gap_sec=gap_seconds,
                    pitch_from_nadir=PITCH_FROM_NADIR,
                    terrain_sampler=terrain_sampler(
                        output_dir.parent / "_terrain_tiles"
                    ),
                ))
            notes = _readable_notes(spoken.getvalue())
            reused = not _is_relative_to(produced, output_dir)
            destination = output_dir / produced.name
            if offset_seconds:
                # Named for the clock it holds. date_time_utc keeps its name so
                # the schema matches the original SIF log, which means a shifted
                # file has a column called UTC that is not UTC - the file name,
                # the panel and the log are what stop that being a silent trap.
                self._progress(
                    90, f"Moving the log's clock to {timezone_label}"
                )
                destination = output_dir / (
                    f"{produced.stem}_{timezone_key.upper()}{produced.suffix}"
                )
                moved, unreadable = shift_time_column(
                    produced, destination, offset_seconds
                )
                notes.append(
                    f"{TIME_COLUMN} moved by {offset_seconds / 3600:+.0f} h to "
                    f"{timezone_label} on {moved:,} row(s); the column keeps its "
                    "name so the schema matches the original SIF log, so it now "
                    "holds " + timezone_label + " rather than UTC."
                )
                if unreadable:
                    notes.append(
                        f"Warning: {unreadable:,} row(s) had no readable "
                        f"{TIME_COLUMN} and were written through unchanged."
                    )
            elif produced.resolve() != destination.resolve():
                # Either an existing log found in the read-only delivery, or the
                # routine's own _combined subfolder. Either way the copy that is
                # offered for download sits beside the project's other exports.
                shutil.copy2(produced, destination)
            self._progress(94, "Checking the log's columns")
            columns = _header_of(destination)
            rows = _row_count(destination)
            for note in notes:
                self._logger.log(
                    LogLevel.WARNING if note.lower().startswith("warning")
                    else LogLevel.INFO,
                    "sif-log", note, instrument="sif", processing_step="sif-log",
                )
            with self._lock:
                self._files = {destination.name: destination}
                self._state = {
                    "status": "complete",
                    "progress": 100.0,
                    "step": (
                        ("Reused the SIF log already present in the delivery"
                         if reused else
                         "Noseboom and Gimbal combined into a SIF log")
                        + f" · times on {timezone_label}"
                    ),
                    "output_timezone": timezone_key,
                    "output_timezone_label": timezone_label,
                    "time_shift_seconds": offset_seconds,
                    "choices": [
                        {"key": key, "label": str(entry["label"]),
                         "offset_seconds": float(entry["offset_seconds"])}
                        for key, entry in OUTPUT_TIMEZONES.items()
                    ],
                    "files": [{
                        "name": destination.name,
                        "url": "/api/sif/log/download/" + destination.name,
                        "size_bytes": destination.stat().st_size,
                        "rows": rows,
                    }],
                    "columns": columns,
                    "reused_existing": reused,
                    "rows_written": rows,
                    "notes": notes,
                    "error": None,
                }
            if self._on_complete:
                self._on_complete([destination])
            self._logger.log(
                LogLevel.SUCCESS, "sif-log",
                f"SIF log ready: {destination.name}, {rows:,} row(s), "
                f"columns {', '.join(columns)}, times on {timezone_label}"
                + (" (reused from the delivery)" if reused else ""),
                instrument="sif", file_path=destination,
                processing_step="sif-log",
            )
        except Exception as exc:
            with self._lock:
                self._state = {
                    **self._idle(),
                    "status": "failed",
                    "progress": 100.0,
                    "step": "The SIF log could not be built.",
                    "error": str(exc),
                }
            self._logger.capture_exception(
                "sif-log", "Building the SIF log failed", exc,
                instrument="sif", processing_step="sif-log",
            )


def shift_time_column(
    source: Path, destination: Path, offset_seconds: float
) -> tuple[int, int]:
    """Rewrite the log with its clock moved, keeping every other value as read.

    Returns (rows shifted, rows whose timestamp could not be read). Only the
    timestamp text is touched: latitude, longitude, altitude and the three
    angles are copied through unparsed, so nothing is re-rounded on the way.
    """
    delta = timedelta(seconds=offset_seconds)
    shifted = 0
    unreadable = 0
    with source.open("r", encoding="utf-8-sig", newline="") as reader_stream:
        reader = csv.reader(reader_stream)
        header = next(reader, [])
        try:
            column = [value.strip() for value in header].index(TIME_COLUMN)
        except ValueError as exc:
            raise ValueError(
                f"The SIF log has no {TIME_COLUMN} column to move: {source.name}"
            ) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8", newline="") as writer_stream:
            writer = csv.writer(writer_stream, lineterminator="\n")
            writer.writerow(header)
            for row in reader:
                if column < len(row):
                    moved = _moved_timestamp(row[column], delta)
                    if moved is None:
                        unreadable += 1
                    else:
                        row[column] = moved
                        shifted += 1
                writer.writerow(row)
    return shifted, unreadable


def _moved_timestamp(value: str, delta: timedelta) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    for pattern in TIME_FORMATS:
        try:
            stamp = datetime.strptime(text, pattern)
        except ValueError:
            continue
        moved = (stamp + delta).strftime(pattern)
        # The routine writes milliseconds, not microseconds, when it writes any.
        return moved[:-3] if pattern.endswith(".%f") else moved
    return None


# The AirFloX data processing manual defines the log's pitch as the angle off
# level - "0 = aircraft is level, <0 = towards ground, >0 = to sky" - while the
# Gremsy publishes gimbal pitch measured down from the horizon, reading 90 at
# nadir. Ninety percent of Flight_CC0806 sits within a tenth of a degree of that
# 90, so writing the raw column would report a Zeppelin diving vertically for the
# whole flight; converted, it sits on zero the way the 2024 reference log does.
# SIF itself never reads pitch, roll or yaw - match_data takes only date_time_utc,
# lat, lon and alt_above_ground_m - so this changes nothing in the pipeline and
# everything for an analysis that reads the angles.
PITCH_FROM_NADIR = True


def terrain_sampler(cache_directory: Path):
    """Ground elevation in metres above sea level, from the campaign's own DTM.

    The same satellite terrain model and the same sampling the Noseboom 1 Hz
    product uses, so an altitude here and an altitude there mean the same thing.
    Tiles are cached on disk; a flight covers a handful of them.
    """
    from instruments.noseboom.legacy_bridge import LegacyNoseboomBridge

    module = LegacyNoseboomBridge().module

    def sample(latitudes, longitudes):
        import pandas as pd

        frame = pd.DataFrame({"plot_lat": latitudes, "plot_lon": longitudes})
        return module.sample_terrarium(frame, Path(cache_directory))

    return sample


def _readable_notes(spoken: str) -> list[str]:
    """The routine's own key=value chatter, as sentences worth logging."""
    notes: list[str] = []
    for line in spoken.splitlines():
        text = line.strip()
        if not text:
            continue
        key, separator, value = text.partition("=")
        if separator and key in {"warning", "note"}:
            notes.append(f"{key.capitalize()}: {value.strip()}")
        elif separator and key in {"rows_written", "rows_skipped"}:
            notes.append(f"{key.replace('_', ' ').capitalize()}: {value.strip()}")
    return notes


def _header_of(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return [value.strip() for value in next(csv.reader(stream), [])]


def _row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return max(0, sum(1 for _ in stream) - 1)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
