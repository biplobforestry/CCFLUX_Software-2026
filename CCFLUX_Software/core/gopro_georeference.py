"""Time-correct and georeference GoPro captures against Noseboom navigation."""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


GOPRO_TIMEZONE = ZoneInfo("Europe/Berlin")


def camera_local_to_utc(value: datetime) -> datetime:
    """Interpret a naive GoPro clock value as Europe/Berlin and return UTC."""
    localized = value.replace(tzinfo=GOPRO_TIMEZONE) if value.tzinfo is None else value
    return localized.astimezone(timezone.utc)


def parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def georeference_captures(
    records: Iterable[Mapping[str, Any]],
    noseboom_points: Iterable[Mapping[str, Any]],
    *,
    maximum_time_delta_seconds: float = 2.5,
) -> list[dict[str, Any]]:
    """Match image captures to the nearest time-sorted Noseboom point."""
    navigation: list[tuple[datetime, Mapping[str, Any]]] = []
    for point in noseboom_points:
        timestamp = parse_utc(point.get("time"))
        if timestamp is None:
            continue
        try:
            latitude = float(point["lat"])
            longitude = float(point["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        navigation.append((timestamp, point))
    navigation.sort(key=lambda item: item[0])
    times = [item[0] for item in navigation]
    captures: list[dict[str, Any]] = []
    if not times:
        return captures

    image_records = [
        record for record in records
        if str(record.get("kind", "")).casefold() == "image"
    ]
    image_records.sort(
        key=lambda item: parse_utc(item.get("timestamp")) or datetime.max.replace(
            tzinfo=timezone.utc
        )
    )
    for record in image_records:
        timestamp = parse_utc(record.get("timestamp"))
        if timestamp is None:
            continue
        position = bisect_left(times, timestamp)
        candidates = [
            index for index in (position - 1, position)
            if 0 <= index < len(navigation)
        ]
        if not candidates:
            continue
        nearest_index = min(
            candidates, key=lambda index: abs((times[index] - timestamp).total_seconds())
        )
        matched_time, point = navigation[nearest_index]
        delta = abs((matched_time - timestamp).total_seconds())
        if delta > maximum_time_delta_seconds:
            continue
        source = Path(str(record.get("source_file", "")))
        altitude = point.get("altitude_m")
        if altitude is None:
            altitude = point.get("height_m")
        captures.append(
            {
                "capture_id": str(len(captures) + 1),
                "image_id": source.stem or source.name,
                "file_name": source.name,
                "source_file": str(source),
                "capture_time_utc": timestamp.isoformat().replace("+00:00", "Z"),
                "capture_time_camera": timestamp.astimezone(GOPRO_TIMEZONE).isoformat(),
                "noseboom_time_utc": matched_time.isoformat().replace("+00:00", "Z"),
                "time_delta_seconds": round(delta, 3),
                "latitude": float(point["lat"]),
                "longitude": float(point["lon"]),
                "altitude_m": _finite(altitude),
            }
        )
    return captures


def public_capture(capture: Mapping[str, Any]) -> dict[str, Any]:
    """Remove local filesystem paths before returning capture data to a browser."""
    return {
        key: value for key, value in capture.items()
        if key != "source_file"
    }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None
