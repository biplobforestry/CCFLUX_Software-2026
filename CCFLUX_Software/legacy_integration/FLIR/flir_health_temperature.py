#!/usr/bin/env python3
"""Fast FLIR acquisition health, time filtering, and radiometric temperature QC.

The input format is the Zeppelin JSON array containing one document per frame:
timestamp, calibration, raw_stats, and a 2-D raw count array. The file is
indexed by byte-range timestamp scans and is never loaded in full.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import math
import re
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from flir_radiometry import (
    CALIBRATION_FIELDS,
    CorrectionInputs,
    counts_to_temperature,
    validate_calibration,
)


TIMESTAMP_BYTES_RE = re.compile(
    rb'"timestamp"\s*:\s*(?:\{\s*"\$date"\s*:\s*)?'
    rb'"([^"\\]*(?:\\.[^"\\]*)*)"'
)
CALIBRATION_BYTES_RE = re.compile(
    rb'"calibration"\s*:\s*(\{[^{}]*\})', re.DOTALL
)
RAW_STATS_BYTES_RE = re.compile(
    rb'"raw_stats"\s*:\s*(\{[^{}]*\})', re.DOTALL
)
OBJECT_DELIMITER_BYTES_RE = re.compile(rb"}\s*,\s*{")

EXPECTED_WIDTH = 640
EXPECTED_HEIGHT = 480
CAMERA_MODEL_FROM_PROJECT_DOCUMENT = "FLIR A70 Thermal Core 95 deg"
CAMERA_PART_NUMBER_FROM_PROJECT_DOCUMENT = "89995-0101"
FORMULA_SOURCE = (
    "https://flir.custhelp.com/app/answers/detail/a_id/3321/"
    "~/flir-cameras---temperature-measurement-formula"
)
SPECIFICATION_SOURCE = (
    "local project document: Teledyne FLIR-89995-0101-DB-1_DE.pdf"
)


def parse_timestamp(value: Any) -> dt.datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        for format_string in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                parsed = dt.datetime.strptime(text, format_string)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def iso_utc(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(dt.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def scan_one_range(
    task: tuple[Path, int, int, int],
) -> tuple[int, list[tuple[dt.datetime, str, Path, int]]]:
    path, start, end, overlap = task
    read_start = max(0, start - overlap)
    read_end = min(path.stat().st_size, end + overlap)
    with path.open("rb") as stream:
        stream.seek(read_start)
        payload = stream.read(read_end - read_start)
    entries = []
    for match in TIMESTAMP_BYTES_RE.finditer(payload):
        absolute = read_start + match.start()
        if not start <= absolute < end:
            continue
        try:
            original = match.group(1).decode("unicode_escape")
        except UnicodeDecodeError:
            continue
        parsed = parse_timestamp(original)
        if parsed is not None:
            entries.append((parsed, original, path, absolute))
    return end - start, entries


def scan_timestamps(
    paths: list[Path],
    workers: int,
    chunk_mb: int,
) -> tuple[list[tuple[dt.datetime, str, Path, int]], float]:
    chunk = max(1, chunk_mb) * 1024 * 1024
    tasks = []
    total_bytes = 0
    for path in paths:
        size = path.stat().st_size
        total_bytes += size
        for start in range(0, size, chunk):
            tasks.append((path, start, min(start + chunk, size), 4096))
    started = time.perf_counter()
    entries: list[tuple[dt.datetime, str, Path, int]] = []
    completed_bytes = 0
    with ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(tasks)))
    ) as executor:
        for index, (byte_count, part) in enumerate(
            executor.map(scan_one_range, tasks), 1
        ):
            completed_bytes += byte_count
            entries.extend(part)
            elapsed = max(time.perf_counter() - started, 1e-9)
            print(
                f"\rtimestamp scan {index}/{len(tasks)}; "
                f"{completed_bytes / 1024**3:.2f}/{total_bytes / 1024**3:.2f} GiB; "
                f"{len(entries):,} frames; "
                f"{completed_bytes / 1024**2 / elapsed:.1f} MiB/s",
                end="",
                flush=True,
            )
    print()
    entries.sort(key=lambda item: (item[0], str(item[2]), item[3]))
    return entries, time.perf_counter() - started


def write_timestamp_index(
    path: Path,
    entries: list[tuple[dt.datetime, str, Path, int]],
) -> None:
    rows = []
    stat_cache: dict[Path, Any] = {}
    for parsed, original, source, offset in entries:
        stat = stat_cache.setdefault(source, source.stat())
        rows.append(
            {
                "timestamp_utc": iso_utc(parsed),
                "timestamp_original": original,
                "source_file": str(source),
                "timestamp_byte_offset": offset,
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            }
        )
    write_csv(path, rows)


def load_timestamp_index(
    path: Path,
) -> list[tuple[dt.datetime, str, Path, int]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("timestamp index is empty")
    entries = []
    verified: dict[Path, tuple[int, int]] = {}
    for row in rows:
        source = Path(row["source_file"])
        expected = (
            int(row["source_size_bytes"]),
            int(row["source_mtime_ns"]),
        )
        if source not in verified:
            stat = source.stat()
            actual = (stat.st_size, stat.st_mtime_ns)
            if actual != expected:
                raise ValueError(
                    f"source changed since index creation: {source}"
                )
            verified[source] = actual
        parsed = parse_timestamp(row["timestamp_utc"])
        if parsed is None:
            raise ValueError("timestamp index contains an invalid UTC value")
        entries.append(
            (
                parsed,
                row["timestamp_original"],
                source,
                int(row["timestamp_byte_offset"]),
            )
        )
    entries.sort(key=lambda item: (item[0], str(item[2]), item[3]))
    return entries


def read_frame_header(
    entry: tuple[dt.datetime, str, Path, int],
    header_bytes: int = 8192,
) -> dict[str, Any]:
    parsed, original, path, offset = entry
    with path.open("rb") as stream:
        stream.seek(offset)
        payload = stream.read(header_bytes)
    calibration_match = CALIBRATION_BYTES_RE.search(payload)
    raw_stats_match = RAW_STATS_BYTES_RE.search(payload)
    calibration = None
    raw_stats = None
    errors = []
    try:
        if calibration_match:
            calibration = json.loads(calibration_match.group(1))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        errors.append(f"calibration_parse:{error}")
    try:
        if raw_stats_match:
            raw_stats = json.loads(raw_stats_match.group(1))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        errors.append(f"raw_stats_parse:{error}")
    missing = []
    if isinstance(calibration, dict):
        missing = [
            name
            for name in CALIBRATION_FIELDS
            if calibration.get(name) is None
        ]
    else:
        missing = list(CALIBRATION_FIELDS)
    raw_min = raw_stats.get("min") if isinstance(raw_stats, dict) else None
    raw_max = raw_stats.get("max") if isinstance(raw_stats, dict) else None
    raw_mean = raw_stats.get("mean") if isinstance(raw_stats, dict) else None
    stats_consistent = (
        all(isinstance(value, (int, float)) for value in (raw_min, raw_max, raw_mean))
        and raw_min <= raw_mean <= raw_max
    )
    all_zero = raw_min == 0 and raw_max == 0 and raw_mean == 0
    status = "PASS"
    flags = []
    if missing:
        status = "FAIL"
        flags.append("missing_calibration")
    if not isinstance(raw_stats, dict) or not stats_consistent:
        status = "FAIL"
        flags.append("missing_or_inconsistent_raw_stats")
    if all_zero:
        status = "FAIL"
        flags.append("all_zero_frame")
    if errors:
        status = "FAIL"
        flags.extend(errors)
    row: dict[str, Any] = {
        "_datetime": parsed,
        "timestamp_utc": iso_utc(parsed),
        "timestamp_original": original,
        "source_file": str(path),
        "timestamp_byte_offset": offset,
        "header_status": status,
        "health_flags": ";".join(flags),
        "calibration_complete": not missing,
        "missing_calibration_fields": ";".join(missing),
        "raw_stats_present": isinstance(raw_stats, dict),
        "raw_stats_consistent": stats_consistent,
        "raw_all_zero": all_zero,
        "raw_min_dn_header": raw_min if raw_min is not None else "",
        "raw_mean_dn_header": raw_mean if raw_mean is not None else "",
        "raw_max_dn_header": raw_max if raw_max is not None else "",
    }
    for field in CALIBRATION_FIELDS:
        row[f"calibration_{field}"] = (
            calibration.get(field, "") if isinstance(calibration, dict) else ""
        )
    row["fast_apparent_temperature_from_mean_dn_c"] = ""
    if (
        not missing
        and isinstance(raw_mean, (int, float))
        and not all_zero
    ):
        try:
            temperature, _ = counts_to_temperature(
                np.array([raw_mean], dtype=np.float64),
                calibration,
                inputs=None,
            )
            if np.isfinite(temperature[0]):
                row["fast_apparent_temperature_from_mean_dn_c"] = float(
                    temperature[0]
                )
        except (TypeError, ValueError, FloatingPointError):
            pass
    return row


def inspect_all_headers(
    entries: list[tuple[dt.datetime, str, Path, int]],
    workers: int,
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(entries)))
    ) as executor:
        rows = list(executor.map(read_frame_header, entries))
    previous = None
    for index, row in enumerate(rows, 1):
        row["frame_index"] = index
        current = row["_datetime"]
        row["interval_from_previous_s"] = (
            (current - previous).total_seconds() if previous else ""
        )
        previous = current
    return rows, time.perf_counter() - started


def find_object_start(path: Path, timestamp_offset: int) -> int:
    for backtrack in (64 * 1024, 256 * 1024, 1024 * 1024):
        start = max(0, timestamp_offset - backtrack)
        with path.open("rb") as stream:
            stream.seek(start)
            payload = stream.read(timestamp_offset - start)
        matches = list(OBJECT_DELIMITER_BYTES_RE.finditer(payload))
        if matches:
            return start + matches[-1].end() - 1
        if start == 0:
            first = payload.find(b"{")
            if first >= 0:
                return first
    raise ValueError(f"could not locate object start before byte {timestamp_offset}")


def object_spans(
    entries: list[tuple[dt.datetime, str, Path, int]]
) -> list[tuple[int, int]]:
    starts = [find_object_start(entry[2], entry[3]) for entry in entries]
    spans = [(start, entries[index][2].stat().st_size) for index, start in enumerate(starts)]
    by_path: dict[Path, list[int]] = {}
    for index, entry in enumerate(entries):
        by_path.setdefault(entry[2], []).append(index)
    for path, indices in by_path.items():
        physical_order = sorted(indices, key=lambda index: starts[index])
        for position, index in enumerate(physical_order):
            end = (
                starts[physical_order[position + 1]]
                if position + 1 < len(physical_order)
                else path.stat().st_size
            )
            spans[index] = (starts[index], end)
    return spans


def raw_array_from_object(
    path: Path,
    start: int,
    end: int,
) -> tuple[np.ndarray, tuple[int, int]]:
    with path.open("rb") as stream:
        stream.seek(start)
        payload = stream.read(end - start)
    marker = re.search(rb'"raw"\s*:\s*\[', payload)
    if marker is None:
        raise ValueError("raw array is missing")
    raw_start = payload.find(b"[", marker.start())
    object_end = payload.rfind(b"}")
    raw_end = payload.rfind(
        b"]", raw_start + 1, object_end if object_end > raw_start else len(payload)
    )
    if raw_start < 0 or raw_end <= raw_start:
        raise ValueError("raw array boundaries are invalid")
    raw_text = payload[raw_start : raw_end + 1]
    first_row_start = raw_text.find(b"[", 1)
    first_row_end = raw_text.find(b"]", first_row_start + 1)
    if first_row_start < 0 or first_row_end < 0:
        raise ValueError("raw array has no complete first row")
    columns = raw_text[first_row_start + 1 : first_row_end].count(b",") + 1
    rows = raw_text.count(b"[") - 1
    numeric = raw_text.translate(None, b"[] \r\n\t")
    values = np.fromstring(numeric.decode("ascii"), sep=",", dtype=np.float64)
    if rows <= 0 or columns <= 0 or values.size != rows * columns:
        raise ValueError(
            f"raw array shape mismatch: inferred {rows}x{columns}, "
            f"parsed {values.size} values"
        )
    return values.reshape(rows, columns), (rows, columns)


def numeric_stats(values: np.ndarray, prefix: str) -> dict[str, Any]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {
            f"{prefix}_valid_pixel_count": 0,
            f"{prefix}_min": "",
            f"{prefix}_p01": "",
            f"{prefix}_mean": "",
            f"{prefix}_median": "",
            f"{prefix}_p99": "",
            f"{prefix}_max": "",
            f"{prefix}_std": "",
        }
    percentiles = np.percentile(finite, [1, 50, 99])
    return {
        f"{prefix}_valid_pixel_count": int(finite.size),
        f"{prefix}_min": float(np.min(finite)),
        f"{prefix}_p01": float(percentiles[0]),
        f"{prefix}_mean": float(np.mean(finite)),
        f"{prefix}_median": float(percentiles[1]),
        f"{prefix}_p99": float(percentiles[2]),
        f"{prefix}_max": float(np.max(finite)),
        f"{prefix}_std": float(np.std(finite)),
    }


def calibration_from_health(row: dict[str, Any]) -> dict[str, float]:
    return {
        name: float(row[f"calibration_{name}"])
        for name in CALIBRATION_FIELDS
    }


def process_one_temperature(
    item: tuple[
        int,
        dict[str, Any],
        tuple[dt.datetime, str, Path, int],
        tuple[int, int],
    ],
    correction: CorrectionInputs | None,
    roi: tuple[int, int, int, int] | None,
    save_directory: Path | None,
    valid_range: tuple[float, float] | None,
) -> dict[str, Any]:
    frame_index, health, entry, span = item
    row: dict[str, Any] = {
        key: value for key, value in health.items() if key != "_datetime"
    }
    row["temperature_status"] = "not_processed"
    row["temperature_error"] = ""
    try:
        calibration = calibration_from_health(health)
        validate_calibration(calibration)
        raw, shape = raw_array_from_object(entry[2], span[0], span[1])
        height, width = shape
        row["raw_height_px"] = height
        row["raw_width_px"] = width
        row["dimension_status"] = (
            "PASS"
            if (height, width) == (EXPECTED_HEIGHT, EXPECTED_WIDTH)
            else "FAIL"
        )
        row.update(numeric_stats(raw, "raw_dn"))
        row["raw_zero_fraction"] = float(np.count_nonzero(raw == 0) / raw.size)
        temperature, diagnostics = counts_to_temperature(
            raw, calibration, correction
        )
        analysis = temperature
        if roi is not None:
            x0, y0, x1, y1 = roi
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                raise ValueError(
                    f"ROI {roi} is outside image dimensions {width}x{height}"
                )
            analysis = temperature[y0:y1, x0:x1]
        row["roi_xyxy"] = ",".join(map(str, roi)) if roi else "full_frame"
        row.update(numeric_stats(analysis, "temperature_c"))
        row["temperature_invalid_fraction"] = diagnostics[
            "invalid_temperature_fraction"
        ]
        row["temperature_method"] = diagnostics["method"]
        row["temperature_equation"] = diagnostics["equation"]
        row["atmospheric_transmission"] = diagnostics[
            "atmospheric_transmission"
        ]
        row["water_content_g_m3"] = diagnostics["water_content_g_m3"]
        row["reflected_radiance_term"] = diagnostics[
            "reflected_radiance_term"
        ]
        row["atmospheric_radiance_term"] = diagnostics[
            "atmospheric_radiance_term"
        ]
        row["external_optics_radiance_term"] = diagnostics[
            "external_optics_radiance_term"
        ]
        if valid_range is not None:
            finite = analysis[np.isfinite(analysis)]
            outside = (
                (finite < valid_range[0]) | (finite > valid_range[1])
            )
            row["outside_declared_temperature_range_fraction"] = (
                float(np.count_nonzero(outside) / finite.size)
                if finite.size
                else ""
            )
        else:
            row["outside_declared_temperature_range_fraction"] = ""
        row["temperature_status"] = (
            "PASS"
            if row["temperature_c_valid_pixel_count"] > 0
            and row["dimension_status"] == "PASS"
            else "FAIL"
        )
        if save_directory is not None:
            save_directory.mkdir(parents=True, exist_ok=True)
            map_path = save_directory / f"frame_{frame_index:06d}.npz"
            np.savez_compressed(
                map_path,
                temperature_c=temperature.astype(np.float32),
                timestamp_utc=row["timestamp_utc"],
            )
            row["temperature_map_npz"] = str(map_path)
        else:
            row["temperature_map_npz"] = ""
    except Exception as error:
        row["temperature_status"] = "FAIL"
        row["temperature_error"] = f"{type(error).__name__}: {error}"
    return row


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key.startswith("_"):
                continue
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def select_indices(
    entries: list[tuple[dt.datetime, str, Path, int]],
    health: list[dict[str, Any]],
    start: dt.datetime | None,
    end: dt.datetime | None,
    every_nth: int,
    include_zero: bool,
) -> list[int]:
    times = [entry[0] for entry in entries]
    left = bisect.bisect_left(times, start) if start else 0
    right = bisect.bisect_right(times, end) if end else len(entries)
    indices = list(range(left, right))
    if not include_zero:
        indices = [index for index in indices if not health[index]["raw_all_zero"]]
    return indices[:: max(1, every_nth)]


def acquisition_summary(
    health: list[dict[str, Any]],
    expected_rate_hz: float | None,
    gap_seconds: float | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    times = [row["_datetime"] for row in health]
    intervals = [
        (times[index] - times[index - 1]).total_seconds()
        for index in range(1, len(times))
    ]
    positive = [value for value in intervals if value > 0]
    median = statistics.median(positive) if positive else None
    expected_interval = (
        1.0 / expected_rate_hz
        if expected_rate_hz is not None and expected_rate_hz > 0
        else median
    )
    threshold_basis = expected_interval or median
    threshold = (
        gap_seconds
        if gap_seconds is not None
        else (
            max(1.5 * threshold_basis, threshold_basis + 0.05)
            if threshold_basis is not None
            else math.inf
        )
    )
    gaps = []
    for index, interval in enumerate(intervals, 1):
        if interval > threshold:
            missing = (
                max(round(interval / expected_interval) - 1, 0)
                if expected_interval
                else ""
            )
            gaps.append(
                {
                    "previous_frame_index": index,
                    "next_frame_index": index + 1,
                    "gap_start_utc": health[index - 1]["timestamp_utc"],
                    "gap_end_utc": health[index]["timestamp_utc"],
                    "gap_seconds": interval,
                    "estimated_missing_frames": missing,
                }
            )
    calibration_signatures = {
        tuple(row[f"calibration_{name}"] for name in CALIBRATION_FIELDS)
        for row in health
        if row["calibration_complete"]
    }
    duration = (
        (times[-1] - times[0]).total_seconds() if len(times) > 1 else 0.0
    )
    proxy_temperatures = [
        float(row["fast_apparent_temperature_from_mean_dn_c"])
        for row in health
        if isinstance(
            row["fast_apparent_temperature_from_mean_dn_c"], (int, float)
        )
    ]
    proxy_change = (
        proxy_temperatures[-1] - proxy_temperatures[0]
        if len(proxy_temperatures) > 1
        else None
    )
    summary = {
        "camera_model_from_project_document": CAMERA_MODEL_FROM_PROJECT_DOCUMENT,
        "camera_model_embedded_in_sample": False,
        "camera_part_number_from_project_document": CAMERA_PART_NUMBER_FROM_PROJECT_DOCUMENT,
        "frame_count": len(health),
        "start_time_utc": iso_utc(times[0]) if times else "",
        "end_time_utc": iso_utc(times[-1]) if times else "",
        "duration_seconds": duration,
        "observed_mean_rate_hz": (
            (len(times) - 1) / duration if duration > 0 else None
        ),
        "median_interval_s": median,
        "minimum_interval_s": min(positive) if positive else None,
        "maximum_interval_s": max(positive) if positive else None,
        "gap_threshold_s": threshold if math.isfinite(threshold) else None,
        "gap_count": len(gaps),
        "estimated_missing_frames": sum(
            int(row["estimated_missing_frames"])
            for row in gaps
            if row["estimated_missing_frames"] != ""
        ),
        "duplicate_timestamp_count": len(times) - len(set(times)),
        "all_zero_frame_count": sum(row["raw_all_zero"] for row in health),
        "missing_calibration_frame_count": sum(
            not row["calibration_complete"] for row in health
        ),
        "inconsistent_raw_stats_frame_count": sum(
            not row["raw_stats_consistent"] for row in health
        ),
        "calibration_signature_count": len(calibration_signatures),
        "calibration_stable": len(calibration_signatures) == 1,
        "fast_apparent_temperature_proxy_min_c": (
            min(proxy_temperatures) if proxy_temperatures else None
        ),
        "fast_apparent_temperature_proxy_median_c": (
            statistics.median(proxy_temperatures)
            if proxy_temperatures
            else None
        ),
        "fast_apparent_temperature_proxy_max_c": (
            max(proxy_temperatures) if proxy_temperatures else None
        ),
        "fast_apparent_temperature_proxy_start_c": (
            proxy_temperatures[0] if proxy_temperatures else None
        ),
        "fast_apparent_temperature_proxy_end_c": (
            proxy_temperatures[-1] if proxy_temperatures else None
        ),
        "fast_apparent_temperature_proxy_change_c": proxy_change,
        "large_full_frame_radiometric_drift_review": (
            abs(proxy_change) > 5.0 if proxy_change is not None else None
        ),
        "header_status_counts": dict(
            Counter(row["header_status"] for row in health)
        ),
        "formula_source": FORMULA_SOURCE,
        "specification_source": SPECIFICATION_SOURCE,
    }
    return summary, gaps


def _font(size: int, bold: bool = False):
    candidates = [
        (
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
            if bold
            else "/System/Library/Fonts/Supplemental/Arial.ttf"
        ),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default()


def line_plot(
    path: Path,
    title: str,
    y_label: str,
    series: list[tuple[str, list[float], list[float], str]],
    note: str,
) -> None:
    width, height = 1600, 900
    left, right, top, bottom = 150, 60, 110, 130
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((left, 28), title, fill="#111827", font=_font(38, True))
    all_x = [value for _, xs, _, _ in series for value in xs]
    all_y = [value for _, _, ys, _ in series for value in ys if math.isfinite(value)]
    if not all_x or not all_y:
        draw.text((left, top + 80), "No valid data", fill="#991B1B", font=_font(28))
        image.save(path)
        return
    x_min, x_max = min(all_x), max(all_x)
    y_min, y_max = min(all_y), max(all_y)
    if x_min == x_max:
        x_max += 1
    if y_min == y_max:
        y_min -= 1
        y_max += 1
    y_pad = 0.05 * (y_max - y_min)
    y_min -= y_pad
    y_max += y_pad
    x0, y0, x1, y1 = left, top, width - right, height - bottom
    for index in range(6):
        fraction = index / 5
        y = y1 - fraction * (y1 - y0)
        value = y_min + fraction * (y_max - y_min)
        draw.line((x0, y, x1, y), fill="#E5E7EB", width=1)
        draw.text((12, y - 11), f"{value:.4g}", fill="#475569", font=_font(18))
    draw.line((x0, y0, x0, y1), fill="#111827", width=2)
    draw.line((x0, y1, x1, y1), fill="#111827", width=2)

    def px(value: float) -> float:
        return x0 + (value - x_min) / (x_max - x_min) * (x1 - x0)

    def py(value: float) -> float:
        return y1 - (value - y_min) / (y_max - y_min) * (y1 - y0)

    rendered_series = []
    for name, xs, ys, color in series:
        points = [
            (px(x), py(y))
            for x, y in zip(xs, ys)
            if math.isfinite(x) and math.isfinite(y)
        ]
        if len(points) > 1:
            draw.line(points, fill=color, width=2)
        for point in points:
            draw.ellipse(
                (point[0] - 2, point[1] - 2, point[0] + 2, point[1] + 2),
                fill=color,
            )
        rendered_series.append((name, color))
    legend_x = x1 - 300
    legend_height = 12 + 30 * len(rendered_series)
    draw.rounded_rectangle(
        (legend_x - 12, top + 2, x1 - 8, top + legend_height),
        radius=8,
        fill="white",
        outline="#CBD5E1",
    )
    for series_index, (name, color) in enumerate(rendered_series):
        legend_y = top + 10 + 30 * series_index
        draw.rectangle(
            (legend_x, legend_y, legend_x + 20, legend_y + 20), fill=color
        )
        draw.text(
            (legend_x + 30, legend_y - 2),
            name,
            fill="#111827",
            font=_font(19),
        )
    start_text = dt.datetime.fromtimestamp(
        x_min, tz=dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")
    end_text = dt.datetime.fromtimestamp(
        x_max, tz=dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")
    draw.text((x0, y1 + 18), start_text, fill="#475569", font=_font(17))
    end_width = draw.textbbox((0, 0), end_text, font=_font(17))[2]
    draw.text((x1 - end_width, y1 + 18), end_text, fill="#475569", font=_font(17))
    draw.text(
        (x0 + (x1 - x0) / 2 - 75, height - 70),
        "Timestamp UTC",
        fill="#111827",
        font=_font(22, True),
    )
    draw.text((12, 72), y_label, fill="#111827", font=_font(20, True))
    draw.text((left, height - 35), note, fill="#475569", font=_font(17))
    image.save(path)


def create_plots(
    output: Path,
    health: list[dict[str, Any]],
    temperatures: list[dict[str, Any]],
) -> None:
    interval_rows = [
        row
        for row in health
        if isinstance(row["interval_from_previous_s"], (int, float))
    ]
    line_plot(
        output / "acquisition_interval_over_time.png",
        "FLIR acquisition interval over time",
        "Frame interval (s)",
        [
            (
                "Interval",
                [row["_datetime"].timestamp() for row in interval_rows],
                [float(row["interval_from_previous_s"]) for row in interval_rows],
                "#2563EB",
            )
        ],
        "Large isolated intervals indicate acquisition or recording gaps.",
    )
    raw_rows = [
        row
        for row in health
        if isinstance(row["raw_mean_dn_header"], (int, float))
        and not row["raw_all_zero"]
    ]
    line_plot(
        output / "raw_signal_health_over_time.png",
        "FLIR raw signal health over time",
        "Raw DN",
        [
            (
                "Minimum",
                [row["_datetime"].timestamp() for row in raw_rows],
                [float(row["raw_min_dn_header"]) for row in raw_rows],
                "#2563EB",
            ),
            (
                "Mean",
                [row["_datetime"].timestamp() for row in raw_rows],
                [float(row["raw_mean_dn_header"]) for row in raw_rows],
                "#16A34A",
            ),
            (
                "Maximum",
                [row["_datetime"].timestamp() for row in raw_rows],
                [float(row["raw_max_dn_header"]) for row in raw_rows],
                "#DC2626",
            ),
        ],
        "All-zero startup frames are flagged in CSV and excluded from this scale.",
    )
    proxy_rows = [
        row
        for row in health
        if isinstance(
            row["fast_apparent_temperature_from_mean_dn_c"], (int, float)
        )
    ]
    line_plot(
        output / "fast_apparent_temperature_proxy_over_time.png",
        "Fast apparent-temperature proxy over full acquisition",
        "Temperature proxy (degC)",
        [
            (
                "T(mean DN)",
                [row["_datetime"].timestamp() for row in proxy_rows],
                [
                    float(row["fast_apparent_temperature_from_mean_dn_c"])
                    for row in proxy_rows
                ],
                "#7C3AED",
            )
        ],
        (
            "Fast health proxy T(mean DN), not the exact mean of per-pixel "
            "temperature and not environment-corrected."
        ),
    )
    valid = [
        row
        for row in temperatures
        if row.get("temperature_status") == "PASS"
    ]
    if valid:
        xs = [
            parse_timestamp(row["timestamp_utc"]).timestamp()
            for row in valid
            if parse_timestamp(row["timestamp_utc"]) is not None
        ]
        method = valid[0].get("temperature_method", "")
        if method == "apparent_blackbody_temperature":
            temperature_note = (
                "Apparent blackbody temperature; not corrected for emissivity, "
                "atmosphere, reflection, distance, or external optics."
            )
        elif (
            valid[0].get("environment_inputs_status")
            == "MEASURED_TRACEABLE"
        ):
            temperature_note = (
                "Environment-corrected using operator-declared traceable "
                "measurements; validate against a reference target."
            )
        else:
            temperature_note = (
                "Environment-corrected with assumed test values; "
                "not quantitative."
            )
        line_plot(
            output / "temperature_over_time.png",
            "FLIR temperature statistics over time",
            "Temperature (degC)",
            [
                (
                    "Minimum",
                    xs,
                    [float(row["temperature_c_min"]) for row in valid],
                    "#2563EB",
                ),
                (
                    "Mean",
                    xs,
                    [float(row["temperature_c_mean"]) for row in valid],
                    "#16A34A",
                ),
                (
                    "Maximum",
                    xs,
                    [float(row["temperature_c_max"]) for row in valid],
                    "#DC2626",
                ),
            ],
            temperature_note,
        )


def markdown_report(
    summary: dict[str, Any],
    processing: dict[str, Any],
) -> str:
    health = "PASS"
    reasons = []
    if summary["gap_count"]:
        health = "REVIEW"
        reasons.append("timestamp gaps")
    if summary["all_zero_frame_count"]:
        health = "REVIEW"
        reasons.append("all-zero frames")
    if summary["missing_calibration_frame_count"]:
        health = "FAIL"
        reasons.append("missing calibration")
    if not summary["calibration_stable"]:
        health = "FAIL"
        reasons.append("calibration constants changed")
    if summary.get("large_full_frame_radiometric_drift_review"):
        if health == "PASS":
            health = "REVIEW"
        reasons.append("large full-frame radiometric drift")
    proxy_values = (
        summary.get("fast_apparent_temperature_proxy_min_c"),
        summary.get("fast_apparent_temperature_proxy_median_c"),
        summary.get("fast_apparent_temperature_proxy_max_c"),
    )
    proxy_text = (
        " / ".join(f"{float(value):.3f}" for value in proxy_values) + " degC"
        if all(value is not None for value in proxy_values)
        else "unavailable"
    )
    lines = [
        "# FLIR scientific health and temperature report",
        "",
        f"Overall acquisition health: **{health}**",
        "",
        f"- Frames indexed: {summary['frame_count']}",
        f"- UTC interval: {summary['start_time_utc']} to {summary['end_time_utc']}",
        f"- Observed rate: {summary['observed_mean_rate_hz']:.6g} Hz",
        f"- Median interval: {summary['median_interval_s']:.6g} s",
        f"- Timestamp gaps: {summary['gap_count']}",
        f"- Estimated missing frames: {summary['estimated_missing_frames']}",
        f"- All-zero frames: {summary['all_zero_frame_count']}",
        f"- Missing calibration frames: {summary['missing_calibration_frame_count']}",
        f"- Calibration signatures: {summary['calibration_signature_count']}",
        f"- Fast apparent proxy T(mean DN), min/median/max: {proxy_text}",
        (
            "- Fast apparent proxy start-to-end change: "
            f"{summary['fast_apparent_temperature_proxy_change_c']:.3f} degC"
            if summary.get("fast_apparent_temperature_proxy_change_c")
            is not None
            else "- Fast apparent proxy start-to-end change: unavailable"
        ),
        f"- Temperature frames processed: {processing['processed_frame_count']}",
        f"- Temperature mode: {processing['temperature_mode']}",
        f"- Processing elapsed: {processing['temperature_processing_seconds']:.3f} s",
        "",
        "## Scientific interpretation",
        "",
    ]
    if reasons:
        lines.append("Review flags: " + ", ".join(reasons) + ".")
    else:
        lines.append("No acquisition-health flags were detected.")
    lines.extend(
        [
            "",
            "The camera model is inferred from the project data sheet, not embedded "
            "in the JSON export. The A70 specification gives 640x480 pixels, a "
            "30 Hz maximum image frequency, and model-dependent measurement ranges. "
            "The observed approximately 0.49 Hz is therefore the configured recording "
            "cadence, not a sensor limitation.",
            "",
            "Apparent temperature uses factory radiometric calibration but assumes a "
            "blackbody and no path loss. Environment-corrected temperature is "
            "quantitative only when emissivity, reflected apparent temperature, "
            "distance, air temperature, humidity, and any external-optics values were "
            "measured for the acquisition.",
            "",
            "Camera accuracy cannot be verified from scene images alone. It requires a "
            "traceable blackbody/reference target within the selected measurement "
            "range and the manufacturer's stated ambient conditions.",
            "",
            "A large full-frame proxy drift can result from scene change, camera "
            "thermal stabilization, or correction events. It is a review flag, not "
            "proof of camera failure. Choose the scientific analysis interval only "
            "after the signal is stable and verify it against reference-target data.",
            "",
            "## Sources",
            "",
            f"- FLIR temperature equation: {FORMULA_SOURCE}",
            f"- Camera specification: {SPECIFICATION_SOURCE}",
            "",
        ]
    )
    return "\n".join(lines)


def require_corrected_inputs(args: argparse.Namespace) -> CorrectionInputs | None:
    if args.mode == "apparent":
        return None
    if args.environment_inputs_provenance is None:
        raise ValueError(
            "corrected mode requires --environment-inputs-provenance "
            "(measured or assumed_for_testing)"
        )
    required = {
        "emissivity": args.emissivity,
        "distance_m": args.distance_m,
        "atmospheric_temp_c": args.atmospheric_temp_c,
        "reflected_temp_c": args.reflected_temp_c,
        "relative_humidity_percent": args.relative_humidity_percent,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(
            "corrected mode requires measured values: " + ", ".join(missing)
        )
    return CorrectionInputs(
        emissivity=args.emissivity,
        object_distance_m=args.distance_m,
        atmospheric_temperature_c=args.atmospheric_temp_c,
        reflected_apparent_temperature_c=args.reflected_temp_c,
        relative_humidity_percent=args.relative_humidity_percent,
        external_optics_transmission=args.external_optics_transmission,
        external_optics_temperature_c=args.external_optics_temp_c,
    )


def add_correction_columns(
    rows: list[dict[str, Any]],
    correction: CorrectionInputs | None,
    provenance: str | None,
) -> None:
    for row in rows:
        row["object_emissivity"] = correction.emissivity if correction else 1.0
        row["object_distance_m"] = (
            correction.object_distance_m if correction else 0.0
        )
        row["atmospheric_temperature_c"] = (
            correction.atmospheric_temperature_c if correction else ""
        )
        row["reflected_apparent_temperature_c"] = (
            correction.reflected_apparent_temperature_c if correction else ""
        )
        row["relative_humidity_percent"] = (
            correction.relative_humidity_percent if correction else ""
        )
        row["external_optics_transmission"] = (
            correction.external_optics_transmission if correction else 1.0
        )
        row["external_optics_temperature_c"] = (
            correction.external_optics_temperature_c
            if correction
            and correction.external_optics_temperature_c is not None
            else ""
        )
        row["environment_inputs_status"] = (
            (
                "MEASURED_TRACEABLE"
                if provenance == "measured"
                else "ASSUMED_FOR_TESTING_NOT_QUANTITATIVE"
            )
            if correction
            else "APPARENT_ONLY_NOT_ENVIRONMENT_CORRECTED"
        )


def find_inputs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    files = sorted(path.rglob("*.json"))
    if not files:
        raise FileNotFoundError(f"No JSON files under {path}")
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--scan-workers", type=int, default=8)
    parser.add_argument("--process-workers", type=int, default=4)
    parser.add_argument("--scan-chunk-mb", type=int, default=256)
    parser.add_argument(
        "--index-cache",
        type=Path,
        help=(
            "Reuse a timestamp_index.csv from a previous verified run. "
            "File size and nanosecond modification time are checked."
        ),
    )
    parser.add_argument("--health-only", action="store_true")
    parser.add_argument("--start-time")
    parser.add_argument("--end-time")
    parser.add_argument("--auto-window-minutes", type=float)
    parser.add_argument("--every-nth-frame", type=int, default=1)
    parser.add_argument("--include-zero-frames", action="store_true")
    parser.add_argument("--expected-rate-hz", type=float)
    parser.add_argument("--gap-seconds", type=float)
    parser.add_argument("--mode", choices=("apparent", "corrected"), default="apparent")
    parser.add_argument("--emissivity", type=float)
    parser.add_argument("--distance-m", type=float)
    parser.add_argument("--atmospheric-temp-c", type=float)
    parser.add_argument("--reflected-temp-c", type=float)
    parser.add_argument("--relative-humidity-percent", type=float)
    parser.add_argument(
        "--environment-inputs-provenance",
        choices=("measured", "assumed_for_testing"),
        help=(
            "Required in corrected mode; records whether environmental inputs "
            "are traceable measurements or test assumptions."
        ),
    )
    parser.add_argument("--external-optics-transmission", type=float, default=1.0)
    parser.add_argument("--external-optics-temp-c", type=float)
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    parser.add_argument("--valid-temperature-min-c", type=float)
    parser.add_argument("--valid-temperature-max-c", type=float)
    parser.add_argument("--save-temperature-npz", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = find_inputs(input_path)
    correction = require_corrected_inputs(args)
    if (args.valid_temperature_min_c is None) != (
        args.valid_temperature_max_c is None
    ):
        raise ValueError("provide both valid temperature range limits or neither")
    valid_range = (
        (args.valid_temperature_min_c, args.valid_temperature_max_c)
        if args.valid_temperature_min_c is not None
        else None
    )

    total_started = time.perf_counter()
    index_reused = False
    scan_started = time.perf_counter()
    if args.index_cache is not None:
        cache_path = args.index_cache.expanduser().resolve()
        try:
            entries = load_timestamp_index(cache_path)
            indexed_paths = {entry[2] for entry in entries}
            if indexed_paths != set(paths):
                raise ValueError(
                    "index source-file set does not match selected input"
                )
            index_reused = True
            print(f"reused verified timestamp index: {cache_path}")
        except (OSError, ValueError, KeyError) as error:
            print(f"timestamp index rejected; rescanning source: {error}")
            entries, _ = scan_timestamps(
                paths, args.scan_workers, args.scan_chunk_mb
            )
    else:
        entries, _ = scan_timestamps(
            paths, args.scan_workers, args.scan_chunk_mb
        )
    scan_seconds = time.perf_counter() - scan_started
    if not entries:
        raise RuntimeError("No valid frame timestamps found")
    write_timestamp_index(output / "timestamp_index.csv", entries)
    health, header_seconds = inspect_all_headers(entries, args.scan_workers)
    summary, gaps = acquisition_summary(
        health, args.expected_rate_hz, args.gap_seconds
    )
    write_csv(output / "frame_health.csv", health)
    write_csv(output / "acquisition_gaps.csv", gaps)

    temperature_rows: list[dict[str, Any]] = []
    processing_started = time.perf_counter()
    if not args.health_only:
        start = parse_timestamp(args.start_time)
        end = parse_timestamp(args.end_time)
        if args.auto_window_minutes is not None:
            valid_health = [
                row for row in health if not row["raw_all_zero"]
            ]
            first = valid_health[0]["_datetime"] if valid_health else entries[0][0]
            start = first
            end = first + dt.timedelta(minutes=args.auto_window_minutes)
        indices = select_indices(
            entries,
            health,
            start,
            end,
            args.every_nth_frame,
            args.include_zero_frames,
        )
        spans = object_spans(entries)
        selected = [
            (index + 1, health[index], entries[index], spans[index])
            for index in indices
        ]
        save_directory = (
            output / "temperature_maps_npz"
            if args.save_temperature_npz
            else None
        )

        def worker(item):
            return process_one_temperature(
                item,
                correction,
                tuple(args.roi) if args.roi else None,
                save_directory,
                valid_range,
            )

        with ThreadPoolExecutor(
            max_workers=max(1, min(args.process_workers, len(selected) or 1))
        ) as executor:
            for index, result in enumerate(executor.map(worker, selected), 1):
                temperature_rows.append(result)
                if index == 1 or index % 25 == 0 or index == len(selected):
                    print(
                        f"\rtemperature {index}/{len(selected)}",
                        end="",
                        flush=True,
                    )
        if selected:
            print()
        add_correction_columns(
            temperature_rows,
            correction,
            args.environment_inputs_provenance,
        )
        write_csv(output / "temperature_frames.csv", temperature_rows)
    temperature_seconds = time.perf_counter() - processing_started
    create_plots(output, health, temperature_rows)

    processing = {
        "processed_frame_count": len(temperature_rows),
        "temperature_mode": (
            "health_only" if args.health_only else args.mode
        ),
        "environment_inputs_provenance": (
            args.environment_inputs_provenance or ""
        ),
        "temperature_status_counts": dict(
            Counter(row.get("temperature_status", "") for row in temperature_rows)
        ),
        "time_filter_start_utc": (
            temperature_rows[0]["timestamp_utc"] if temperature_rows else ""
        ),
        "time_filter_end_utc": (
            temperature_rows[-1]["timestamp_utc"] if temperature_rows else ""
        ),
        "every_nth_frame": args.every_nth_frame,
        "scan_seconds": scan_seconds,
        "timestamp_index_reused": index_reused,
        "header_health_seconds": header_seconds,
        "temperature_processing_seconds": temperature_seconds,
        "total_elapsed_seconds": time.perf_counter() - total_started,
    }
    combined = {**summary, **processing}
    (output / "summary.json").write_text(
        json.dumps(combined, indent=2, default=str), encoding="utf-8"
    )
    (output / "SCIENTIFIC_QC_REPORT.md").write_text(
        markdown_report(summary, processing), encoding="utf-8"
    )
    print(json.dumps(combined, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
