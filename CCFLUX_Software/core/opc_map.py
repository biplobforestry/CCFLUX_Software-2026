"""Place OPC bin-resolved concentrations on the flight track.

The OPC records concentration against its own clock and carries no position.
Noseboom is the navigation reference for the whole payload, so each OPC sample
is paired with the nearest Noseboom fix in time and takes that position. A
sample with no fix close enough in time is reported as unmatched rather than
placed at a guessed position - the map may only show where the airship
demonstrably was.
"""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

# Noseboom runs at 10 Hz and the OPC at roughly 1 Hz, so a fix is normally
# within a fraction of a second. Two seconds still describes the same place at
# airship speed, and anything beyond it means the navigation record has a gap.
DEFAULT_MAXIMUM_TIME_DELTA_SECONDS = 2.0
DEFAULT_POINT_LIMIT = 4000
BIN_COUNT = 24


def parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def navigation_index(
    points: Iterable[Mapping[str, Any]]
) -> tuple[list[datetime], list[Mapping[str, Any]]]:
    """Return Noseboom fixes with a usable time and position, in time order."""
    fixes: list[tuple[datetime, Mapping[str, Any]]] = []
    for point in points:
        timestamp = parse_utc(point.get("time"))
        latitude = _finite(point.get("lat"))
        longitude = _finite(point.get("lon"))
        if timestamp is None or latitude is None or longitude is None:
            continue
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        if latitude == 0 and longitude == 0:
            # A fix before the receiver locks reads exactly zero, which is a
            # real place in the Atlantic and never where the airship was.
            continue
        fixes.append((timestamp, point))
    fixes.sort(key=lambda item: item[0])
    return [item[0] for item in fixes], [item[1] for item in fixes]


def nearest_fix(
    times: Sequence[datetime],
    timestamp: datetime,
    maximum_delta_seconds: float,
) -> tuple[int, float] | None:
    """Return the index of the closest fix in time, and how far off it is."""
    if not times:
        return None
    insertion = bisect_left(times, timestamp)
    candidates = [
        index for index in (insertion - 1, insertion) if 0 <= index < len(times)
    ]
    if not candidates:
        return None
    index = min(
        candidates,
        key=lambda value: abs((times[value] - timestamp).total_seconds()),
    )
    delta = abs((times[index] - timestamp).total_seconds())
    return (index, delta) if delta <= maximum_delta_seconds else None


def _sample_positions(count: int, limit: int) -> range | list[int]:
    """Thin evenly, keeping the first and last so the track stays complete."""
    if count <= limit:
        return range(count)
    step = count / limit
    kept = sorted({int(index * step) for index in range(limit)} | {0, count - 1})
    return [index for index in kept if index < count]


def georeference_sensor(
    heatmap: Mapping[str, Any],
    navigation_times: Sequence[datetime],
    navigation_points: Sequence[Mapping[str, Any]],
    *,
    maximum_delta_seconds: float = DEFAULT_MAXIMUM_TIME_DELTA_SECONDS,
    point_limit: int = DEFAULT_POINT_LIMIT,
) -> dict[str, Any]:
    """Pair one sensor's bin-resolved samples with the flight track."""
    stamps = list(heatmap.get("time") or [])
    grid = [list(row or []) for row in (heatmap.get("z") or [])]
    bins = [int(value) for value in (heatmap.get("bin_index") or range(BIN_COUNT))]
    points: list[dict[str, Any]] = []
    unmatched = 0
    undated = 0
    for position in _sample_positions(len(stamps), point_limit):
        timestamp = parse_utc(stamps[position])
        if timestamp is None:
            undated += 1
            continue
        found = nearest_fix(navigation_times, timestamp, maximum_delta_seconds)
        if found is None:
            unmatched += 1
            continue
        index, delta = found
        fix = navigation_points[index]
        values = [
            _finite(row[position]) if position < len(row) else None for row in grid
        ]
        carried = [value for value in values if value is not None]
        altitude = fix.get("altitude_m")
        if altitude is None:
            altitude = fix.get("height_m")
        points.append(
            {
                "time": timestamp.isoformat().replace("+00:00", "Z"),
                "lat": float(fix["lat"]),
                "lon": float(fix["lon"]),
                "altitude_m": _finite(altitude),
                "delta_s": round(delta, 3),
                "bins": values,
                # The sum over bins is the number concentration the map shows
                # when no single size class is selected.
                "total": sum(carried) if carried else None,
            }
        )
    return {
        "points": points,
        "bin_index": bins,
        "matched_count": len(points),
        "unmatched_count": unmatched,
        "undated_count": undated,
        "sampled_from": len(stamps),
    }


def build_map_payload(
    opc_payload: Mapping[str, Any],
    noseboom_points: Iterable[Mapping[str, Any]],
    *,
    flight_id: str = "",
    maximum_delta_seconds: float = DEFAULT_MAXIMUM_TIME_DELTA_SECONDS,
    point_limit: int = DEFAULT_POINT_LIMIT,
) -> dict[str, Any]:
    """Return both OPC sensors placed on the Noseboom flight track."""
    times, fixes = navigation_index(noseboom_points)
    sensors: dict[str, Any] = {}
    for sensor_id, sensor in (opc_payload.get("sensors") or {}).items():
        placed = georeference_sensor(
            sensor.get("heatmap") or {},
            times,
            fixes,
            maximum_delta_seconds=maximum_delta_seconds,
            point_limit=point_limit,
        )
        placed["label"] = str(sensor.get("label") or sensor_id)
        sensors[sensor_id] = placed
    track = [
        {"lat": float(fix["lat"]), "lon": float(fix["lon"])}
        for index, fix in enumerate(fixes)
        if index in set(_sample_positions(len(fixes), point_limit))
    ]
    available = any(sensor["matched_count"] for sensor in sensors.values())
    return {
        "schema": "ccflux-opc-map-v1",
        "available": available,
        "flight_id": flight_id,
        "navigation": "Noseboom, nearest fix in time",
        "maximum_time_delta_seconds": maximum_delta_seconds,
        "navigation_fix_count": len(times),
        "sensors": sensors,
        "flight_track": track,
        "message": (
            ""
            if available
            else (
                "No OPC sample falls within "
                f"{maximum_delta_seconds:g} s of a Noseboom position fix."
                if times
                else "Noseboom carries no usable position fix, so the OPC "
                "samples cannot be placed on a map."
            )
        ),
    }
