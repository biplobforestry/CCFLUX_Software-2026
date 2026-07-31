

from __future__ import annotations

import argparse
import bisect
import os
import csv
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import numpy as np


DEFAULT_INPUT = Path(
    r"D:\backup_20260708_164500_mongodb\20260708_164500\mongodb\camera"
)

TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
TIMESTAMP_BYTES_RE = re.compile(rb'"timestamp"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"')
OBJECT_DELIMITER_BYTES_RE = re.compile(rb'}\s*,\s*\{')

# Tuned for this PC: Intel Core Ultra 7 165U, 14 logical CPUs, ~32 GB RAM.
PC_TUNED_SCAN_WORKERS = 8
PC_TUNED_CALC_WORKERS = 12
PC_TUNED_SCAN_CHUNK_MB = 256
PC_TUNED_BATCH_SIZE = 50

# Full-power profile for this PC. Use when you want maximum speed and accept
# higher CPU/RAM/disk pressure while the script is running.
FULL_POWER_SCAN_WORKERS = 14
FULL_POWER_CALC_WORKERS = 14
FULL_POWER_SCAN_CHUNK_MB = 512
FULL_POWER_BATCH_SIZE = 100

# Spyder-friendly globals. They are filled at the end if pandas is installed.
df = None
selected_dataframe = None


# ---------------------------------------------------------------------------
# Section 1: Find JSON files and stream raw frame objects
# ---------------------------------------------------------------------------

def find_json_files(input_path: Path, recursive: bool = False) -> list[Path]:
    """Return all JSON files to process from one file or one folder."""
    if input_path.is_file():
        if input_path.suffix.lower() != ".json":
            raise ValueError(f"Input file is not a .json file: {input_path}")
        return [input_path]

    if input_path.is_dir():
        pattern = "**/*.json" if recursive else "*.json"
        files = sorted(path for path in input_path.glob(pattern) if path.is_file())
        if files:
            return files
        raise FileNotFoundError(f"No .json files found in folder: {input_path}")

    raise FileNotFoundError(f"Input path not found: {input_path}")


def find_object_start_before_timestamp(
    path: Path,
    timestamp_byte_offset: int,
    backtrack_bytes: int = 4 * 1024 * 1024,
) -> int:
    """Estimate the top-level frame-object start before a timestamp field."""
    read_start = max(0, timestamp_byte_offset - backtrack_bytes)
    with path.open("rb") as source:
        source.seek(read_start)
        data = source.read(timestamp_byte_offset - read_start)

    last_delimiter = None
    for match in OBJECT_DELIMITER_BYTES_RE.finditer(data):
        last_delimiter = match

    if last_delimiter is not None:
        # Match is previous_object_end, comma, current_object_start.
        return read_start + last_delimiter.end() - 1

    first_object = data.find(b"{")
    if first_object >= 0:
        return read_start + first_object
    return read_start


def iter_json_object_texts(
    path: Path,
    chunk_size: int = 8 * 1024 * 1024,
    start_offset: int = 0,
) -> Iterator[str]:
    """Yield each top-level JSON object as text without parsing it.

    start_offset lets Step 2 seek near the selected time window instead of
    walking from the beginning of a huge JSON export.
    """
    depth = 0
    in_string = False
    escape = False
    collecting = False
    parts: list[str] = []

    with path.open("r", encoding="utf-8") as source:
        if start_offset > 0:
            source.seek(start_offset)
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break

            for char in chunk:
                if not collecting:
                    if char == "{":
                        collecting = True
                        depth = 1
                        in_string = False
                        escape = False
                        parts = [char]
                    continue

                parts.append(char)

                if escape:
                    escape = False
                    continue

                if char == "\\":
                    escape = True
                    continue

                if char == '"':
                    in_string = not in_string
                    continue

                if in_string:
                    continue

                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        yield "".join(parts)
                        collecting = False
                        parts = []

    if collecting:
        # If we started inside an object because of a rough seek offset, ignore
        # the incomplete leading/trailing object instead of crashing.
        if start_offset <= 0:
            raise ValueError(f"Unfinished JSON object at end of file: {path}")


def extract_timestamp_from_object_text(object_text: str) -> str:
    """Read timestamp from one frame object without full JSON parsing."""
    match = TIMESTAMP_RE.search(object_text)
    if not match:
        return ""
    return sortable_timestamp(match.group(1))


def iter_all_object_texts(
    json_files: list[Path],
    start_offsets: dict[Path, int] | None = None,
) -> Iterator[tuple[Path, int, str]]:
    """Yield (source_file, source_file_index, object_text) across JSON files."""
    start_offsets = start_offsets or {}
    for source_file_index, source_file in enumerate(json_files, start=1):
        start_offset = start_offsets.get(source_file, 0)
        for object_text in iter_json_object_texts(source_file, start_offset=start_offset):
            yield source_file, source_file_index, object_text


# ---------------------------------------------------------------------------
# Section 2: Date-time scan and selected window
# ---------------------------------------------------------------------------

def sortable_timestamp(value: Any) -> str:
    """Return an ISO-like timestamp string whose lexical order is chronological."""
    if value is None:
        return ""
    return str(value).strip().replace("T", " ")


def parse_timestamp(value: str) -> datetime | None:
    """Parse common timestamp formats used in the campaign JSON."""
    value = sortable_timestamp(value)
    if not value:
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_timestamp(value: datetime) -> str:
    """Format datetime for string comparisons and CSV consistency."""
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def scan_one_byte_range(
    task: tuple[Path, int, int, int],
) -> tuple[str, str, int, int, list[tuple[str, Path, int]]]:
    """Scan one byte range for timestamps.

    Returns min time, max time, count, bytes scanned, and timestamp index
    entries. Each index entry is (timestamp, source_file, timestamp_byte_offset).
    """
    json_file, start_byte, end_byte, overlap = task
    file_size = json_file.stat().st_size
    read_start = max(0, start_byte - overlap)
    read_end = min(file_size, end_byte + overlap)

    with json_file.open("rb") as source:
        source.seek(read_start)
        data = source.read(read_end - read_start)

    minimum_time = ""
    maximum_time = ""
    timestamp_entries: list[tuple[str, Path, int]] = []
    count = 0
    for match in TIMESTAMP_BYTES_RE.finditer(data):
        absolute_match_start = read_start + match.start()
        if absolute_match_start < start_byte or absolute_match_start >= end_byte:
            continue
        try:
            timestamp = sortable_timestamp(match.group(1).decode("unicode_escape"))
        except UnicodeDecodeError:
            continue

        count += 1
        if timestamp:
            timestamp_entries.append((timestamp, json_file, absolute_match_start))
            if not minimum_time or timestamp < minimum_time:
                minimum_time = timestamp
            if not maximum_time or timestamp > maximum_time:
                maximum_time = timestamp

    return minimum_time, maximum_time, count, end_byte - start_byte, timestamp_entries


def make_scan_tasks(
    json_files: list[Path],
    scan_chunk_mb: int,
    overlap: int = 512,
) -> list[tuple[Path, int, int, int]]:
    """Split all JSON files into independent byte-range timestamp scan tasks."""
    chunk_size = max(scan_chunk_mb, 1) * 1024 * 1024
    tasks: list[tuple[Path, int, int, int]] = []
    for json_file in json_files:
        file_size = json_file.stat().st_size
        start_byte = 0
        while start_byte < file_size:
            end_byte = min(file_size, start_byte + chunk_size)
            tasks.append((json_file, start_byte, end_byte, overlap))
            start_byte = end_byte
    return tasks


def scan_time_range(
    json_files: list[Path],
    progress_every: int = 5000,
    scan_chunk_mb: int = 128,
    scan_workers: int = 1,
) -> tuple[str, str, int, list[str], list[tuple[str, Path, int]]]:
    """Parallel min/max timestamp scan without object splitting or JSON parsing."""
    minimum_time = ""
    maximum_time = ""
    all_timestamps: list[str] = []
    timestamp_index: list[tuple[str, Path, int]] = []
    count = 0
    started = time.monotonic()
    bytes_scanned = 0
    tasks = make_scan_tasks(json_files, scan_chunk_mb=scan_chunk_mb)
    total_bytes = sum(task[2] - task[1] for task in tasks)
    worker_count = max(scan_workers, 1)

    def merge_result(result: tuple[str, str, int, int, list[tuple[str, Path, int]]]) -> None:
        nonlocal minimum_time, maximum_time, count, bytes_scanned, all_timestamps, timestamp_index
        task_minimum, task_maximum, task_count, task_bytes, task_entries = result
        count += task_count
        bytes_scanned += task_bytes
        timestamp_index.extend(task_entries)
        all_timestamps.extend(timestamp for timestamp, _, _ in task_entries)
        if task_minimum and (not minimum_time or task_minimum < minimum_time):
            minimum_time = task_minimum
        if task_maximum and (not maximum_time or task_maximum > maximum_time):
            maximum_time = task_maximum

    def print_progress(task_index: int) -> None:
        if progress_every <= 0:
            return
        elapsed = max(time.monotonic() - started, 0.001)
        mb_per_second = bytes_scanned / elapsed / (1024 * 1024)
        print(
            f"\rParallel timestamp scan: task {task_index}/{len(tasks)}, "
            f"{bytes_scanned / (1024 ** 3):.2f}/{total_bytes / (1024 ** 3):.2f} GB, "
            f"{count:,} timestamps, {mb_per_second:.1f} MB/s, workers={worker_count}",
            end="",
            flush=True,
        )

    if worker_count <= 1 or len(tasks) <= 1:
        for task_index, task in enumerate(tasks, start=1):
            merge_result(scan_one_byte_range(task))
            print_progress(task_index)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for task_index, result in enumerate(executor.map(scan_one_byte_range, tasks), start=1):
                merge_result(result)
                print_progress(task_index)

    if bytes_scanned:
        print()
    all_timestamps = sorted(set(all_timestamps))
    timestamp_index.sort(key=lambda item: (item[0], str(item[1]), item[2]))
    return minimum_time, maximum_time, count, all_timestamps, timestamp_index

def ask_for_time(label: str, default_value: str) -> str:
    """Ask for one date-time. Empty input uses the default value.

    Important: if interactive input is unavailable, we stop clearly instead of
    silently processing the full huge dataset.
    """
    try:
        text = input(f"{label} date-time [press Enter for {default_value}]: ").strip()
    except EOFError as error:
        raise RuntimeError(
            "Interactive date-time input is not available. Rerun with "
            "--start-time and --end-time, or use --auto-window-minutes for testing."
        ) from error
    return sortable_timestamp(text) if text else default_value


def choose_auto_window(minimum_time: str, maximum_time: str, minutes: float) -> tuple[str, str]:
    """Choose a random available time window for automated testing."""
    start_available = parse_timestamp(minimum_time)
    end_available = parse_timestamp(maximum_time)
    if start_available is None or end_available is None:
        return minimum_time, maximum_time

    duration = timedelta(minutes=minutes)
    latest_start = end_available - duration
    if latest_start <= start_available:
        return format_timestamp(start_available), format_timestamp(end_available)

    random_seconds = random.uniform(0, (latest_start - start_available).total_seconds())
    selected_start = start_available + timedelta(seconds=random_seconds)
    selected_end = selected_start + duration
    return format_timestamp(selected_start), format_timestamp(selected_end)


def snap_to_nearest_available_time(
    requested_time: str,
    available_timestamps: list[str],
    label: str,
) -> str:
    """Snap a requested time to the nearest actual frame timestamp."""
    requested_dt = parse_timestamp(requested_time)
    if requested_dt is None or not available_timestamps:
        return sortable_timestamp(requested_time)

    requested_normalized = format_timestamp(requested_dt)
    position = bisect.bisect_left(available_timestamps, requested_normalized)
    candidates: list[str] = []
    if position > 0:
        candidates.append(available_timestamps[position - 1])
    if position < len(available_timestamps):
        candidates.append(available_timestamps[position])
    if not candidates:
        return requested_normalized

    def distance_seconds(timestamp: str) -> float:
        candidate_dt = parse_timestamp(timestamp)
        if candidate_dt is None:
            return float("inf")
        return abs((candidate_dt - requested_dt).total_seconds())

    nearest = min(candidates, key=distance_seconds)
    if nearest != requested_normalized:
        print(f"{label} requested {requested_time} -> nearest available frame {nearest}")
    return nearest

def choose_time_window(
    minimum_time: str,
    maximum_time: str,
    available_timestamps: list[str],
    start_time: str | None,
    end_time: str | None,
    no_time_prompt: bool,
    auto_window_minutes: float | None,
) -> tuple[str, str]:
    """Choose the processing window before any temperature calculation."""
    if not minimum_time or not maximum_time:
        return "", ""

    print("\nAvailable date-time range across all JSON files:")
    print(f"Minimum date-time: {minimum_time}")
    print(f"Maximum date-time: {maximum_time}")

    if auto_window_minutes is not None:
        selected_start, selected_end = choose_auto_window(
            minimum_time, maximum_time, auto_window_minutes
        )
        print(f"\nAuto-selected random {auto_window_minutes:g}-minute test window.")
    elif start_time or end_time:
        selected_start = sortable_timestamp(start_time) if start_time else minimum_time
        selected_end = sortable_timestamp(end_time) if end_time else maximum_time
    elif no_time_prompt:
        selected_start = minimum_time
        selected_end = maximum_time
        print("\n--no-time-prompt was used without start/end time, so full available range is selected.")
    else:
        print("\nSelect the date-time window to calculate/export.")
        print("Example format: 2026-06-10 13:40:00")
        print("Press Enter twice to process the full available range.\n")
        selected_start = ask_for_time("Start", minimum_time)
        selected_end = ask_for_time("End", maximum_time)

    selected_start = snap_to_nearest_available_time(
        selected_start, available_timestamps, "Start time"
    )
    selected_end = snap_to_nearest_available_time(
        selected_end, available_timestamps, "End time"
    )

    if selected_start > selected_end:
        print("Start date-time was after end date-time, so I swapped them.")
        selected_start, selected_end = selected_end, selected_start

    print("\nSelected calculation window:")
    print(f"Start: {selected_start}")
    print(f"End:   {selected_end}\n")
    return selected_start, selected_end


# ---------------------------------------------------------------------------
# Section 3: Metadata flattening and per-image statistics
# ---------------------------------------------------------------------------

def flatten_metadata(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dictionaries into CSV/DataFrame columns."""
    flat: dict[str, Any] = {}

    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            flat.update(flatten_metadata(child, name))
    elif isinstance(value, (list, tuple)):
        flat[prefix] = json.dumps(value, ensure_ascii=False, allow_nan=True)
    else:
        flat[prefix] = value

    return flat


def remove_pixel_arrays(document: dict) -> dict:
    """Remove image-sized arrays after recording their shape/type."""
    for field_name in ("raw", "temperature"):
        pixels = document.pop(field_name, None)
        shape_name = f"{field_name}_shape"
        type_name = f"{field_name}_storage_type"

        if isinstance(pixels, list):
            document[shape_name] = [
                len(pixels),
                len(pixels[0]) if pixels and isinstance(pixels[0], list) else 0,
            ]
            document[type_name] = "JSON array"
        else:
            document[shape_name] = None
            document[type_name] = type(pixels).__name__ if pixels is not None else None

    return document


def calculate_temperature_metadata(document: dict) -> dict[str, Any]:
    """Calculate per-image apparent-temperature and raw-DN statistics.

    The CSV stores one row per image. It stores summary statistics from all
    pixels, not the full 2-D image, because the full image belongs in HDF5/Zarr
    if later needed for per-pixel mapping.
    """
    result: dict[str, Any] = {
        "calculated_temperature.status": "not_calculated",
        "calculated_temperature.method": "FLIR Planck, apparent temperature",
        "calculated_temperature.unit": "degC",
        "calculated_temperature.valid_pixel_count": 0,
        "calculated_temperature.min_c": None,
        "calculated_temperature.max_c": None,
        "calculated_temperature.mean_c": None,
        "calculated_temperature.median_c": None,
        "calculated_temperature.std_c": None,
        "pixel_temperature.unit": "degC",
        "pixel_temperature.valid_pixel_count": 0,
        "pixel_temperature.min_c": None,
        "pixel_temperature.max_c": None,
        "pixel_temperature.mean_c": None,
        "pixel_temperature.median_c": None,
        "pixel_temperature.std_c": None,
        "raw_dn.valid_pixel_count": 0,
        "raw_dn.min": None,
        "raw_dn.max": None,
        "raw_dn.mean": None,
        "raw_dn.median": None,
        "raw_dn.std": None,
    }

    raw = document.get("raw")
    calibration = document.get("calibration")
    if not isinstance(raw, list) or not isinstance(calibration, dict):
        result["calculated_temperature.status"] = "missing_raw_or_calibration"
        return result

    required = ("R", "B", "F", "J0", "J1")
    missing = [name for name in required if calibration.get(name) is None]
    if missing:
        result["calculated_temperature.status"] = "missing_calibration:" + ",".join(missing)
        return result

    try:
        R, B, F, J0, J1 = (float(calibration[name]) for name in required)
        if J1 == 0:
            raise ValueError("J1 is zero")

        raw_array = np.asarray(raw, dtype=np.float64)
        raw_valid = raw_array[np.isfinite(raw_array)]
        result["raw_dn.valid_pixel_count"] = int(raw_valid.size)
        if raw_valid.size:
            result.update({
                "raw_dn.min": float(np.min(raw_valid)),
                "raw_dn.max": float(np.max(raw_valid)),
                "raw_dn.mean": float(np.mean(raw_valid)),
                "raw_dn.median": float(np.median(raw_valid)),
                "raw_dn.std": float(np.std(raw_valid)),
            })

        signal = (raw_array - J0) / J1
        denominator = signal + F
        valid_domain = (denominator > 0) & (R / denominator > 1)
        temperature = np.full(raw_array.shape, np.nan, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            temperature[valid_domain] = B / np.log(R / denominator[valid_domain]) - 273.15

        valid = temperature[np.isfinite(temperature)]
        valid_count = int(valid.size)
        result["calculated_temperature.valid_pixel_count"] = valid_count
        result["pixel_temperature.valid_pixel_count"] = valid_count
        if valid.size == 0:
            result["calculated_temperature.status"] = "no_valid_pixels"
            return result

        min_c = float(np.min(valid))
        max_c = float(np.max(valid))
        mean_c = float(np.mean(valid))
        median_c = float(np.median(valid))
        std_c = float(np.std(valid))
        result.update({
            "calculated_temperature.status": "ok",
            "calculated_temperature.min_c": min_c,
            "calculated_temperature.max_c": max_c,
            "calculated_temperature.mean_c": mean_c,
            "calculated_temperature.median_c": median_c,
            "calculated_temperature.std_c": std_c,
            "pixel_temperature.min_c": min_c,
            "pixel_temperature.max_c": max_c,
            "pixel_temperature.mean_c": mean_c,
            "pixel_temperature.median_c": median_c,
            "pixel_temperature.std_c": std_c,
        })
    except (TypeError, ValueError, OverflowError) as error:
        result["calculated_temperature.status"] = f"error:{error}"

    return result


def find_json_value_span(object_text: str, field_name: str) -> tuple[int, int] | None:
    """Return character span of a JSON field value inside one object string."""
    match = re.search(rf'"{re.escape(field_name)}"\s*:', object_text)
    if not match:
        return None

    start = match.end()
    while start < len(object_text) and object_text[start].isspace():
        start += 1
    if start >= len(object_text):
        return None

    opening = object_text[start]
    if opening in "[{":
        closing = "]" if opening == "[" else "}"
        depth = 0
        in_string = False
        escape = False
        for position in range(start, len(object_text)):
            char = object_text[position]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return start, position + 1
        return None

    # Primitive value: read until comma or object end.
    end = start
    while end < len(object_text) and object_text[end] not in ",}":
        end += 1
    return start, end


def object_text_without_large_arrays(object_text: str) -> tuple[str, dict[str, Any]]:
    """Replace raw/temperature arrays with null before metadata JSON parsing."""
    replacements: list[tuple[int, int, str]] = []
    shape_metadata: dict[str, Any] = {}

    for field_name in ("raw", "temperature"):
        span = find_json_value_span(object_text, field_name)
        if span is None:
            shape_metadata[f"{field_name}_shape"] = None
            shape_metadata[f"{field_name}_storage_type"] = None
            continue

        value_text = object_text[span[0]:span[1]]
        shape_metadata[f"{field_name}_storage_type"] = "JSON array"
        if field_name == "raw":
            raw_shape = estimate_raw_shape_from_text(value_text)
            shape_metadata[f"{field_name}_shape"] = list(raw_shape) if raw_shape else None
        else:
            shape_metadata[f"{field_name}_shape"] = estimate_array_shape_light(value_text)
        replacements.append((span[0], span[1], "null"))

    clean_text_parts: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(replacements):
        clean_text_parts.append(object_text[cursor:start])
        clean_text_parts.append(replacement)
        cursor = end
    clean_text_parts.append(object_text[cursor:])
    return "".join(clean_text_parts), shape_metadata


def estimate_array_shape_light(array_text: str) -> list[int] | None:
    """Estimate 2-D JSON array shape without full JSON parsing."""
    if not array_text.strip().startswith("["):
        return None
    row_count = array_text.count("[") - 1
    first_row_start = array_text.find("[", 1)
    if first_row_start < 0:
        return [0, 0]
    first_row_end = array_text.find("]", first_row_start)
    if first_row_end < 0:
        return [row_count, 0]
    first_row = array_text[first_row_start + 1:first_row_end]
    values = np.fromstring(first_row, sep=",", dtype=np.float64)
    return [max(row_count, 0), int(values.size)]


def estimate_raw_shape_from_text(raw_text: str) -> tuple[int, int] | None:
    """Estimate raw image shape from JSON list-of-lists text."""
    shape = estimate_array_shape_light(raw_text)
    if not shape:
        return None
    return int(shape[0]), int(shape[1])


def raw_array_from_text(raw_text: str) -> tuple[np.ndarray, tuple[int, int] | None]:
    """Convert raw JSON numeric array text directly to NumPy."""
    shape = estimate_raw_shape_from_text(raw_text)
    numeric_text = raw_text.replace("[", " ").replace("]", " ")
    raw_values = np.fromstring(numeric_text, sep=",", dtype=np.float64)
    if shape and shape[0] * shape[1] == raw_values.size:
        return raw_values.reshape(shape), shape
    return raw_values, shape


def empty_temperature_result() -> dict[str, Any]:
    """Default per-image result fields."""
    return {
        "calculated_temperature.status": "not_calculated",
        "calculated_temperature.method": "FLIR Planck, apparent temperature",
        "calculated_temperature.unit": "degC",
        "calculated_temperature.valid_pixel_count": 0,
        "calculated_temperature.min_c": None,
        "calculated_temperature.max_c": None,
        "calculated_temperature.mean_c": None,
        "calculated_temperature.median_c": None,
        "calculated_temperature.std_c": None,
        "pixel_temperature.unit": "degC",
        "pixel_temperature.valid_pixel_count": 0,
        "pixel_temperature.min_c": None,
        "pixel_temperature.max_c": None,
        "pixel_temperature.mean_c": None,
        "pixel_temperature.median_c": None,
        "pixel_temperature.std_c": None,
        "raw_dn.valid_pixel_count": 0,
        "raw_dn.min": None,
        "raw_dn.max": None,
        "raw_dn.mean": None,
        "raw_dn.median": None,
        "raw_dn.std": None,
    }


def calculate_temperature_from_raw_array(
    raw_array: np.ndarray,
    calibration: dict | None,
) -> dict[str, Any]:
    """Calculate stats from already-parsed NumPy raw array."""
    result = empty_temperature_result()
    if raw_array.size == 0 or not isinstance(calibration, dict):
        result["calculated_temperature.status"] = "missing_raw_or_calibration"
        return result

    required = ("R", "B", "F", "J0", "J1")
    missing = [name for name in required if calibration.get(name) is None]
    if missing:
        result["calculated_temperature.status"] = "missing_calibration:" + ",".join(missing)
        return result

    try:
        R, B, F, J0, J1 = (float(calibration[name]) for name in required)
        if J1 == 0:
            raise ValueError("J1 is zero")

        raw_valid = raw_array[np.isfinite(raw_array)]
        result["raw_dn.valid_pixel_count"] = int(raw_valid.size)
        if raw_valid.size:
            result.update({
                "raw_dn.min": float(np.min(raw_valid)),
                "raw_dn.max": float(np.max(raw_valid)),
                "raw_dn.mean": float(np.mean(raw_valid)),
                "raw_dn.median": float(np.median(raw_valid)),
                "raw_dn.std": float(np.std(raw_valid)),
            })

        signal = (raw_array - J0) / J1
        denominator = signal + F
        valid_domain = (denominator > 0) & (R / denominator > 1)
        temperature = np.full(raw_array.shape, np.nan, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            temperature[valid_domain] = B / np.log(R / denominator[valid_domain]) - 273.15

        valid = temperature[np.isfinite(temperature)]
        valid_count = int(valid.size)
        result["calculated_temperature.valid_pixel_count"] = valid_count
        result["pixel_temperature.valid_pixel_count"] = valid_count
        if valid.size == 0:
            result["calculated_temperature.status"] = "no_valid_pixels"
            return result

        min_c = float(np.min(valid))
        max_c = float(np.max(valid))
        mean_c = float(np.mean(valid))
        median_c = float(np.median(valid))
        std_c = float(np.std(valid))
        result.update({
            "calculated_temperature.status": "ok",
            "calculated_temperature.min_c": min_c,
            "calculated_temperature.max_c": max_c,
            "calculated_temperature.mean_c": mean_c,
            "calculated_temperature.median_c": median_c,
            "calculated_temperature.std_c": std_c,
            "pixel_temperature.min_c": min_c,
            "pixel_temperature.max_c": max_c,
            "pixel_temperature.mean_c": mean_c,
            "pixel_temperature.median_c": median_c,
            "pixel_temperature.std_c": std_c,
        })
    except (TypeError, ValueError, OverflowError) as error:
        result["calculated_temperature.status"] = f"error:{error}"
    return result


def build_metadata_record_fast(
    object_text: str,
    source_file: Path,
    source_file_index: int,
) -> dict[str, Any]:
    """Fast row builder that avoids json.loads() on giant pixel arrays."""
    raw_span = find_json_value_span(object_text, "raw")
    raw_array = np.array([], dtype=np.float64)
    raw_shape = None
    if raw_span is not None:
        raw_array, raw_shape = raw_array_from_text(object_text[raw_span[0]:raw_span[1]])

    clean_text, shape_metadata = object_text_without_large_arrays(object_text)
    metadata = json.loads(clean_text)
    metadata.update(shape_metadata)
    if raw_shape is not None:
        metadata["raw_shape"] = [int(raw_shape[0]), int(raw_shape[1])]

    metadata["source_file"] = source_file.name
    metadata["source_path"] = str(source_file)
    metadata["source_file_index"] = source_file_index

    calculated_temperature = calculate_temperature_from_raw_array(
        raw_array=raw_array,
        calibration=metadata.get("calibration"),
    )
    flat = flatten_metadata(metadata)
    flat.update(calculated_temperature)
    return flat

def build_metadata_record(
    document: dict,
    source_file: Path,
    source_file_index: int,
) -> dict[str, Any]:
    """Return one flat row with all metadata and image-level statistics."""
    calculated_temperature = calculate_temperature_metadata(document)
    metadata = remove_pixel_arrays(document)

    metadata["source_file"] = source_file.name
    metadata["source_path"] = str(source_file)
    metadata["source_file_index"] = source_file_index

    flat = flatten_metadata(metadata)
    flat.update(calculated_temperature)
    return flat


# ---------------------------------------------------------------------------
# Section 4: Batch calculation for selected timeframe
# ---------------------------------------------------------------------------

def calculate_one_frame(
    item: tuple[str, int, Path, int, str],
) -> tuple[str, int, dict[str, Any]]:
    """Parse and calculate one selected frame.

    This function is intentionally top-level so it can be used by worker pools.
    """
    timestamp, global_frame_index, source_file, source_file_index, object_text = item
    flat = build_metadata_record_fast(object_text, source_file, source_file_index)
    return timestamp, global_frame_index, flat


def choose_calculation_worker_count(requested_workers: int | None) -> int:
    """Choose selected-frame calculation workers for this PC.

    On this machine os.cpu_count() reports 14 logical CPUs. The tuned default
    uses 12 workers, leaving a little room for Windows, disk I/O, and Spyder.
    """
    cpu_count = os.cpu_count() or 1
    if requested_workers is None or requested_workers <= 0:
        return min(PC_TUNED_CALC_WORKERS, max(cpu_count - 2, 1))
    return max(requested_workers, 1)


def choose_scan_worker_count(requested_workers: int | None) -> int:
    """Choose timestamp scan workers for this PC.

    Timestamp scanning is disk I/O plus regex. Too many scan workers can make
    the disk thrash, so the tuned default is lower than calculation workers.
    """
    cpu_count = os.cpu_count() or 1
    if requested_workers is None or requested_workers <= 0:
        return min(PC_TUNED_SCAN_WORKERS, max(cpu_count - 2, 1))
    return max(requested_workers, 1)


def process_batch(
    batch: list[tuple[str, int, Path, int, str]],
    records: list[tuple[str, int, dict[str, Any]]],
    all_columns: set[str],
    workers: int = 1,
) -> None:
    """Parse and calculate one batch of selected frame objects.

    Threads avoid copying very large JSON strings between Windows processes.
    NumPy operations can still use optimized CPU kernels internally.
    """
    if workers <= 1 or len(batch) <= 1:
        results = [calculate_one_frame(item) for item in batch]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(calculate_one_frame, batch))

    for timestamp, global_frame_index, flat in results:
        all_columns.update(flat)
        records.append((timestamp, global_frame_index, flat))


def preferred_columns(all_columns: set[str]) -> list[str]:
    """Put the most useful postprocessing columns first."""
    preferred = [
        "timestamp", "_id", "source_file", "source_file_index", "source_path",
        "raw_shape", "raw_storage_type",
        "temperature_shape", "temperature_storage_type",
        "calculated_temperature.status",
        "calculated_temperature.method",
        "calculated_temperature.unit",
        "pixel_temperature.valid_pixel_count",
        "pixel_temperature.min_c",
        "pixel_temperature.max_c",
        "pixel_temperature.mean_c",
        "pixel_temperature.median_c",
        "pixel_temperature.std_c",
        "raw_dn.valid_pixel_count",
        "raw_dn.min",
        "raw_dn.max",
        "raw_dn.mean",
        "raw_dn.median",
        "raw_dn.std",
        "calculated_temperature.valid_pixel_count",
        "calculated_temperature.min_c",
        "calculated_temperature.max_c",
        "calculated_temperature.mean_c",
        "calculated_temperature.median_c",
        "calculated_temperature.std_c",
        "calibration.R",
        "calibration.B",
        "calibration.F",
        "calibration.J0",
        "calibration.J1",
        "emissivity",
        "object_emissivity",
        "reflected_temperature",
        "atmospheric_temperature",
        "relative_humidity",
        "distance",
        "transmission",
        "window_temperature",
        "window_transmission",
    ]
    columns = [name for name in preferred if name in all_columns]
    columns += sorted(all_columns - set(columns))
    return columns if columns else preferred


def selected_timestamp_entries(
    timestamp_index: list[tuple[str, Path, int]],
    start_time: str,
    end_time: str,
) -> list[tuple[str, Path, int]]:
    """Return timestamp index entries inside the selected time window."""
    return [
        entry for entry in timestamp_index
        if start_time <= entry[0] <= end_time
    ]


def count_selected_timestamps(
    timestamp_index: list[tuple[str, Path, int]],
    start_time: str,
    end_time: str,
) -> int:
    """Count selected frames from the timestamp byte index."""
    return len(selected_timestamp_entries(timestamp_index, start_time, end_time))


def calculate_start_offsets(
    timestamp_index: list[tuple[str, Path, int]],
    start_time: str,
    end_time: str,
) -> dict[Path, int]:
    """Calculate seek offsets so Step 2 starts near selected frames."""
    offsets: dict[Path, int] = {}
    for _, source_file, timestamp_byte_offset in selected_timestamp_entries(
        timestamp_index, start_time, end_time
    ):
        object_start = find_object_start_before_timestamp(source_file, timestamp_byte_offset)
        if source_file not in offsets or object_start < offsets[source_file]:
            offsets[source_file] = object_start
    return offsets

def format_duration(seconds: float) -> str:
    """Format seconds as a compact human-readable duration."""
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {sec}s"

def extract_metadata(
    json_files: list[Path],
    output_csv: Path,
    start_time: str,
    end_time: str,
    batch_size: int = 100,
    progress_every: int = 1000,
    workers: int = 1,
    selected_total: int | None = None,
    start_offsets: dict[Path, int] | None = None,
) -> tuple[int, list[str]]:
    """Calculate/export only frames inside the selected time window."""
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    all_columns: set[str] = set()
    records: list[tuple[str, int, dict[str, Any]]] = []
    batch: list[tuple[str, int, Path, int, str]] = []
    selected_count = 0
    scanned_count = 0
    completed_batches = 0
    total_batches = None
    if selected_total is not None:
        total_batches = max((selected_total + max(batch_size, 1) - 1) // max(batch_size, 1), 1)
    started = time.monotonic()
    last_progress_line = ""

    def run_current_batch() -> None:
        """Run current batch and print durable progress lines for Spyder."""
        nonlocal batch, completed_batches, last_progress_line
        if not batch:
            return

        completed_batches += 1
        current_batch_number = completed_batches
        current_batch_size = len(batch)
        batch_start_time = batch[0][0]
        batch_end_time = batch[-1][0]
        batch_started = time.monotonic()

        if last_progress_line:
            print()
            last_progress_line = ""

        if total_batches is not None:
            print(
                f"Batch {current_batch_number}/{total_batches} started: "
                f"{current_batch_size} frame(s), "
                f"time {batch_start_time} -> {batch_end_time}"
            )
        else:
            print(
                f"Batch {current_batch_number} started: "
                f"{current_batch_size} frame(s), "
                f"time {batch_start_time} -> {batch_end_time}"
            )

        process_batch(batch, records, all_columns, workers=workers)
        batch.clear()

        batch_seconds = time.monotonic() - batch_started
        elapsed_total = time.monotonic() - started
        if total_batches is not None and current_batch_number > 0:
            average_batch_seconds = elapsed_total / current_batch_number
            remaining_batches = max(total_batches - current_batch_number, 0)
            eta_text = format_duration(average_batch_seconds * remaining_batches)
            print(
                f"Batch {current_batch_number}/{total_batches} finished: "
                f"{format_duration(batch_seconds)} for this batch, "
                f"selected frames {selected_count}/{selected_total}, "
                f"elapsed {format_duration(elapsed_total)}, ETA {eta_text}"
            )
        else:
            print(
                f"Batch {current_batch_number} finished: "
                f"{format_duration(batch_seconds)} for this batch, "
                f"selected frames {selected_count}, "
                f"elapsed {format_duration(elapsed_total)}"
            )

    for scanned_count, (source_file, source_file_index, object_text) in enumerate(
        iter_all_object_texts(json_files, start_offsets=start_offsets), start=1
    ):
        timestamp = extract_timestamp_from_object_text(object_text)

        if timestamp and start_time and timestamp < start_time:
            continue
        if timestamp and end_time and timestamp > end_time:
            if selected_count > 0:
                if last_progress_line:
                    print()
                    last_progress_line = ""
                print(
                    f"Reached end of selected window at {timestamp}; "
                    "stopping file scan early."
                )
                break
            continue

        selected_count += 1
        batch.append((timestamp, scanned_count, source_file, source_file_index, object_text))

        if len(batch) >= batch_size:
            run_current_batch()

        if progress_every > 0 and scanned_count % progress_every == 0:
            elapsed = max(time.monotonic() - started, 0.001)
            total_text = selected_total if selected_total is not None else "?"
            batch_text = total_batches if total_batches is not None else "?"
            last_progress_line = (
                f"\rSelection pass: scanned {scanned_count:,}; "
                f"selected {selected_count:,}/{total_text}; "
                f"batch {completed_batches}/{batch_text} "
                f"({scanned_count / elapsed:.2f} scanned frames/s)"
            )
            print(last_progress_line, end="", flush=True)

    if batch:
        run_current_batch()

    if last_progress_line:
        print()

    columns = preferred_columns(all_columns)
    records.sort(key=lambda item: (item[0], item[1]))

    print("Writing selected metadata CSV...")
    with output_csv.open("w", newline="", encoding="utf-8-sig") as target:
        writer = csv.DictWriter(target, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for _, _, flat in records:
            writer.writerow(flat)
    print("CSV writing finished.")

    return selected_count, columns


# ---------------------------------------------------------------------------
# Section 5: DataFrame creation in the same code
# ---------------------------------------------------------------------------

def load_output_as_dataframe(output_csv: Path):
    """Load selected CSV as df for Spyder, if pandas is installed."""
    global df, selected_dataframe

    try:
        import pandas as pd
    except ImportError:
        print("pandas is not installed, so df was not created. CSV is still ready.")
        return None

    df = pd.read_csv(output_csv)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.sort_values("timestamp").reset_index(drop=True)

    selected_dataframe = df
    print("\nDataFrame ready: variable name is df")
    print(f"df rows: {len(df):,}")
    print(f"df columns: {len(df.columns):,}")
    if "timestamp" in df.columns and len(df):
        print(f"df start: {df['timestamp'].min()}")
        print(f"df end:   {df['timestamp'].max()}")
    return df


# ---------------------------------------------------------------------------
# Section 6: Command-line entry point
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-code FLIR folder/file processor with fast time scan and selected-window calculation."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=DEFAULT_INPUT,
        help="Input .json file or folder containing .json files.",
    )
    parser.add_argument(
        "--recursive-json",
        action="store_true",
        help="When input is a folder, include .json files in subfolders too.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("FLIR_Zeppelin_metadata_by_time.csv"),
        help="Sorted selected-timeframe CSV destination.",
    )
    parser.add_argument(
        "--start-time",
        type=str,
        default=None,
        help="Optional processing start date-time, e.g. '2026-06-10 13:40:00'.",
    )
    parser.add_argument(
        "--end-time",
        type=str,
        default=None,
        help="Optional processing end date-time, e.g. '2026-06-10 14:10:00'.",
    )
    parser.add_argument(
        "--no-time-prompt",
        action="store_true",
        help="Do not ask interactively; use times, auto-window, or full range.",
    )
    parser.add_argument(
        "--auto-window-minutes",
        type=float,
        default=None,
        help="Automatically choose a random N-minute window after scanning min/max time.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=PC_TUNED_BATCH_SIZE,
        help="Number of selected frames to parse/calculate at once. Default is PC-tuned.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=5000,
        help="Print progress after this many scanned frames; use 0 to disable.",
    )
    parser.add_argument(
        "--scan-chunk-mb",
        type=int,
        default=PC_TUNED_SCAN_CHUNK_MB,
        help="Timestamp scan chunk size in MB per scan task. Default is PC-tuned.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Selected-frame calculation workers. 0 = PC-tuned auto, 12 workers on this machine.",
    )
    parser.add_argument(
        "--scan-workers",
        type=int,
        default=0,
        help="Timestamp scan workers. 0 = PC-tuned auto, 8 workers on this machine.",
    )
    parser.add_argument(
        "--full-power",
        action="store_true",
        help="Use this PC's maximum-speed profile: 14 scan workers, 14 calculation workers, 512 MB scan chunks, batch size 100.",
    )
    parser.add_argument(
        "--skip-df",
        action="store_true",
        help="Do not load the output CSV as pandas df at the end.",
    )
    return parser.parse_args()



def apply_performance_profile(arguments: argparse.Namespace) -> None:
    """Apply normal or full-power performance settings in-place."""
    if not arguments.full_power:
        return

    # Full-power intentionally overrides defaults. If you need a custom value,
    # do not use --full-power; pass --workers/--batch-size/etc manually.
    arguments.scan_workers = FULL_POWER_SCAN_WORKERS
    arguments.workers = FULL_POWER_CALC_WORKERS
    arguments.scan_chunk_mb = FULL_POWER_SCAN_CHUNK_MB
    arguments.batch_size = FULL_POWER_BATCH_SIZE


def main() -> int:
    arguments = parse_arguments()
    apply_performance_profile(arguments)
    input_path = arguments.input.resolve()
    output_csv = arguments.output.resolve()

    try:
        json_files = find_json_files(input_path, recursive=arguments.recursive_json)
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    print(f"Input:    {input_path}")
    print(f"JSON files found: {len(json_files):,}")
    for index, json_file in enumerate(json_files[:10], start=1):
        print(f"  {index:>3}. {json_file}")
    if len(json_files) > 10:
        print(f"  ... {len(json_files) - 10:,} more JSON files")
    print("Mode:     virtual concatenate/merge of all JSON files")
    print("Note:     no extra giant merged JSON file is created")
    worker_count = choose_calculation_worker_count(arguments.workers)
    scan_worker_count = choose_scan_worker_count(arguments.scan_workers)
    print(f"CSV:      {output_csv}")
    if arguments.full_power:
        print("Profile:  FULL POWER for Intel Core Ultra 7 165U / 32 GB RAM")
    else:
        print("Profile:  PC-tuned defaults for Intel Core Ultra 7 165U / 32 GB RAM")
    print(f"CPU:      using {scan_worker_count} worker(s) for timestamp scan")
    print(f"CPU:      using {worker_count} worker(s) for selected-frame calculation")
    print(f"RAM:      scan chunk = {arguments.scan_chunk_mb} MB per scan task; calculation batch = {arguments.batch_size} frame(s)")

    print("\nStep 1/4: fast timestamp scan only, no raw image parsing...")
    minimum_time, maximum_time, total_frames, available_timestamps, timestamp_index = scan_time_range(
        json_files=json_files,
        progress_every=max(arguments.progress_every, 5000),
        scan_chunk_mb=arguments.scan_chunk_mb,
        scan_workers=scan_worker_count,
    )
    print(f"Timestamp scan finished: {total_frames:,} frames found.")

    selected_start, selected_end = choose_time_window(
        minimum_time=minimum_time,
        maximum_time=maximum_time,
        available_timestamps=available_timestamps,
        start_time=arguments.start_time,
        end_time=arguments.end_time,
        no_time_prompt=arguments.no_time_prompt,
        auto_window_minutes=arguments.auto_window_minutes,
    )

    selected_total = count_selected_timestamps(
        timestamp_index, selected_start, selected_end
    )
    start_offsets = calculate_start_offsets(
        timestamp_index, selected_start, selected_end
    )
    total_batches = max((selected_total + max(arguments.batch_size, 1) - 1) // max(arguments.batch_size, 1), 1)
    print("Step 2/4: parse/calculate only selected frames, batch by batch...")
    print(f"Selected frames expected: {selected_total:,}")
    print(f"Total calculation batches: {total_batches:,}")
    if start_offsets:
        print("Seek index: starting selected-frame scan near selected timestamp, not at file beginning.")
        for source_file, offset in start_offsets.items():
            print(f"  {source_file.name}: byte offset {offset:,}")
    count, columns = extract_metadata(
        json_files=json_files,
        output_csv=output_csv,
        start_time=selected_start,
        end_time=selected_end,
        batch_size=max(arguments.batch_size, 1),
        progress_every=arguments.progress_every,
        workers=worker_count,
        selected_total=selected_total,
        start_offsets=start_offsets,
    )
    print(f"Finished: {count:,} selected frames written in timestamp order.")
    print(f"CSV columns: {len(columns):,}")

    print("\nStep 3/4: create df from selected-timeframe CSV...")
    if arguments.skip_df:
        print("Skipped df creation because --skip-df was used.")
    else:
        load_output_as_dataframe(output_csv)

    print("\nStep 4/4: complete.")
    print(f"Selected CSV: {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
