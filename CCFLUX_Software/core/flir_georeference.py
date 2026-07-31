"""Match FLIR frame-temperature statistics to Noseboom UTC navigation."""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


def parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def georeference_temperature_records(
    records: Iterable[Mapping[str, Any]],
    noseboom_points: Iterable[Mapping[str, Any]],
    *,
    maximum_time_delta_seconds: float = 2.5,
) -> list[dict[str, Any]]:
    """Return frame statistics paired with the nearest valid navigation point."""
    navigation: list[tuple[datetime, Mapping[str, Any]]] = []
    for point in noseboom_points:
        timestamp = parse_utc(point.get("time"))
        latitude = _finite(point.get("lat"))
        longitude = _finite(point.get("lon"))
        if (
            timestamp is None
            or latitude is None
            or longitude is None
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            continue
        navigation.append((timestamp, point))
    navigation.sort(key=lambda item: item[0])
    navigation_times = [item[0] for item in navigation]
    if not navigation_times:
        return []

    matched: list[dict[str, Any]] = []
    for record in records:
        timestamp = parse_utc(
            record.get("timestamp.$date") or record.get("timestamp")
        )
        median = _finite(record.get("pixel_temperature.median_c"))
        mean = _finite(record.get("pixel_temperature.mean_c"))
        if timestamp is None or (median is None and mean is None):
            continue
        insertion = bisect_left(navigation_times, timestamp)
        candidates = [
            index
            for index in (insertion - 1, insertion)
            if 0 <= index < len(navigation)
        ]
        if not candidates:
            continue
        index = min(
            candidates,
            key=lambda value: abs(
                (navigation_times[value] - timestamp).total_seconds()
            ),
        )
        navigation_time, point = navigation[index]
        delta = abs((navigation_time - timestamp).total_seconds())
        if delta > maximum_time_delta_seconds:
            continue
        altitude = point.get("altitude_m")
        if altitude is None:
            altitude = point.get("height_m")
        matched.append(
            {
                "frame_id": str(
                    record.get("record_index_in_selected_scan")
                    or len(matched) + 1
                ),
                "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
                "noseboom_time_utc": navigation_time.isoformat().replace(
                    "+00:00", "Z"
                ),
                "time_delta_seconds": round(delta, 3),
                "latitude": float(point["lat"]),
                "longitude": float(point["lon"]),
                "altitude_m": _finite(altitude),
                "temperature_min_c": _finite(
                    record.get("pixel_temperature.min_c")
                ),
                "temperature_max_c": _finite(
                    record.get("pixel_temperature.max_c")
                ),
                "temperature_mean_c": mean,
                "temperature_median_c": median,
                "temperature_std_c": _finite(
                    record.get("pixel_temperature.std_c")
                ),
                "valid_pixel_count": _integer(
                    record.get("pixel_temperature.valid_pixel_count")
                ),
                "status": str(
                    record.get("calculated_temperature.status") or ""
                ),
            }
        )
    return matched


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _integer(value: Any) -> int | None:
    number = _finite(value)
    return int(number) if number is not None else None
