
"""Stream huge FLIR Zeppelin JSON into one compact metadata/temperature CSV.

- Never load the full JSON file.
- Never write raw image arrays to CSV.
- User selects a minute-level time window, e.g. 2026-07-10 10:48.
- CSV keeps only FLIR radiometric constants, image statistics, correction placeholders,
  and Zeppelin/noseboom fields needed later for GPS/atmospheric correction.
"""
from __future__ import annotations

import argparse, bisect, csv, json, math, random, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_INPUT = Path(r"D:\Flight_2123\FLIR260710\camera.FLIR_Zeppelin.json")
DEFAULT_OUTPUT = Path("FLIR_Zeppelin_selected_metadata.csv")
DEFAULT_RAM_BUDGET_GB = 25.0
DEFAULT_SCAN_WORKERS = 14
DEFAULT_CALC_WORKERS = 14
DEFAULT_SCAN_CHUNK_MB = 512
DEFAULT_BATCH_SIZE = 100

TIMESTAMP_BYTES_RE = re.compile(rb'"timestamp"\s*:\s*(?:\{\s*"\$date"\s*:\s*)?"([^"\\]*(?:\\.[^"\\]*)*)"')
TIMESTAMP_TEXT_RE = re.compile(r'"timestamp"\s*:\s*(?:\{\s*"\$date"\s*:\s*)?"([^"\\]*(?:\\.[^"\\]*)*)"')
OBJECT_DELIMITER_BYTES_RE = re.compile(rb"}\s*,\s*{")

POSTPROCESSING_PLACEHOLDER_COLUMNS = [
    "post.object_emissivity",
    "post.object_distance_m",
    "post.target_altitude_m",
    "post.camera_altitude_m",
    "post.reflected_apparent_temperature_degC",
    "post.atmospheric_temperature_degC",
    "post.relative_humidity_percent",
    "post.atmospheric_transmission",
    "post.external_optics_temperature_degC",
    "post.external_optics_transmission",
    "post.correction_status",
    "post.correction_notes",
]

NOSEBOOM_VARIABLE_COLUMNS = [
    # Time synchronization between FLIR frame time and Zeppelin data stream.
    "TIMESTAMP",
    "Airflow_UTCcorr_Nanoseconds_ns",
    "GNSSRecv1_ExtTimestamp_t_ns",
    "INS_Filter_ExtTimestamp_t_ns",

    # Atmospheric correction inputs for thermal/radiometric post-processing.
    "Airflow_Flow_OAT_degC",
    "Airflow_Sensor_OAT_Pt1000_degC",
    "Airflow_Sensor_OAT2_Pt100_degC",
    "Airflow_Flow_rel_humidity_",

    # GPS position and altitude for later temperature map / flight-track merge.
    "GNSSRecv1_LLHPos_Latitude_deg",
    "GNSSRecv1_LLHPos_Longitude_deg",
    "GNSSRecv1_LLHPos_MSLHeight_m",
    "GNSSRecv1_LLHPos_ElipsoidHeight_m",

    # Backup GPS receiver, useful if receiver 1 has gaps.
    "GNSSRecv2_LLHPos_Latitude_deg",
    "GNSSRecv2_LLHPos_Longitude_deg",
    "GNSSRecv2_LLHPos_MSLHeight_m",
    "GNSSRecv2_LLHPos_ElipsoidHeight_m",

    # Aircraft attitude/orientation, needed later if each pixel is projected to ground.
    "INS_Filter_EulerAngles_Roll_rad",
    "INS_Filter_EulerAngles_Pitch_rad",
    "INS_Filter_EulerAngles_Yaw_rad",
    "GNSSRecv1_vNED_Heading_deg",
]

BASE_OUTPUT_COLUMNS = [
    "_id.$oid", "timestamp.$date", "source_file", "source_file_index",
    "record_index_in_selected_scan", "raw_shape", "raw_storage_type",
    "raw_stats.min", "raw_stats.max", "raw_stats.mean",
    "raw_dn.valid_pixel_count", "raw_dn.min", "raw_dn.max", "raw_dn.mean",
    "raw_dn.median", "raw_dn.std", "calculated_temperature.status",
    "calculated_temperature.method", "calculated_temperature.unit",
    "calculated_temperature.equation", "pixel_temperature.valid_pixel_count",
    "pixel_temperature.min_c", "pixel_temperature.max_c", "pixel_temperature.mean_c",
    "pixel_temperature.median_c", "pixel_temperature.std_c", "calibration.R",
    "calibration.B", "calibration.F", "calibration.J0", "calibration.J1",
    "calibration.X", "calibration.alpha1", "calibration.alpha2",
    "calibration.beta1", "calibration.beta2",
]
OUTPUT_COLUMNS = BASE_OUTPUT_COLUMNS + POSTPROCESSING_PLACEHOLDER_COLUMNS + NOSEBOOM_VARIABLE_COLUMNS


def format_duration(seconds: float) -> str:
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {sec}s"


def sortable_timestamp(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("T", " ").replace("Z", "")


def parse_timestamp(value: str) -> datetime | None:
    value = sortable_timestamp(value)
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def flatten_metadata(value: Any, prefix: str = "") -> dict[str, Any]:
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


def add_blank_future_columns(row: dict[str, Any]) -> None:
    for name in POSTPROCESSING_PLACEHOLDER_COLUMNS + NOSEBOOM_VARIABLE_COLUMNS:
        row.setdefault(name, "")


def scan_one_byte_range(task: tuple[Path, int, int, int]) -> tuple[str, str, int, int, list[tuple[str, Path, int]]]:
    path, start_byte, end_byte, overlap = task
    read_start = max(0, start_byte - overlap)
    read_end = min(path.stat().st_size, end_byte + overlap)
    with path.open("rb") as source:
        source.seek(read_start)
        data = source.read(read_end - read_start)
    minimum = maximum = ""
    count = 0
    entries: list[tuple[str, Path, int]] = []
    for match in TIMESTAMP_BYTES_RE.finditer(data):
        absolute = read_start + match.start()
        if absolute < start_byte or absolute >= end_byte:
            continue
        try:
            timestamp = sortable_timestamp(match.group(1).decode("unicode_escape"))
        except UnicodeDecodeError:
            continue
        count += 1
        entries.append((timestamp, path, absolute))
        if not minimum or timestamp < minimum:
            minimum = timestamp
        if not maximum or timestamp > maximum:
            maximum = timestamp
    return minimum, maximum, count, end_byte - start_byte, entries


def scan_timestamps(json_files: list[Path], scan_workers: int, scan_chunk_mb: int) -> tuple[str, str, int, list[str], list[tuple[str, Path, int]]]:
    tasks: list[tuple[Path, int, int, int]] = []
    chunk = max(scan_chunk_mb, 1) * 1024 * 1024
    overlap = 4096
    total_bytes = 0
    for path in json_files:
        size = path.stat().st_size
        total_bytes += size
        for start in range(0, size, chunk):
            tasks.append((path, start, min(start + chunk, size), overlap))
    minimum = maximum = ""
    count = scanned = 0
    started = time.monotonic()
    timestamp_index: list[tuple[str, Path, int]] = []

    def merge(result: tuple[str, str, int, int, list[tuple[str, Path, int]]]) -> None:
        nonlocal minimum, maximum, count, scanned, timestamp_index
        mn, mx, n, b, entries = result
        scanned += b
        count += n
        timestamp_index.extend(entries)
        if mn and (not minimum or mn < minimum):
            minimum = mn
        if mx and (not maximum or mx > maximum):
            maximum = mx

    def print_progress(done: int) -> None:
        elapsed = max(time.monotonic() - started, 1e-9)
        speed = scanned / (1024 * 1024) / elapsed
        print(
            f"\rTimestamp scan: task {done}/{len(tasks)}, "
            f"{scanned / 1024**3:.2f}/{total_bytes / 1024**3:.2f} GiB, "
            f"{count:,} timestamps, {speed:.1f} MB/s, workers={scan_workers}",
            end="", flush=True,
        )

    workers = max(scan_workers, 1)
    if workers == 1 or len(tasks) <= 1:
        for i, task in enumerate(tasks, start=1):
            merge(scan_one_byte_range(task)); print_progress(i)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for i, result in enumerate(executor.map(scan_one_byte_range, tasks), start=1):
                merge(result); print_progress(i)
    print()
    timestamp_index.sort(key=lambda item: (item[0], str(item[1]), item[2]))
    return minimum, maximum, count, sorted(set(t for t, _, _ in timestamp_index)), timestamp_index


def ask_for_time(label: str, default_value: str) -> str:
    try:
        text = input(f"{label} date-time, format YYYY-MM-DD HH:MM [Enter for {default_value[:16]}]: ").strip()
    except EOFError as error:
        raise RuntimeError("Interactive input unavailable. Use --start-time and --end-time.") from error
    return sortable_timestamp(text) if text else default_value


def nearest_available_time(requested: str, available_timestamps: list[str], label: str) -> str:
    requested_dt = parse_timestamp(requested)
    if requested_dt is None or not available_timestamps:
        return sortable_timestamp(requested)
    normalized = format_timestamp(requested_dt)
    pos = bisect.bisect_left(available_timestamps, normalized)
    candidates = []
    if pos > 0:
        candidates.append(available_timestamps[pos - 1])
    if pos < len(available_timestamps):
        candidates.append(available_timestamps[pos])
    if not candidates:
        return normalized
    nearest = min(candidates, key=lambda ts: abs((parse_timestamp(ts) - requested_dt).total_seconds()) if parse_timestamp(ts) else float("inf"))
    if nearest != normalized:
        print(f"{label} requested {requested} -> nearest available frame {nearest}")
    return nearest


def choose_auto_window(minimum_time: str, maximum_time: str, minutes: float) -> tuple[str, str]:
    start_dt = parse_timestamp(minimum_time); end_dt = parse_timestamp(maximum_time)
    if start_dt is None or end_dt is None:
        return minimum_time, maximum_time
    duration = timedelta(minutes=minutes)
    latest_start = end_dt - duration
    if latest_start <= start_dt:
        return format_timestamp(start_dt), format_timestamp(end_dt)
    start = start_dt + timedelta(seconds=random.uniform(0, (latest_start - start_dt).total_seconds()))
    return format_timestamp(start), format_timestamp(start + duration)


def choose_time_window(minimum_time: str, maximum_time: str, available_timestamps: list[str], start_time: str | None, end_time: str | None, auto_window_minutes: float | None, no_time_prompt: bool) -> tuple[str, str]:
    print("\nAvailable date-time range:")
    print(f"Minimum date-time: {minimum_time}")
    print(f"Maximum date-time: {maximum_time}")
    print("Use this input style: 2026-07-10 10:48")
    if auto_window_minutes is not None:
        start, end = choose_auto_window(minimum_time, maximum_time, auto_window_minutes)
        print(f"\nAuto-selected random {auto_window_minutes:g}-minute window for testing.")
    elif start_time or end_time:
        start = sortable_timestamp(start_time) if start_time else minimum_time
        end = sortable_timestamp(end_time) if end_time else maximum_time
    elif no_time_prompt:
        start, end = minimum_time, maximum_time
        print("\n--no-time-prompt used without start/end; full range selected.")
    else:
        print("\nSelect the date-time window to calculate/export.")
        start = ask_for_time("Start", minimum_time)
        end = ask_for_time("End", maximum_time)
    start = nearest_available_time(start, available_timestamps, "Start time")
    end = nearest_available_time(end, available_timestamps, "End time")
    if start > end:
        print("Start was after end; swapping them."); start, end = end, start
    print("\nSelected calculation window:")
    print(f"Start: {start}")
    print(f"End:   {end}\n")
    return start, end


def selected_entries(timestamp_index: list[tuple[str, Path, int]], start_time: str, end_time: str) -> list[tuple[str, Path, int]]:
    return [entry for entry in timestamp_index if start_time <= entry[0] <= end_time]


def find_object_start_before_timestamp(path: Path, timestamp_byte_offset: int, initial_backtrack: int = 256 * 1024) -> int:
    for backtrack in (initial_backtrack, 1024 * 1024, 4 * 1024 * 1024, 16 * 1024 * 1024):
        read_start = max(0, timestamp_byte_offset - backtrack)
        with path.open("rb") as source:
            source.seek(read_start)
            data = source.read(timestamp_byte_offset - read_start)
        last = None
        for match in OBJECT_DELIMITER_BYTES_RE.finditer(data):
            last = match
        if last is not None:
            return read_start + last.end() - 1
        if read_start == 0:
            first = data.find(b"{")
            return read_start + first if first >= 0 else read_start
    return max(0, timestamp_byte_offset - initial_backtrack)


def read_object_text_at_timestamp(path: Path, timestamp_byte_offset: int, chunk_size: int = 1024 * 1024) -> str:
    start_offset = find_object_start_before_timestamp(path, timestamp_byte_offset)
    depth = 0; in_string = False; escape = False; collecting = False; parts: list[str] = []
    with path.open("r", encoding="utf-8") as source:
        source.seek(start_offset)
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            for char in chunk:
                if not collecting:
                    if char == "{":
                        collecting = True; depth = 1; in_string = False; escape = False; parts = [char]
                    continue
                parts.append(char)
                if escape:
                    escape = False; continue
                if char == "\\":
                    escape = True; continue
                if char == '"':
                    in_string = not in_string; continue
                if in_string:
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        return "".join(parts)
    raise RuntimeError(f"Could not read complete JSON object near byte {timestamp_byte_offset:,} in {path}")


def find_json_value_span(object_text: str, field_name: str) -> tuple[int, int] | None:
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
        depth = 0; in_string = False; escape = False
        for pos in range(start, len(object_text)):
            char = object_text[pos]
            if escape:
                escape = False; continue
            if char == "\\":
                escape = True; continue
            if char == '"':
                in_string = not in_string; continue
            if in_string:
                continue
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return start, pos + 1
        return None
    end = start
    while end < len(object_text) and object_text[end] not in ",}":
        end += 1
    return start, end


def estimate_array_shape(array_text: str) -> list[int] | None:
    if not array_text.strip().startswith("["):
        return None
    rows = array_text.count("[") - 1
    first_row_start = array_text.find("[", 1)
    if first_row_start < 0:
        return [0, 0]
    first_row_end = array_text.find("]", first_row_start)
    if first_row_end < 0:
        return [rows, 0]
    first_row = array_text[first_row_start + 1:first_row_end]
    values = np.fromstring(first_row, sep=",", dtype=np.float64)
    return [max(rows, 0), int(values.size)]


def raw_array_from_text(raw_text: str) -> tuple[np.ndarray, list[int] | None]:
    shape = estimate_array_shape(raw_text)
    numeric_text = raw_text.replace("[", " ").replace("]", " ")
    values = np.fromstring(numeric_text, sep=",", dtype=np.float64)
    if shape and shape[0] * shape[1] == values.size:
        return values.reshape((shape[0], shape[1])), shape
    return values, shape


def object_text_without_large_arrays(object_text: str) -> tuple[str, dict[str, Any]]:
    replacements: list[tuple[int, int, str]] = []
    shape_metadata: dict[str, Any] = {}
    for field_name in ("raw", "temperature"):
        span = find_json_value_span(object_text, field_name)
        if span is None:
            shape_metadata[f"{field_name}_shape"] = None
            shape_metadata[f"{field_name}_storage_type"] = None
            continue
        value_text = object_text[span[0]:span[1]]
        shape_metadata[f"{field_name}_shape"] = estimate_array_shape(value_text)
        shape_metadata[f"{field_name}_storage_type"] = "JSON array"
        replacements.append((span[0], span[1], "null"))
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(replacements):
        parts.append(object_text[cursor:start]); parts.append(replacement); cursor = end
    parts.append(object_text[cursor:])
    return "".join(parts), shape_metadata


def default_temperature_result() -> dict[str, Any]:
    return {
        "calculated_temperature.status": "not_calculated",
        "calculated_temperature.method": "FLIR Planck apparent temperature from raw DN",
        "calculated_temperature.unit": "degC",
        "calculated_temperature.equation": "signal=(raw-J0)/J1; T_C=B/ln(R/(signal+F))-273.15",
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


def calculate_temperature_stats(raw_array: np.ndarray, calibration: dict | None) -> dict[str, Any]:
    result = default_temperature_result()
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
        result["pixel_temperature.valid_pixel_count"] = int(valid.size)
        if valid.size == 0:
            result["calculated_temperature.status"] = "no_valid_pixels"
            return result
        result.update({
            "calculated_temperature.status": "ok_apparent_no_atmospheric_correction",
            "pixel_temperature.min_c": float(np.min(valid)),
            "pixel_temperature.max_c": float(np.max(valid)),
            "pixel_temperature.mean_c": float(np.mean(valid)),
            "pixel_temperature.median_c": float(np.median(valid)),
            "pixel_temperature.std_c": float(np.std(valid)),
        })
    except (TypeError, ValueError, OverflowError) as error:
        result["calculated_temperature.status"] = f"error:{error}"
    return result


def build_one_row(item: tuple[str, int, Path, int, str]) -> tuple[str, int, dict[str, Any]]:
    timestamp, global_index, source_file, source_file_index, object_text = item
    raw_span = find_json_value_span(object_text, "raw")
    raw_array = np.array([], dtype=np.float64)
    raw_shape = None
    if raw_span is not None:
        raw_array, raw_shape = raw_array_from_text(object_text[raw_span[0]:raw_span[1]])
    clean_text, shape_metadata = object_text_without_large_arrays(object_text)
    metadata = json.loads(clean_text)
    metadata.pop("raw", None); metadata.pop("temperature", None)
    metadata.update(shape_metadata)
    if raw_shape is not None:
        metadata["raw_shape"] = raw_shape
    metadata["source_file"] = source_file.name
    metadata["source_file_index"] = source_file_index
    metadata["record_index_in_selected_scan"] = global_index
    stats = calculate_temperature_stats(raw_array, metadata.get("calibration"))
    row = flatten_metadata(metadata)
    row.update(stats)
    add_blank_future_columns(row)
    return timestamp, global_index, row


def process_batch(batch: list[tuple[str, int, Path, int, str]], workers: int, progress_callback: Any | None = None) -> list[tuple[str, int, dict[str, Any]]]:
    results: list[tuple[str, int, dict[str, Any]]] = []
    if workers <= 1 or len(batch) <= 1:
        for item in batch:
            result = build_one_row(item)
            results.append(result)
            if progress_callback:
                progress_callback(len(results), len(batch), result)
        return results
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(build_one_row, item) for item in batch]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if progress_callback:
                progress_callback(len(results), len(batch), result)
    results.sort(key=lambda item: (item[0], item[1]))
    return results


def calculate_selected_window(entries: list[tuple[str, Path, int]], json_files: list[Path], batch_size: int, workers: int, output_csv: Path) -> int:
    selected_total = len(entries)
    total_batches = max(math.ceil(selected_total / max(batch_size, 1)), 1)
    started = time.monotonic()
    completed_batches = 0
    processed_rows = 0
    batch: list[tuple[str, int, Path, int, str]] = []
    file_index_by_path = {path: i for i, path in enumerate(json_files, start=1)}
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8-sig") as target:
        writer = csv.DictWriter(target, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()

        def run_batch() -> None:
            nonlocal batch, completed_batches, processed_rows
            if not batch:
                return
            completed_batches += 1
            batch_number = completed_batches
            time_start = batch[0][0]; time_end = batch[-1][0]
            batch_started = time.monotonic()
            completed_before_batch = processed_rows
            print(f"\nBatch {batch_number}/{total_batches} started: {len(batch)} frame(s), time {time_start} -> {time_end}", flush=True)

            def show_frame_progress(done_in_batch: int, total_in_batch: int, _result: tuple[str, int, dict[str, Any]]) -> None:
                overall_done = completed_before_batch + done_in_batch
                batch_percent = 100.0 * done_in_batch / max(total_in_batch, 1)
                total_percent = 100.0 * overall_done / max(selected_total, 1)
                elapsed_now = time.monotonic() - started
                rate = overall_done / elapsed_now if elapsed_now > 0 else 0.0
                remaining_frames = max(selected_total - overall_done, 0)
                eta_seconds = remaining_frames / rate if rate > 0 else None
                eta_text = format_duration(eta_seconds) if eta_seconds is not None else "unknown"
                print(
                    f"\rBatch {batch_number}/{total_batches}: "
                    f"{done_in_batch}/{total_in_batch} frame(s) ({batch_percent:5.1f}%), "
                    f"total {overall_done}/{selected_total} ({total_percent:5.1f}%), "
                    f"rate {rate:5.2f} frame/s, elapsed {format_duration(elapsed_now)}, ETA {eta_text}",
                    end="", flush=True,
                )

            results = process_batch(batch, workers=workers, progress_callback=show_frame_progress)
            print()
            for _, _, row in results:
                writer.writerow(row)
            target.flush()
            processed_rows += len(results)
            batch.clear()
            batch_seconds = time.monotonic() - batch_started
            elapsed = time.monotonic() - started
            average_batch = elapsed / batch_number
            remaining_batches = max(total_batches - batch_number, 0)
            print(
                f"Batch {batch_number}/{total_batches} finished: {format_duration(batch_seconds)} batch, "
                f"processed {processed_rows}/{selected_total}, elapsed {format_duration(elapsed)}, "
                f"ETA {format_duration(average_batch * remaining_batches)}",
                flush=True,
            )

        for global_index, (timestamp, source_file, timestamp_offset) in enumerate(entries, start=1):
            batch_number = min(math.ceil(global_index / max(batch_size, 1)), total_batches)
            done_in_prepare = ((global_index - 1) % max(batch_size, 1)) + 1
            total_in_prepare = min(batch_size, selected_total - (batch_number - 1) * batch_size)
            print(
                f"\rPreparing batch {batch_number}/{total_batches}: read {done_in_prepare}/{total_in_prepare} selected object(s), "
                f"total prepared {global_index}/{selected_total}",
                end="", flush=True,
            )
            object_text = read_object_text_at_timestamp(source_file, timestamp_offset)
            batch.append((timestamp, global_index, source_file, file_index_by_path[source_file], object_text))
            if len(batch) >= batch_size:
                run_batch()
        if batch:
            run_batch()
    print(f"CSV written: {output_csv}")
    print(f"Rows written: {processed_rows:,}")
    print(f"Columns written: {len(OUTPUT_COLUMNS):,}")
    return processed_rows


def find_json_files(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        pattern = "**/*.json" if recursive else "*.json"
        return sorted(p for p in input_path.glob(pattern) if p.is_file())
    raise FileNotFoundError(f"Input path not found: {input_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream huge FLIR JSON into one compact metadata/temperature CSV.")
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--recursive-json", action="store_true")
    parser.add_argument("--start-time", type=str, default=None, help="Example: 2026-07-10 10:48")
    parser.add_argument("--end-time", type=str, default=None, help="Example: 2026-07-10 10:58")
    parser.add_argument("--auto-window-minutes", type=float, default=None)
    parser.add_argument("--no-time-prompt", action="store_true")
    parser.add_argument("--ram-gb", type=float, default=DEFAULT_RAM_BUDGET_GB)
    parser.add_argument("--scan-workers", type=int, default=DEFAULT_SCAN_WORKERS)
    parser.add_argument("--workers", type=int, default=DEFAULT_CALC_WORKERS)
    parser.add_argument("--scan-chunk-mb", type=int, default=DEFAULT_SCAN_CHUNK_MB)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    output_csv = args.output.resolve()
    json_files = find_json_files(input_path, recursive=args.recursive_json)
    total_size = sum(p.stat().st_size for p in json_files)
    print(f"Input: {input_path}")
    print(f"JSON files: {len(json_files)}")
    print(f"Total input size: {total_size / 1024**3:.2f} GiB")
    print(f"Output CSV: {output_csv}")
    print(f"RAM budget requested: {args.ram_gb:g} GB")
    print(f"CPU workers: scan={args.scan_workers}, calculation={args.workers}")
    print(f"Batch size: {args.batch_size} frame(s)")
    print(f"Scan chunk: {args.scan_chunk_mb} MB per task")

    print("\nStep 1/4: build timestamp byte index...")
    minimum_time, maximum_time, total_frames, available_timestamps, timestamp_index = scan_timestamps(
        json_files=json_files,
        scan_workers=max(args.scan_workers, 1),
        scan_chunk_mb=max(args.scan_chunk_mb, 1),
    )
    print(f"Timestamp scan finished: {total_frames:,} timestamps found.")
    if not timestamp_index:
        raise RuntimeError("No timestamps found. Cannot select a time window.")

    print("\nStep 2/4: choose time window...")
    start_time, end_time = choose_time_window(
        minimum_time=minimum_time,
        maximum_time=maximum_time,
        available_timestamps=available_timestamps,
        start_time=args.start_time,
        end_time=args.end_time,
        auto_window_minutes=args.auto_window_minutes,
        no_time_prompt=args.no_time_prompt,
    )
    entries = selected_entries(timestamp_index, start_time, end_time)
    selected_total = len(entries)
    print(f"Selected frames expected: {selected_total:,}")
    print(f"Total calculation batches: {max(math.ceil(selected_total / max(args.batch_size, 1)), 1):,}")
    if selected_total == 0:
        print("No selected frames found; only a header CSV will be created.")

    print("\nStep 3/4: read selected objects, calculate frames, and write CSV...")
    calculate_selected_window(
        entries=entries,
        json_files=json_files,
        batch_size=max(args.batch_size, 1),
        workers=max(args.workers, 1),
        output_csv=output_csv,
    )
    print("\nStep 4/4: complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

