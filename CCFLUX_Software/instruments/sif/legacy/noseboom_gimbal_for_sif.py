#!/usr/bin/env python
"""Prepare Noseboom and Gremsy gimbal inputs for SIF processing.

Commands:
  convert-gimbal  Convert Gremsy gimbal CSV to legacy SIF angle CSV format.
  demo-noseboom   Create demo 100 Hz Noseboom INS/GPS data over the gimbal time range.
  make-sif        Match Noseboom INS/GPS to Gremsy time and write final SIF CSV.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import random
from datetime import datetime, timedelta, timezone
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

# The Gremsy publishes gimbal_pitch_deg relative to the horizon, reading -90 with
# the sensor at nadir; on Flight_CC0806 it holds that to within a tenth of a
# degree for 99.5% of the flight. The AirFloX log instead wants the angle off
# level - "0 = aircraft is level, <0 = towards ground, >0 = to sky" (AirFloX data
# processing manual, "Log file angle definition"). Both count the same direction
# as positive, so the conversion is the offset between their zeros: tilting the
# sensor up off nadir raises both.
NADIR_GIMBAL_PITCH_DEG = -90.0


def pitch_off_level(gimbal_pitch: float) -> float:
    """Gremsy gimbal pitch as the AirFloX manual's angle off level."""
    return gimbal_pitch - NADIR_GIMBAL_PITCH_DEG


NOSEBOOM_DEMO_COLUMNS = [
    "TIMESTAMP",
    "INS_Filter_LLHPos_Latitude_deg",
    "INS_Filter_LLHPos_Longitude_deg",
    "INS_Filter_LLHPos_ElipsoidHeight_m",
]

# A Noseboom export may name its columns with or without a leading 'NoseBoom_',
# depending on the logger configuration. The prefix is removed as the header is
# read, so the column names above match either kind of file.
NOSEBOOM_COLUMN_PREFIX = "NoseBoom_"


def normalize_column_name(column: str) -> str:
    return str(column).removeprefix(NOSEBOOM_COLUMN_PREFIX)


def normalize_noseboom_fieldnames(fieldnames, source: str) -> list[str]:
    """Header without the prefix, refusing a file where two columns collide."""
    names = list(fieldnames or [])
    sources: dict[str, list[str]] = {}
    for column in names:
        sources.setdefault(normalize_column_name(column), []).append(str(column))
    duplicates = {name: found for name, found in sources.items() if len(found) > 1}
    if duplicates:
        detail = "; ".join(
            f"{name} (from {' and '.join(found)})"
            for name, found in sorted(duplicates.items())
        )
        raise SystemExit(
            f"Duplicate Noseboom column name(s) in {source} after removing the "
            f"{NOSEBOOM_COLUMN_PREFIX!r} prefix: {detail}. "
            "Keep only the prefixed or only the unprefixed copy of each column."
        )
    return [normalize_column_name(column) for column in names]


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


def parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # CCFLUX discovery fix, not a science change: Noseboom deliveries write
    # nanosecond precision (…52.602000000), which fromisoformat rejects because
    # it accepts only 3 or 6 fractional digits. Truncate to microseconds.
    if "." in text:
        prefix, suffix = text.split(".", 1)
        fraction_length = 0
        while fraction_length < len(suffix) and suffix[fraction_length].isdigit():
            fraction_length += 1
        if fraction_length > 6:
            text = prefix + "." + suffix[:6] + suffix[fraction_length:]
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_sif_time(value: str) -> str:
    return parse_datetime(value).strftime("%Y-%m-%d %H:%M:%S")


def format_noseboom_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


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


def sort_sif_csv(csv_path: Path) -> None:
    with open_text(csv_path) as src:
        reader = csv.DictReader(src)
        rows = list(reader)
    rows.sort(key=lambda row: parse_datetime(row["date_time_utc"]))
    with csv_path.open("w", newline="", encoding="utf-8") as dst:
        writer = csv.DictWriter(dst, fieldnames=SIF_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

def yes_no(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"yes", "y", "true", "1", "on"}:
        return True
    if text in {"no", "n", "false", "0", "off"}:
        return False
    raise argparse.ArgumentTypeError("Use yes or no")

def read_time_range(csv_path: Path, time_column: str) -> tuple[datetime, datetime]:
    min_time: datetime | None = None
    max_time: datetime | None = None
    with open_text(csv_path) as src:
        reader = csv.DictReader(src)
        if time_column not in (reader.fieldnames or []):
            raise SystemExit(f"Missing time column: {time_column}")
        for row in reader:
            value = row.get(time_column)
            if not value:
                continue
            current = parse_datetime(value)
            if min_time is None or current < min_time:
                min_time = current
            if max_time is None or current > max_time:
                max_time = current
    if min_time is None or max_time is None:
        raise SystemExit(f"No timestamps found in {csv_path}")
    return min_time, max_time


def convert_gimbal(args: argparse.Namespace) -> None:
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

                # The reflection already carries the sign change, so the two
                # pitch options are alternatives rather than a sequence.
                if args.pitch_from_nadir and pitch is not None:
                    pitch = pitch_off_level(pitch)
                elif args.invert_pitch and pitch is not None:
                    pitch = -pitch
                if args.invert_roll and roll is not None:
                    roll = -roll
                if args.invert_yaw and yaw is not None:
                    yaw = -yaw
                if args.normalize_yaw:
                    yaw = normalize_angle_180(yaw)

                try:
                    date_time_utc = format_sif_time(source_row[args.time_column])
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

    sort_sif_csv(output_path)
    print(f"wrote {output_path}")
    print_stats(stats, rows_written, rows_skipped)


def demo_noseboom(args: argparse.Namespace) -> None:
    gimbal_path = Path(args.gimbal_csv)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    start_time, end_time = read_time_range(gimbal_path, args.gimbal_time_column)
    step = timedelta(seconds=1.0 / args.hz)
    rng = random.Random(args.seed)

    base_lat = args.start_lat
    base_lon = args.start_lon
    base_height = args.start_height_m
    total_seconds = max((end_time - start_time).total_seconds(), 1.0)
    target_rows = int(total_seconds * args.hz) + 1
    if args.max_rows is not None:
        target_rows = min(target_rows, args.max_rows)

    with output_path.open("w", newline="", encoding="utf-8") as dst:
        writer = csv.DictWriter(dst, fieldnames=NOSEBOOM_DEMO_COLUMNS)
        writer.writeheader()
        for index in range(target_rows):
            current_time = start_time + step * index
            progress = index / max(target_rows - 1, 1)

            # Demo path: a smooth Zeppelin-like track over NRW near Cologne/Bonn.
            lat = base_lat + 0.055 * progress + 0.0012 * math.sin(progress * math.tau * 5.0)
            lon = base_lon + 0.085 * progress + 0.0018 * math.cos(progress * math.tau * 4.0)
            height = (
                base_height
                + 35.0 * math.sin(progress * math.tau * 2.0)
                + 8.0 * math.sin(progress * math.tau * 17.0)
                + rng.uniform(-0.35, 0.35)
            )

            writer.writerow(
                {
                    "TIMESTAMP": format_noseboom_timestamp(current_time),
                    "INS_Filter_LLHPos_Latitude_deg": f"{lat:.9f}",
                    "INS_Filter_LLHPos_Longitude_deg": f"{lon:.9f}",
                    "INS_Filter_LLHPos_ElipsoidHeight_m": f"{height:.3f}",
                }
            )

    print(f"wrote {output_path}")
    print(f"gimbal_start={format_noseboom_timestamp(start_time)}")
    print(f"gimbal_end={format_noseboom_timestamp(end_time)}")
    print(f"hz={args.hz}")
    print(f"rows_written={target_rows}")
    if args.max_rows is not None and target_rows < int(total_seconds * args.hz) + 1:
        print("note=demo capped by --max-rows; remove that option for full-range 100 Hz data")


def load_noseboom_positions(args: argparse.Namespace) -> list[tuple[datetime, float, float, float]]:
    noseboom_path = Path(args.noseboom_csv)
    samples: list[tuple[datetime, float, float, float]] = []
    with open_text(noseboom_path) as src:
        reader = csv.DictReader(src)
        # Reassigned before the first row is read, so every row is keyed by the
        # unprefixed name and the matching below is unchanged.
        reader.fieldnames = normalize_noseboom_fieldnames(
            reader.fieldnames, noseboom_path.name
        )
        required = [args.noseboom_time_column, args.noseboom_lat_column, args.noseboom_lon_column, args.noseboom_alt_column]
        missing = [col for col in required if col not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing required Noseboom column(s): {', '.join(missing)}")
        for row_number, row in enumerate(reader, start=2):
            try:
                timestamp = parse_datetime(row[args.noseboom_time_column])
                lat = parse_float(row.get(args.noseboom_lat_column))
                lon = parse_float(row.get(args.noseboom_lon_column))
                alt = parse_float(row.get(args.noseboom_alt_column))
            except Exception as exc:
                if args.skip_bad_rows:
                    continue
                raise SystemExit(f"Bad Noseboom row {row_number}: {exc}")
            if lat is None or lon is None or alt is None:
                if args.skip_bad_rows:
                    continue
                raise SystemExit(f"Bad Noseboom numeric value in row {row_number}")
            samples.append((timestamp, lat, lon, alt))
    if len(samples) < 2:
        raise SystemExit("Need at least two valid Noseboom samples for interpolation")
    samples.sort(key=lambda item: item[0])

    deduped: list[tuple[datetime, float, float, float]] = []
    for sample in samples:
        if deduped and sample[0] == deduped[-1][0]:
            deduped[-1] = sample
        else:
            deduped.append(sample)
    if len(deduped) < 2:
        raise SystemExit("Need at least two unique Noseboom timestamps for matching")
    return deduped


def nearest_position(
    target_time: datetime,
    samples: list[tuple[datetime, float, float, float]],
    sample_times: list[datetime],
    max_gap_seconds: float,
) -> tuple[float, float, float, float] | None:
    index = bisect.bisect_left(sample_times, target_time)
    candidates = []
    if index < len(samples):
        candidates.append(samples[index])
    if index > 0:
        candidates.append(samples[index - 1])
    if not candidates:
        return None
    nearest = min(candidates, key=lambda item: abs((item[0] - target_time).total_seconds()))
    delta = abs((nearest[0] - target_time).total_seconds())
    if delta > max_gap_seconds:
        return None
    _, lat, lon, alt = nearest
    return lat, lon, alt, delta


def detect_flight_interval(
    samples: list[tuple[datetime, float, float, float]],
    min_altitude_span_m: float = 10.0,
) -> tuple[datetime, datetime, str]:
    altitudes = sorted(sample[3] for sample in samples if math.isfinite(sample[3]))
    if len(altitudes) < 20:
        return samples[0][0], samples[-1][0], "altitude filter skipped: too few valid Noseboom altitude samples"
    p05 = altitudes[int(0.05 * (len(altitudes) - 1))]
    p95 = altitudes[int(0.95 * (len(altitudes) - 1))]
    span = p95 - p05
    if span < min_altitude_span_m:
        return samples[0][0], samples[-1][0], f"altitude filter skipped: altitude span {span:.2f} m is too small"
    threshold = p05 + max(5.0, 0.10 * span)
    mask = [sample[3] >= threshold for sample in samples]

    best_start = best_end = None
    best_len = 0
    current_start = None
    for idx, keep in enumerate(mask):
        if keep and current_start is None:
            current_start = idx
        if (not keep or idx == len(mask) - 1) and current_start is not None:
            current_end = idx if keep and idx == len(mask) - 1 else idx - 1
            current_len = current_end - current_start + 1
            if current_len > best_len:
                best_start, best_end, best_len = current_start, current_end, current_len
            current_start = None
    if best_start is None or best_end is None or best_len < 2:
        return samples[0][0], samples[-1][0], "altitude filter skipped: no stable flying interval detected"
    start_time = samples[best_start][0]
    end_time = samples[best_end][0]
    return start_time, end_time, (
        f"altitude filter kept {start_time.strftime('%Y-%m-%d %H:%M:%S')} to "
        f"{end_time.strftime('%Y-%m-%d %H:%M:%S')} UTC using threshold {threshold:.2f} m"
    )


def make_sif(args: argparse.Namespace) -> None:
    gimbal_path = Path(args.gimbal_csv)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    noseboom_samples = load_noseboom_positions(args)
    noseboom_times = [sample[0] for sample in noseboom_samples]
    stats: dict[str, list[float]] = {}
    rows_written = 0
    rows_skipped = 0
    rows_without_position = 0
    max_observed_gap = 0.0
    flight_start = flight_end = None
    if getattr(args, "altitude_filter", False):
        flight_start, flight_end, message = detect_flight_interval(noseboom_samples)
        print(f"warning={message}")

    with open_text(gimbal_path) as src:
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

                try:
                    gimbal_time = parse_datetime(source_row[args.gimbal_time_column])
                    date_time_utc = gimbal_time.strftime("%Y-%m-%d %H:%M:%S")
                except Exception as exc:
                    rows_skipped += 1
                    if args.skip_bad_rows:
                        continue
                    raise SystemExit(f"Bad Gremsy timestamp in row {rows_written + rows_skipped}: {exc}")

                if flight_start is not None and flight_end is not None and not (flight_start <= gimbal_time <= flight_end):
                    rows_skipped += 1
                    continue

                position = nearest_position(
                    gimbal_time,
                    noseboom_samples,
                    noseboom_times,
                    args.max_position_gap_sec,
                )
                if position is None:
                    rows_without_position += 1
                    if args.drop_unmatched:
                        rows_skipped += 1
                        continue
                    lat = lon = alt = None
                else:
                    lat, lon, alt, position_gap = position
                    max_observed_gap = max(max_observed_gap, position_gap)

                pitch = parse_float(source_row.get(args.pitch_column))
                roll = parse_float(source_row.get(args.roll_column))
                yaw = parse_float(source_row.get(args.yaw_column))
                # See convert_gimbal: the reflection already carries the sign.
                if args.pitch_from_nadir and pitch is not None:
                    pitch = pitch_off_level(pitch)
                elif args.invert_pitch and pitch is not None:
                    pitch = -pitch
                if args.invert_roll and roll is not None:
                    roll = -roll
                if args.invert_yaw and yaw is not None:
                    yaw = -yaw
                if args.normalize_yaw:
                    yaw = normalize_angle_180(yaw)

                output_row = {
                    "lat": format_value(lat),
                    "lon": format_value(lon),
                    "alt_above_ground_m": format_value(alt),
                    "date_time_utc": date_time_utc,
                    "pitch": format_value(pitch),
                    "roll": format_value(roll),
                    "yaw": format_value(yaw),
                }
                writer.writerow(output_row)
                update_stats(stats, output_row)
                rows_written += 1

    sort_sif_csv(output_path)
    print(f"wrote {output_path}")
    print_stats(stats, rows_written, rows_skipped)
    print(f"noseboom_samples={len(noseboom_samples)}")
    print(f"rows_without_position={rows_without_position}")
    print(f"max_position_gap_sec={args.max_position_gap_sec}")
    print(f"max_observed_position_gap_sec={max_observed_gap:.6f}")
    if rows_without_position:
        print("warning=Some Gimbal timestamps had no Noseboom sample within the allowed time gap.")


def add_convert_gimbal_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "convert-gimbal",
        help="Convert Gremsy_T3V3_Gimbal.csv to legacy SIF angle CSV format.",
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
    parser.add_argument(
        "--pitch-from-nadir",
        action="store_true",
        help=(
            "Read the pitch column as Gremsy gimbal pitch (-90 = nadir) and write "
            "the AirFloX manual's angle off level. Supersedes --invert-pitch."
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
    parser.set_defaults(
        func=convert_gimbal,
        invert_pitch=False,
        pitch_from_nadir=False,
        normalize_yaw=True,
        required_columns=[
            "_time",
            "gimbal_pitch_deg",
            "gimbal_roll_deg",
            "gimbal_yaw_absolute_deg",
        ],
    )


def add_demo_noseboom_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "demo-noseboom",
        help="Create demo 100 Hz Noseboom INS/GPS CSV over the Gremsy time range.",
    )
    parser.add_argument("gimbal_csv", help="Gremsy gimbal CSV used to get start/end time")
    parser.add_argument("output", help="Output demo Noseboom CSV")
    parser.add_argument("--gimbal-time-column", default="_time")
    parser.add_argument("--hz", type=float, default=100.0)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional cap for a small demo. Omit for full 100 Hz gimbal time range.",
    )
    parser.add_argument("--seed", type=int, default=20260710)
    parser.add_argument("--start-lat", type=float, default=50.82)
    parser.add_argument("--start-lon", type=float, default=6.96)
    parser.add_argument("--start-height-m", type=float, default=320.0)
    parser.set_defaults(func=demo_noseboom)


def add_make_sif_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "make-sif",
        help="Fill SIF lat/lon/alt from Noseboom by time-matching to Gremsy gimbal rows.",
    )
    parser.add_argument("gimbal_csv", help="Gremsy gimbal CSV exported from InfluxDB")
    parser.add_argument("noseboom_csv", help="Noseboom INS/GPS CSV")
    parser.add_argument("output", help="Output SIF CSV with position and gimbal attitude")
    parser.add_argument("--gimbal-time-column", default="_time")
    parser.add_argument("--noseboom-time-column", default="TIMESTAMP")
    parser.add_argument("--noseboom-lat-column", default="INS_Filter_LLHPos_Latitude_deg")
    parser.add_argument("--noseboom-lon-column", default="INS_Filter_LLHPos_Longitude_deg")
    parser.add_argument("--noseboom-alt-column", default="INS_Filter_LLHPos_ElipsoidHeight_m")
    parser.add_argument("--pitch-column", default="gimbal_pitch_deg")
    parser.add_argument("--roll-column", default="gimbal_roll_deg")
    parser.add_argument("--yaw-column", default="gimbal_yaw_absolute_deg")
    parser.add_argument(
        "--max-position-gap-sec",
        type=float,
        default=0.2,
        help="Maximum allowed absolute time difference to the nearest Noseboom sample.",
    )
    parser.add_argument(
        "--drop-unmatched",
        action="store_true",
        help="Drop gimbal rows that cannot be matched to Noseboom position.",
    )
    parser.add_argument(
        "--altitude-filter",
        type=yes_no,
        default=False,
        metavar="yes/no",
        help="Use Noseboom altitude to keep only the detected flying interval.",
    )
    parser.add_argument(
        "--invert-pitch",
        action="store_true",
        help=(
            "Invert pitch sign. Default keeps SIF convention: "
            "pitch < 0 points toward ground, pitch > 0 points skyward."
        ),
    )
    parser.add_argument(
        "--pitch-from-nadir",
        action="store_true",
        help=(
            "Read the pitch column as Gremsy gimbal pitch (-90 = nadir) and write "
            "the AirFloX manual's angle off level. Supersedes --invert-pitch."
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
        help="Skip bad Noseboom/Gimbal rows instead of stopping.",
    )
    parser.set_defaults(
        func=make_sif,
        invert_pitch=False,
        pitch_from_nadir=False,
        normalize_yaw=True,
        required_columns=[
            "_time",
            "gimbal_pitch_deg",
            "gimbal_roll_deg",
            "gimbal_yaw_absolute_deg",
        ],
    )


def is_sif_log(path: Path) -> bool:
    try:
        with open_text(path) as src:
            reader = csv.DictReader(src)
            return set(SIF_COLUMNS).issubset(set(reader.fieldnames or []))
    except Exception:
        return False


def find_hatchbox_folder(flight_root: Path) -> Path:
    direct = flight_root / "HATCH-BOX"
    if direct.exists():
        return direct
    matches = sorted(p for p in flight_root.rglob("HATCH-BOX") if p.is_dir())
    if not matches:
        raise FileNotFoundError(f"No HATCH-BOX folder found below {flight_root}")
    return matches[0]


def find_existing_sif_log(hatchbox: Path) -> Path | None:
    csv_files = sorted(set(hatchbox.glob("*.csv")) | set(hatchbox.glob("*.CSV")))
    preferred = [
        path for path in csv_files
        if ("log_" in path.name.lower() or "conv_ang" in path.name.lower()) and is_sif_log(path)
    ]
    if preferred:
        return preferred[0]
    fallback = [path for path in csv_files if ("log" in path.name.lower() or "conv" in path.name.lower()) and is_sif_log(path)]
    return fallback[0] if fallback else None


def find_gimbal_file(hatchbox: Path) -> Path:
    # CCFLUX discovery fix, not a science change: search subfolders too.
    csv_files = sorted(set(hatchbox.rglob("*.csv")) | set(hatchbox.rglob("*.CSV")))
    candidates = [path for path in csv_files if "gimbal" in path.name.lower() and "gremsy" in path.name.lower()]
    if not candidates:
        candidates = [path for path in csv_files if "gimbal" in path.name.lower()]
    if not candidates:
        raise FileNotFoundError(f"No Gimbal CSV found in {hatchbox}")
    return candidates[0]


def find_noseboom_file(hatchbox: Path) -> Path:
    # CCFLUX discovery fix, not a science change: search subfolders too.
    csv_files = sorted(set(hatchbox.rglob("*.csv")) | set(hatchbox.rglob("*.CSV")))
    candidates = [path for path in csv_files if "noseboom" in path.name.lower()]
    if not candidates:
        candidates = [path for path in csv_files if "ins" in path.name.lower() and "100hz" in path.name.lower()]
    if not candidates:
        raise FileNotFoundError(f"No Noseboom INS/GPS CSV found in {hatchbox}")
    return candidates[0]


GEOID_SAMPLE_ROWS = 20000


def median_geoid_separation(
    noseboom_csv: Path, sample_rows: int = GEOID_SAMPLE_ROWS
) -> float | None:
    """Ellipsoid height minus mean-sea-level height, from the receiver with both.

    The INS solution is the better position but publishes only an ellipsoid
    height, while a terrain model is referenced to sea level, so the two cannot
    be differenced without this. GNSSRecv1 reports both. The separation is a
    property of the geoid and moves by well under a metre across one flight, so
    the median of the opening rows stands for the whole record and costs a few
    megabytes rather than a second pass over the delivery.
    """
    ellipsoid = "GNSSRecv1_LLHPos_ElipsoidHeight_m"
    sea_level = "GNSSRecv1_LLHPos_MSLHeight_m"
    separations: list[float] = []
    with open_text(Path(noseboom_csv)) as src:
        reader = csv.DictReader(src)
        reader.fieldnames = normalize_noseboom_fieldnames(
            reader.fieldnames, Path(noseboom_csv).name
        )
        names = reader.fieldnames or []
        if ellipsoid not in names or sea_level not in names:
            return None
        for index, row in enumerate(reader):
            if index >= sample_rows:
                break
            high = parse_float(row.get(ellipsoid))
            low = parse_float(row.get(sea_level))
            if high is not None and low is not None:
                separations.append(high - low)
    if not separations:
        return None
    separations.sort()
    return separations[len(separations) // 2]


def to_height_above_ground(
    csv_path: Path, geoid_separation: float, terrain_sampler
) -> tuple[int, int, int]:
    """Rewrite alt_above_ground_m as what its name says.

    The column is filled from the Noseboom's INS ellipsoid height, because that
    is the only altitude the INS publishes. Height above ground is that minus
    the geoid separation, which puts it above sea level, minus the ground
    elevation there. SIF turns this column straight into the footprint radius
    (Alt x tan 11.5 deg), so the difference is a scientific one and not a label:
    on Flight_CC0806 the ground sits near 165 m ellipsoid, so an uncorrected
    column reports a hundred-metre footprint for a spectrometer sitting on it.

    ``terrain_sampler`` takes (latitudes, longitudes) and returns ground
    elevation in metres above sea level, NaN where it could not be sampled.
    Returns (rows converted, rows left as delivered, rows below ground).

    A row can come out slightly negative: the terrain model is a raster of
    roughly forty metres per pixel, so on the ground its elevation and the
    aircraft's own altitude disagree by a few metres. The reference AirFloX log
    does the same thing, reaching -6.6 m, so the values are reported as measured
    rather than clamped - a floor of zero would be an invented altitude.
    """
    with open_text(Path(csv_path)) as src:
        rows = list(csv.DictReader(src))
    if not rows:
        return 0, 0, 0
    latitudes = [parse_float(row.get("lat")) for row in rows]
    longitudes = [parse_float(row.get("lon")) for row in rows]
    ground = terrain_sampler(latitudes, longitudes)
    converted = 0
    unchanged = 0
    below_ground = 0
    for row, elevation in zip(rows, ground):
        height = parse_float(row.get("alt_above_ground_m"))
        # NaN compares unequal to itself; that is how the sampler reports a tile
        # it could not fetch, and a guessed ground is worse than none.
        if height is None or elevation is None or elevation != elevation:
            unchanged += 1
            continue
        above = height - geoid_separation - float(elevation)
        row["alt_above_ground_m"] = format_value(above)
        converted += 1
        if above < 0:
            below_ground += 1
    with Path(csv_path).open("w", newline="", encoding="utf-8") as dst:
        writer = csv.DictWriter(dst, fieldnames=SIF_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return converted, unchanged, below_ground


def prepare_sif_log_from_hatchbox(
    flight_root: Path,
    output_dir: Path,
    custom_log: Path | None = None,
    altitude_filter: bool = False,
    max_position_gap_sec: float = 0.2,
    invert_pitch: bool = False,
    pitch_from_nadir: bool = False,
    terrain_sampler=None,
) -> Path:
    if custom_log is not None:
        custom_log = Path(custom_log)
        if not is_sif_log(custom_log):
            raise ValueError(f"Provided log is not in SIF format: {custom_log}")
        return custom_log

    hatchbox = find_hatchbox_folder(Path(flight_root))
    existing_log = find_existing_sif_log(hatchbox)
    if existing_log is not None:
        if altitude_filter:
            print("warning=Altitude_filter requested, but an existing SIF log was found and used unchanged.")
        return existing_log

    # CCFLUX discovery fix, not a science change: deliveries commonly keep the
    # gimbal or the processed NoseBoom product outside the HATCH-BOX tree, so
    # fall back to the whole flight folder before giving up.
    try:
        gimbal_csv = find_gimbal_file(hatchbox)
    except FileNotFoundError:
        gimbal_csv = find_gimbal_file(Path(flight_root))
        print(f"warning=Gimbal position was discovered outside HATCH-BOX: {gimbal_csv}")
    try:
        noseboom_csv = find_noseboom_file(hatchbox)
    except FileNotFoundError:
        noseboom_csv = find_noseboom_file(Path(flight_root))
        print(f"warning=Noseboom position was discovered outside HATCH-BOX: {noseboom_csv}")
    output_path = Path(output_dir) / "_combined" / f"{Path(flight_root).name}_noseboom_gimbal_sif_log.csv"
    args = argparse.Namespace(
        gimbal_csv=gimbal_csv,
        noseboom_csv=noseboom_csv,
        output=output_path,
        gimbal_time_column="_time",
        noseboom_time_column="TIMESTAMP",
        noseboom_lat_column="INS_Filter_LLHPos_Latitude_deg",
        noseboom_lon_column="INS_Filter_LLHPos_Longitude_deg",
        noseboom_alt_column="INS_Filter_LLHPos_ElipsoidHeight_m",
        pitch_column="gimbal_pitch_deg",
        roll_column="gimbal_roll_deg",
        yaw_column="gimbal_yaw_absolute_deg",
        max_position_gap_sec=max_position_gap_sec,
        drop_unmatched=True,
        altitude_filter=altitude_filter,
        invert_pitch=invert_pitch,
        pitch_from_nadir=pitch_from_nadir,
        invert_roll=False,
        invert_yaw=False,
        normalize_yaw=True,
        require_downfacing_ok=False,
        skip_bad_rows=True,
        required_columns=["_time", "gimbal_pitch_deg", "gimbal_roll_deg", "gimbal_yaw_absolute_deg"],
    )
    make_sif(args)
    if terrain_sampler is not None:
        separation = median_geoid_separation(noseboom_csv)
        if separation is None:
            print(
                "warning=The Noseboom publishes no mean-sea-level height beside "
                "its ellipsoid height, so alt_above_ground_m is the ellipsoid "
                "height and the SIF footprint radius is computed from it."
            )
        else:
            converted, unchanged, below_ground = to_height_above_ground(
                output_path, separation, terrain_sampler
            )
            print(
                f"note=alt_above_ground_m converted to height above ground on "
                f"{converted} row(s); geoid separation {separation:.2f} m"
            )
            if unchanged:
                print(
                    f"warning={unchanged} row(s) had no terrain sample and keep "
                    "the ellipsoid height."
                )
            if below_ground:
                print(
                    f"note={below_ground} row(s) sit just below the terrain "
                    "model, which is a raster of about forty metres per pixel; "
                    "these are on the ground and are reported as measured."
                )
    if not is_sif_log(output_path):
        raise RuntimeError(f"Generated log is not a valid SIF log: {output_path}")
    return output_path

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_convert_gimbal_parser(subparsers)
    add_demo_noseboom_parser(subparsers)
    add_make_sif_parser(subparsers)
    return parser


if __name__ == "__main__":
    parsed_args = build_parser().parse_args()
    parsed_args.func(parsed_args)







