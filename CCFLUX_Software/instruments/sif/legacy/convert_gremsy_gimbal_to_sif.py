#!/usr/bin/env python
"""Convert Gremsy gimbal CSV logs to the legacy SIF angle CSV format.

Output columns match the original SIF processing input:
lat,lon,alt_above_ground_m,date_time_utc,pitch,roll,yaw
"""

from __future__ import annotations

import argparse
import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean


# Instrument CSVs are not always UTF-8. Acquisition software on Windows writes
# headers in cp1252, where a degree sign is the single byte 0xb0 that UTF-8
# rejects: a column named 'Temp [degC]' spelled with the symbol ended the whole
# run with "invalid start byte" and named no file. The encoding is decided from
# the head of the file, which is where such a name lives.
TEXT_PROBE_BYTES = 1 << 20


def detect_encoding(path: Path) -> str:
    try:
        with Path(path).open("rb") as probe:
            head = probe.read(TEXT_PROBE_BYTES)
    except OSError:
        return "utf-8-sig"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # cp1252 has no invalid bytes, and turns 0xb0 back into the degree sign.
        return "cp1252"
    return "utf-8-sig"


def open_text(path: Path, newline: str = ""):
    """Read *path* as written. A stray byte deep inside a UTF-8 file degrades
    one character rather than ending an hour of processing."""
    encoding = detect_encoding(path)
    if encoding == "cp1252":
        return Path(path).open("r", newline=newline, encoding=encoding)
    return Path(path).open("r", newline=newline, encoding=encoding, errors="replace")


SIF_COLUMNS = [
    "lat",
    "lon",
    "alt_above_ground_m",
    "date_time_utc",
    "pitch",
    "roll",
    "yaw",
]


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    if math.isnan(result):
        return None
    return result


def parse_utc_timestamp(value: str) -> str:
    """Return SIF-style UTC timestamp without fractional seconds."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_angle_180(value: float | None) -> float | None:
    if value is None:
        return None
    return ((value + 180.0) % 360.0) - 180.0


def format_value(value: float | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if math.isnan(value):
        return ""
    return f"{value:.10g}"


def update_stats(stats: dict[str, list[float]], row: dict[str, str]) -> None:
    for key in ("lat", "lon", "alt_above_ground_m", "pitch", "roll", "yaw"):
        value = parse_float(row.get(key))
        if value is not None:
            stats.setdefault(key, []).append(value)


def print_stats(stats: dict[str, list[float]], rows_written: int, rows_skipped: int) -> None:
    print(f"rows_written={rows_written}")
    print(f"rows_skipped={rows_skipped}")
    for key in SIF_COLUMNS:
        if key == "date_time_utc":
            continue
        values = stats.get(key, [])
        if not values:
            print(f"{key}: empty")
            continue
        print(
            f"{key}: min={min(values):.6g}, mean={mean(values):.6g}, max={max(values):.6g}"
        )


def convert(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stats: dict[str, list[float]] = {}
    rows_written = 0
    rows_skipped = 0

    with open_text(input_path) as src:
        reader = csv.DictReader(src)
        missing = [col for col in args.required_columns if col not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing required Gremsy column(s): {', '.join(missing)}")

        with output_path.open("w", newline="", encoding="utf-8") as dst:
            writer = csv.DictWriter(dst, fieldnames=SIF_COLUMNS)
            writer.writeheader()

            for source_row in reader:
                ok_value = parse_float(source_row.get("gimbal_downfacing_ok_binary"))
                if args.require_downfacing_ok and ok_value != 1.0:
                    rows_skipped += 1
                    continue

                pitch = parse_float(source_row.get(args.pitch_column))
                roll = parse_float(source_row.get(args.roll_column))
                yaw = parse_float(source_row.get(args.yaw_column))

                if args.invert_pitch and pitch is not None:
                    pitch = -pitch
                if args.invert_roll and roll is not None:
                    roll = -roll
                if args.invert_yaw and yaw is not None:
                    yaw = -yaw
                if args.normalize_yaw:
                    yaw = normalize_angle_180(yaw)

                try:
                    date_time_utc = parse_utc_timestamp(source_row[args.time_column])
                except Exception as exc:
                    rows_skipped += 1
                    if args.skip_bad_rows:
                        continue
                    raise SystemExit(f"Bad timestamp in row {rows_written + rows_skipped}: {exc}")

                output_row = {
                    "lat": format_value(args.lat),
                    "lon": format_value(args.lon),
                    "alt_above_ground_m": format_value(args.alt_above_ground_m),
                    "date_time_utc": date_time_utc,
                    "pitch": format_value(pitch),
                    "roll": format_value(roll),
                    "yaw": format_value(yaw),
                }
                writer.writerow(output_row)
                update_stats(stats, output_row)
                rows_written += 1

    print(f"wrote {output_path}")
    print_stats(stats, rows_written, rows_skipped)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Gremsy_T3V3_Gimbal.csv to legacy SIF angle CSV format."
    )
    parser.add_argument("input", help="Gremsy gimbal CSV exported from InfluxDB")
    parser.add_argument("output", help="Output CSV for SIF processing")
    parser.add_argument("--lat", type=float, default=None, help="Constant latitude to write")
    parser.add_argument("--lon", type=float, default=None, help="Constant longitude to write")
    parser.add_argument(
        "--alt-above-ground-m",
        type=float,
        default=None,
        help="Constant altitude above ground to write",
    )
    parser.add_argument("--time-column", default="_time")
    parser.add_argument("--pitch-column", default="gimbal_pitch_deg")
    parser.add_argument("--roll-column", default="gimbal_roll_deg")
    parser.add_argument("--yaw-column", default="gimbal_yaw_absolute_deg")
    parser.add_argument(
        "--invert-pitch",
        action="store_true",
        help=(
            "Invert pitch sign. Default keeps SIF convention: "
            "pitch < 0 points toward ground, pitch > 0 points skyward."
        ),
    )
    parser.add_argument("--invert-roll", action="store_true", help="Invert roll sign")
    parser.add_argument("--invert-yaw", action="store_true", help="Invert yaw sign")
    parser.add_argument(
        "--no-normalize-yaw",
        dest="normalize_yaw",
        action="store_false",
        help="Do not normalize yaw to [-180, 180].",
    )
    parser.add_argument(
        "--require-downfacing-ok",
        action="store_true",
        help="Keep only rows where gimbal_downfacing_ok_binary equals 1.",
    )
    parser.add_argument(
        "--skip-bad-rows",
        action="store_true",
        help="Skip rows with bad timestamps instead of stopping.",
    )
    parser.set_defaults(invert_pitch=False, normalize_yaw=True)
    parser.set_defaults(
        required_columns=[
            "_time",
            "gimbal_pitch_deg",
            "gimbal_roll_deg",
            "gimbal_yaw_absolute_deg",
        ]
    )
    return parser


if __name__ == "__main__":
    convert(build_parser().parse_args())
