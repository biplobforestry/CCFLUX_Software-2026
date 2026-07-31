"""
Fast FLIR flight health check.

Purpose
-------
Use this immediately after/during a Zeppelin flight to answer:

- Did the FLIR camera write frames?
- What was the acquisition rate?
- Are there timestamp gaps?
- Are sampled frames structurally valid?
- Are raw_stats and calibration metadata present?

This script does NOT calculate temperature and does NOT load the full JSON file.
It only scans timestamps by byte chunks and optionally inspects a small number of
sample frame documents.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path(r"D:\Flight_2123\FLIR260710\camera.FLIR_Zeppelin.json")
DEFAULT_REPORT = Path("FLIR_quick_look_report.txt")
DEFAULT_GAP_CSV = Path("FLIR_quick_look_gaps.csv")
DEFAULT_SAMPLE_CSV = Path("FLIR_quick_look_sample_frames.csv")

DEFAULT_SCAN_WORKERS = 14
DEFAULT_SCAN_CHUNK_MB = 512

TIMESTAMP_BYTES_RE = re.compile(
    rb'"timestamp"\s*:\s*(?:\{\s*"\$date"\s*:\s*)?"([^"\\]*(?:\\.[^"\\]*)*)"'
)
OBJECT_DELIMITER_BYTES_RE = re.compile(rb"}\s*,\s*{")


def sortable_timestamp(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().replace("T", " ").replace("Z", "")


def parse_timestamp(value: str) -> datetime | None:
    value = sortable_timestamp(value)
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_duration(seconds: float) -> str:
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {sec}s"


def find_json_files(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        pattern = "**/*.json" if recursive else "*.json"
        return sorted(p for p in input_path.glob(pattern) if p.is_file())
    raise FileNotFoundError(f"Input path not found: {input_path}")


def scan_one_byte_range(
    task: tuple[Path, int, int, int],
) -> tuple[int, int, list[tuple[str, Path, int]]]:
    file_path, start_byte, end_byte, overlap = task
    read_start = max(0, start_byte - overlap)
    read_end = min(file_path.stat().st_size, end_byte + overlap)

    with file_path.open("rb") as source:
        source.seek(read_start)
        data = source.read(read_end - read_start)

    entries: list[tuple[str, Path, int]] = []
    for match in TIMESTAMP_BYTES_RE.finditer(data):
        absolute = read_start + match.start()
        if absolute < start_byte or absolute >= end_byte:
            continue
        try:
            timestamp = sortable_timestamp(match.group(1).decode("unicode_escape"))
        except UnicodeDecodeError:
            continue
        entries.append((timestamp, file_path, absolute))

    return end_byte - start_byte, len(entries), entries


def scan_timestamps(
    json_files: list[Path],
    scan_workers: int,
    scan_chunk_mb: int,
) -> list[tuple[str, Path, int]]:
    tasks: list[tuple[Path, int, int, int]] = []
    chunk_size = max(scan_chunk_mb, 1) * 1024 * 1024
    overlap = 4096
    total_bytes = 0

    for file_path in json_files:
        size = file_path.stat().st_size
        total_bytes += size
        for start in range(0, size, chunk_size):
            tasks.append((file_path, start, min(start + chunk_size, size), overlap))

    scanned = 0
    found = 0
    entries: list[tuple[str, Path, int]] = []
    started = time.monotonic()

    def show_progress(done_tasks: int) -> None:
        elapsed = max(time.monotonic() - started, 1e-9)
        speed = scanned / 1024 / 1024 / elapsed
        print(
            f"\rTimestamp scan: task {done_tasks}/{len(tasks)}, "
            f"{scanned / 1024**3:.2f}/{total_bytes / 1024**3:.2f} GiB, "
            f"{found:,} frames, {speed:.1f} MB/s",
            end="",
            flush=True,
        )

    workers = max(scan_workers, 1)
    if workers == 1 or len(tasks) <= 1:
        for i, task in enumerate(tasks, start=1):
            bytes_done, count, part = scan_one_byte_range(task)
            scanned += bytes_done
            found += count
            entries.extend(part)
            show_progress(i)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for i, (bytes_done, count, part) in enumerate(
                executor.map(scan_one_byte_range, tasks),
                start=1,
            ):
                scanned += bytes_done
                found += count
                entries.extend(part)
                show_progress(i)

    print()
    entries.sort(key=lambda item: (item[0], str(item[1]), item[2]))
    return entries



def trim_edge_minutes(
    entries: list[tuple[str, Path, int]],
    edge_minutes: float,
) -> list[tuple[str, Path, int]]:
    """Remove first/last N minutes before rate/gap/raw-health analysis."""
    if edge_minutes <= 0 or len(entries) < 2:
        return entries
    parsed = [(entry, parse_timestamp(entry[0])) for entry in entries]
    valid = [(entry, dt) for entry, dt in parsed if dt is not None]
    if len(valid) < 2:
        return entries
    start = valid[0][1]
    end = valid[-1][1]
    margin = edge_minutes * 60.0
    kept = [entry for entry, dt in valid if (dt - start).total_seconds() >= margin and (end - dt).total_seconds() >= margin]
    return kept if kept else entries

def calculate_time_statistics(
    entries: list[tuple[str, Path, int]],
    gap_seconds: list[float],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    datetimes = [parse_timestamp(ts) for ts, _, _ in entries]
    valid_pairs = [(entry, dt) for entry, dt in zip(entries, datetimes) if dt is not None]

    if not valid_pairs:
        return {
            "status": "NO_VALID_TIMESTAMPS",
            "frame_count": len(entries),
        }, []

    valid_entries = [entry for entry, _ in valid_pairs]
    times = [dt for _, dt in valid_pairs]
    intervals = [
        (times[i] - times[i - 1]).total_seconds()
        for i in range(1, len(times))
        if (times[i] - times[i - 1]).total_seconds() >= 0
    ]

    start = times[0]
    end = times[-1]
    duration = max((end - start).total_seconds(), 0.0)
    frame_count = len(times)
    mean_rate = frame_count / duration if duration > 0 else None
    median_interval = statistics.median(intervals) if intervals else None
    mean_interval = statistics.mean(intervals) if intervals else None
    min_interval = min(intervals) if intervals else None
    max_interval = max(intervals) if intervals else None
    expected_rate = 1.0 / median_interval if median_interval and median_interval > 0 else None

    gap_rows: list[dict[str, Any]] = []
    primary_gap = min(gap_seconds) if gap_seconds else 2.0
    estimated_missing = 0
    for i in range(1, len(times)):
        dt = (times[i] - times[i - 1]).total_seconds()
        if dt > primary_gap:
            expected_missing_here = 0
            if median_interval and median_interval > 0:
                expected_missing_here = max(round(dt / median_interval) - 1, 0)
            estimated_missing += expected_missing_here
            gap_rows.append({
                "gap_start": valid_entries[i - 1][0],
                "gap_end": valid_entries[i][0],
                "gap_seconds": round(dt, 6),
                "previous_file": str(valid_entries[i - 1][1]),
                "next_file": str(valid_entries[i][1]),
                "estimated_missing_frames_from_median_interval": expected_missing_here,
            })

    gap_counts = {
        f"gaps_gt_{threshold:g}s": sum(1 for interval in intervals if interval > threshold)
        for threshold in gap_seconds
    }

    summary: dict[str, Any] = {
        "status": "OK_FRAMES_FOUND",
        "frame_count": frame_count,
        "start_time": valid_entries[0][0],
        "end_time": valid_entries[-1][0],
        "duration_seconds": duration,
        "duration_text": format_duration(duration),
        "mean_acquisition_rate_hz": mean_rate,
        "expected_rate_from_median_interval_hz": expected_rate,
        "mean_interval_seconds": mean_interval,
        "median_interval_seconds": median_interval,
        "min_interval_seconds": min_interval,
        "max_interval_seconds": max_interval,
        "longest_gap_seconds": max_interval,
        "gap_threshold_primary_seconds": primary_gap,
        "gap_count_primary": len(gap_rows),
        "estimated_missing_frames_from_median_interval": estimated_missing,
        **gap_counts,
    }
    return summary, gap_rows


def find_object_start_before_timestamp(
    path: Path,
    timestamp_byte_offset: int,
    initial_backtrack: int = 256 * 1024,
) -> int:
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


def read_object_text_at_timestamp(path: Path, timestamp_byte_offset: int) -> str:
    start_offset = find_object_start_before_timestamp(path, timestamp_byte_offset)
    depth = 0
    in_string = False
    escape = False
    collecting = False
    parts: list[str] = []

    with path.open("r", encoding="utf-8") as source:
        source.seek(start_offset)
        while True:
            chunk = source.read(1024 * 1024)
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
                        return "".join(parts)

    raise RuntimeError(f"Could not read complete object near byte {timestamp_byte_offset:,} in {path}")


def choose_sample_entries(
    entries: list[tuple[str, Path, int]],
    sample_frames: int,
) -> list[tuple[int, str, Path, int]]:
    if sample_frames <= 0 or not entries:
        return []
    count = min(sample_frames, len(entries))
    if count == 1:
        indices = [0]
    else:
        indices = sorted(set(round(i * (len(entries) - 1) / (count - 1)) for i in range(count)))
    return [(idx + 1, entries[idx][0], entries[idx][1], entries[idx][2]) for idx in indices]


def raw_shape_from_text(raw_text: str) -> str:
    if not raw_text.strip().startswith("["):
        return ""
    rows = raw_text.count("[") - 1
    first_row_start = raw_text.find("[", 1)
    first_row_end = raw_text.find("]", first_row_start)
    if first_row_start < 0 or first_row_end < 0:
        return f"{rows}x?"
    first_row = raw_text[first_row_start + 1:first_row_end].strip()
    cols = 0 if not first_row else first_row.count(",") + 1
    return f"{rows}x{cols}"


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
        depth = 0
        in_string = False
        escape = False
        for pos in range(start, len(object_text)):
            char = object_text[pos]
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
                    return start, pos + 1
        return None
    end = start
    while end < len(object_text) and object_text[end] not in ",}":
        end += 1
    return start, end


def object_without_raw(object_text: str) -> tuple[str, str]:
    raw_shape = ""
    raw_span = find_json_value_span(object_text, "raw")
    if raw_span is None:
        return object_text, raw_shape
    raw_text = object_text[raw_span[0]:raw_span[1]]
    raw_shape = raw_shape_from_text(raw_text)
    return object_text[:raw_span[0]] + "null" + object_text[raw_span[1]:], raw_shape


def inspect_sample_frames(
    entries: list[tuple[str, Path, int]],
    sample_frames: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    samples = choose_sample_entries(entries, sample_frames)
    if not samples:
        return rows

    print(f"\nInspecting {len(samples)} sample frame(s) without temperature calculation...")
    for sample_no, (record_index, timestamp, file_path, offset) in enumerate(samples, start=1):
        print(f"\rSample frame {sample_no}/{len(samples)}", end="", flush=True)
        row: dict[str, Any] = {
            "sample_no": sample_no,
            "record_index": record_index,
            "timestamp": timestamp,
            "source_file": file_path.name,
            "timestamp_byte_offset": offset,
            "read_status": "not_read",
            "raw_present": False,
            "raw_shape": "",
            "raw_stats_present": False,
            "raw_stats_min": "",
            "raw_stats_max": "",
            "raw_stats_mean": "",
            "raw_all_zero_by_stats": "",
            "calibration_present": False,
            "calibration_required_present": False,
            "missing_calibration_constants": "",
        }
        try:
            object_text = read_object_text_at_timestamp(file_path, offset)
            light_text, raw_shape = object_without_raw(object_text)
            doc = json.loads(light_text)
            calibration = doc.get("calibration") if isinstance(doc, dict) else None
            raw_stats = doc.get("raw_stats") if isinstance(doc, dict) else None

            row["read_status"] = "ok"
            row["raw_present"] = raw_shape != ""
            row["raw_shape"] = raw_shape
            if isinstance(raw_stats, dict):
                row["raw_stats_present"] = True
                row["raw_stats_min"] = raw_stats.get("min", "")
                row["raw_stats_max"] = raw_stats.get("max", "")
                row["raw_stats_mean"] = raw_stats.get("mean", "")
                row["raw_all_zero_by_stats"] = (
                    raw_stats.get("min") == 0
                    and raw_stats.get("max") == 0
                    and raw_stats.get("mean") == 0
                )
            if isinstance(calibration, dict):
                required = ["R", "B", "F", "J0", "J1", "X", "alpha1", "alpha2", "beta1", "beta2"]
                missing = [name for name in required if calibration.get(name) is None]
                row["calibration_present"] = True
                row["calibration_required_present"] = len(missing) == 0
                row["missing_calibration_constants"] = ",".join(missing)
        except Exception as error:  # noqa: BLE001 - QC should report and continue.
            row["read_status"] = f"error:{error}"
        rows.append(row)
    print()
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as target:
        writer = csv.DictWriter(target, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)




def make_overview_figure(
    entries: list[tuple[str, Path, int]],
    summary: dict[str, Any],
    gap_rows: list[dict[str, Any]],
    sample_rows: list[dict[str, Any]],
    figure_path: Path,
) -> None:
    """Create a compact 8-inch-wide visual health panel."""
    if not entries:
        print("No entries available; overview figure skipped.")
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except Exception as error:  # noqa: BLE001 - optional plotting dependency.
        print(f"Overview figure skipped because matplotlib is unavailable: {error}")
        return

    parsed = [(parse_timestamp(ts), ts) for ts, _, _ in entries]
    parsed = [(dt, ts) for dt, ts in parsed if dt is not None]
    if len(parsed) < 2:
        print("Not enough valid timestamps for overview figure; skipped.")
        return

    plt.rcParams.update({
        "font.size": 8,
        "axes.titlesize": 9,
        "axes.labelsize": 8,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "figure.titlesize": 10,
    })

    times = [dt for dt, _ in parsed]
    intervals = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
    interval_times = times[1:]
    cumulative = list(range(1, len(times) + 1))
    primary_gap = float(summary.get("gap_threshold_primary_seconds", 2.5) or 2.5)

    fig, axes = plt.subplots(2, 2, figsize=(8, 5.8), constrained_layout=True)
    fig.suptitle("FLIR Quick Look - Acquisition Health", fontweight="bold")

    ax = axes[0, 0]
    ax.plot(interval_times, intervals, ".", markersize=2.2)
    ax.axhline(primary_gap, color="red", linestyle="--", linewidth=0.9, label=f"gap threshold {primary_gap:g}s")
    ax.set_title("Frame interval over time")
    ax.set_ylabel("seconds")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", framealpha=0.9)

    ax = axes[0, 1]
    ax.plot(times, cumulative, linewidth=1.0)
    ax.set_title("Cumulative frames")
    ax.set_ylabel("frame count")
    ax.grid(True, alpha=0.3)

    ax = axes[1, 0]
    if intervals:
        ax.hist(intervals, bins=min(50, max(10, int(len(intervals) ** 0.5))), color="#4C78A8", alpha=0.85)
        median_interval = summary.get("median_interval_seconds")
        if isinstance(median_interval, (int, float)):
            ax.axvline(median_interval, color="black", linestyle="--", linewidth=0.9, label=f"median {median_interval:.3g}s")
            ax.legend(loc="upper left", framealpha=0.9)
    ax.set_title("Frame interval distribution")
    ax.set_xlabel("seconds")
    ax.set_ylabel("count")
    ax.grid(True, alpha=0.3)

    stats_lines = [
        f"Frames: {summary.get('frame_count', ''):,}",
        f"Duration: {summary.get('duration_text', '')}",
        f"Mean rate: {summary.get('mean_acquisition_rate_hz', 0):.4g} Hz",
        f"Median dt: {summary.get('median_interval_seconds', 0):.4g} s",
        f"Gaps>{primary_gap:g}s: {summary.get('gap_count_primary', '')}",
        f"Max dt: {summary.get('max_interval_seconds', 0):.4g} s",
    ]
    stats_text = "\n".join(stats_lines)
    ax.text(
        0.98,
        0.96,
        stats_text,
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=8,
        family="monospace",
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "edgecolor": "0.65", "alpha": 0.9},
    )

    ax = axes[1, 1]
    valid_samples = [row for row in sample_rows if row.get("read_status") == "ok"]
    if valid_samples:
        x = [int(row.get("record_index", i + 1)) for i, row in enumerate(valid_samples)]
        mins = [float(row["raw_stats_min"]) if row.get("raw_stats_min") != "" else float("nan") for row in valid_samples]
        means = [float(row["raw_stats_mean"]) if row.get("raw_stats_mean") != "" else float("nan") for row in valid_samples]
        maxs = [float(row["raw_stats_max"]) if row.get("raw_stats_max") != "" else float("nan") for row in valid_samples]
        ax.plot(x, mins, "o-", label="raw min", markersize=2.5, linewidth=0.9)
        ax.plot(x, means, "o-", label="raw mean", markersize=2.5, linewidth=0.9)
        ax.plot(x, maxs, "o-", label="raw max", markersize=2.5, linewidth=0.9)
        zero_x = [int(row.get("record_index", 0)) for row in valid_samples if row.get("raw_all_zero_by_stats") is True]
        if zero_x:
            ax.scatter(zero_x, [0] * len(zero_x), color="red", marker="x", s=35, label="all-zero sample")
        ax.set_title("Sampled raw data health")
        ax.set_xlabel("record index")
        ax.set_ylabel("raw DN")
        ax.legend(loc="best", framealpha=0.9)
    else:
        ax.text(0.5, 0.5, "No sample-frame raw stats\n(use --sample-frames 15)", ha="center", va="center", fontsize=8)
        ax.set_title("Sampled raw data health")
    ax.grid(True, alpha=0.3)

    for ax in axes.flat[:2]:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        for label in ax.get_xticklabels():
            label.set_rotation(25)
            label.set_horizontalalignment("right")

    figure_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(figure_path, dpi=180)
    plt.close(fig)
    print(f"Overview figure written: {figure_path}")
def make_report(
    input_path: Path,
    json_files: list[Path],
    total_size: int,
    summary: dict[str, Any],
    gap_rows: list[dict[str, Any]],
    sample_rows: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append("FLIR QUICK LOOK HEALTH REPORT")
    lines.append("=" * 31)
    lines.append(f"Input: {input_path}")
    lines.append(f"JSON files: {len(json_files)}")
    lines.append(f"Total size: {total_size / 1024**3:.2f} GiB")
    lines.append("")
    lines.append("Timestamp / acquisition")
    lines.append("-----------------------")
    for key in [
        "status",
        "frame_count",
        "total_scanned_frame_count",
        "edge_exclusion_minutes",
        "start_time",
        "end_time",
        "duration_text",
        "mean_acquisition_rate_hz",
        "expected_rate_from_median_interval_hz",
        "median_interval_seconds",
        "mean_interval_seconds",
        "min_interval_seconds",
        "max_interval_seconds",
        "gap_count_primary",
        "estimated_missing_frames_from_median_interval",
    ]:
        if key in summary:
            value = summary[key]
            if isinstance(value, float):
                value = f"{value:.6g}"
            lines.append(f"{key}: {value}")
    for key in sorted(k for k in summary if k.startswith("gaps_gt_")):
        lines.append(f"{key}: {summary[key]}")

    if gap_rows:
        longest = max(gap_rows, key=lambda row: row.get("gap_seconds", 0))
        lines.append("")
        lines.append("Longest gap")
        lines.append("-----------")
        lines.append(f"gap_start: {longest.get('gap_start')}")
        lines.append(f"gap_end: {longest.get('gap_end')}")
        lines.append(f"gap_seconds: {longest.get('gap_seconds')}")
    else:
        lines.append("")
        lines.append("No gaps above primary threshold were detected.")

    if sample_rows:
        bad_reads = [row for row in sample_rows if row.get("read_status") != "ok"]
        missing_raw = [row for row in sample_rows if not row.get("raw_present")]
        zero_raw = [row for row in sample_rows if row.get("raw_all_zero_by_stats") is True]
        missing_cal = [row for row in sample_rows if not row.get("calibration_required_present")]
        lines.append("")
        lines.append("Sample frame metadata")
        lines.append("---------------------")
        lines.append(f"sampled_frames: {len(sample_rows)}")
        lines.append(f"sample_read_errors: {len(bad_reads)}")
        lines.append(f"sample_missing_raw: {len(missing_raw)}")
        lines.append(f"sample_all_zero_by_raw_stats: {len(zero_raw)}")
        lines.append(f"sample_missing_calibration_constants: {len(missing_cal)}")

    lines.append("")
    lines.append("Interpretation")
    lines.append("--------------")
    if summary.get("frame_count", 0) == 0:
        lines.append("BAD: no FLIR frames were found.")
    elif summary.get("gap_count_primary", 0) > 0:
        lines.append("CHECK: frames exist, but timestamp gaps were detected.")
    else:
        lines.append("OK: frames exist and no gaps above the primary threshold were detected.")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast FLIR camera acquisition health check; no temperature calculation.")
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--recursive-json", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--gap-csv", type=Path, default=DEFAULT_GAP_CSV)
    parser.add_argument("--sample-csv", type=Path, default=DEFAULT_SAMPLE_CSV)
    parser.add_argument("--figure", type=Path, default=Path("FLIR_quick_look_overview.png"))
    parser.add_argument("--sample-frames", type=int, default=1500)
    # Default starts at 5 s because this Zeppelin FLIR dataset is about 0.49 Hz
    # (roughly one frame every 2.06 s). A 2 s threshold would incorrectly flag
    # normal acquisition intervals as gaps. The figure/report now use 2.5 s by default.
    parser.add_argument("--gap-seconds", type=float, nargs="+", default=[2.5, 5.0, 10.0])
    parser.add_argument("--scan-workers", type=int, default=DEFAULT_SCAN_WORKERS)
    parser.add_argument("--scan-chunk-mb", type=int, default=DEFAULT_SCAN_CHUNK_MB)
    parser.add_argument("--exclude-edge-minutes", type=float, default=2.0, help="Ignore first/last N minutes for QC stats and plots.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    json_files = find_json_files(input_path, recursive=args.recursive_json)
    total_size = sum(path.stat().st_size for path in json_files)

    print("FLIR quick look: timestamp/gap/rate check only, no temperature calculation.")
    print(f"Input: {input_path}")
    print(f"JSON files: {len(json_files)}")
    print(f"Total input size: {total_size / 1024**3:.2f} GiB")
    print(f"Scan workers: {args.scan_workers}")
    print(f"Scan chunk: {args.scan_chunk_mb} MB")

    started = time.monotonic()
    entries = scan_timestamps(
        json_files=json_files,
        scan_workers=max(args.scan_workers, 1),
        scan_chunk_mb=max(args.scan_chunk_mb, 1),
    )
    analysis_entries = trim_edge_minutes(entries, edge_minutes=max(args.exclude_edge_minutes, 0.0))
    if len(analysis_entries) != len(entries):
        print(
            f"Using {len(analysis_entries):,}/{len(entries):,} frames after excluding "
            f"first/last {args.exclude_edge_minutes:g} minute(s)."
        )
    summary, gap_rows = calculate_time_statistics(analysis_entries, gap_seconds=sorted(args.gap_seconds))
    summary["total_scanned_frame_count"] = len(entries)
    summary["edge_exclusion_minutes"] = max(args.exclude_edge_minutes, 0.0)
    sample_rows = inspect_sample_frames(analysis_entries, sample_frames=max(args.sample_frames, 0))

    report = make_report(input_path, json_files, total_size, summary, gap_rows, sample_rows)
    args.report.resolve().write_text(report, encoding="utf-8")
    write_csv(args.gap_csv.resolve(), gap_rows)
    write_csv(args.sample_csv.resolve(), sample_rows)
    make_overview_figure(analysis_entries, summary, gap_rows, sample_rows, args.figure.resolve())

    print()
    print(report)
    print(f"Report written: {args.report.resolve()}")
    if gap_rows:
        print(f"Gap CSV written: {args.gap_csv.resolve()}")
    if sample_rows:
        print(f"Sample CSV written: {args.sample_csv.resolve()}")
    print(f"Quick look completed in {format_duration(time.monotonic() - started)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


