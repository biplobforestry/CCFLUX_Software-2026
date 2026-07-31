"""Validated dashboard time-filter state; source timestamps remain immutable."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping


START_TRIM = timedelta(minutes=2)
END_TRIM = timedelta(minutes=1)
# These products are discrete captures or short spectral acquisition sessions.
# Applying the continuous-sensor edge trim can erase their entire valid range.
UNTRIMMED_INSTRUMENTS = frozenset({"sif", "flir", "gopro", "micasense"})


@dataclass(slots=True)
class InstrumentTimeSelection:
    instrument_id: str
    available_start: datetime | None
    available_end: datetime | None
    raw_start: datetime | None = None
    raw_end: datetime | None = None
    timezone_warnings: tuple[str, ...] = ()
    override_start: datetime | None = None
    override_end: datetime | None = None
    outside_selected_range: bool = False
    availability_percentage: float | None = None

    @property
    def effective_start(self) -> datetime | None:
        return self.override_start or self.available_start

    @property
    def effective_end(self) -> datetime | None:
        return self.override_end or self.available_end


@dataclass(slots=True)
class DashboardTimeState:
    detected_global_start: datetime | None = None
    detected_global_end: datetime | None = None
    common_overlap_start: datetime | None = None
    common_overlap_end: datetime | None = None
    selected_analysis_start: datetime | None = None
    selected_analysis_end: datetime | None = None
    display_timezone: str = "UTC"
    instruments: dict[str, InstrumentTimeSelection] = field(default_factory=dict)

    @classmethod
    def from_instrument_ranges(
        cls,
        ranges: Mapping[
            str, tuple[datetime | None, datetime | None, Iterable[str]]
        ],
        *,
        analysis_anchor_id: str | None = None,
    ) -> "DashboardTimeState":
        instruments: dict[str, InstrumentTimeSelection] = {}
        for instrument_id, (start, end, warnings) in ranges.items():
            normalized_start, normalized_end = _utc(start), _utc(end)
            messages = list(warnings)
            if (
                normalized_start is not None
                and normalized_end is not None
                and instrument_id not in UNTRIMMED_INSTRUMENTS
            ):
                trimmed_start = normalized_start + START_TRIM
                trimmed_end = normalized_end - END_TRIM
                if trimmed_start < trimmed_end:
                    normalized_start, normalized_end = trimmed_start, trimmed_end
                else:
                    normalized_start = normalized_end = None
                    messages.append(
                        "Dataset is too short after excluding the first 2 minutes "
                        "and final 1 minute."
                    )
            instruments[instrument_id] = InstrumentTimeSelection(
                instrument_id,
                normalized_start,
                normalized_end,
                _utc(start),
                _utc(end),
                tuple(dict.fromkeys(messages)),
            )
        valid = [
            item
            for item in instruments.values()
            if item.available_start is not None and item.available_end is not None
        ]
        anchor = instruments.get(analysis_anchor_id) if analysis_anchor_id else None
        if (
            anchor is not None
            and anchor.available_start is not None
            and anchor.available_end is not None
        ):
            global_start = anchor.available_start
            global_end = anchor.available_end
            overlap_candidates = [
                item
                for item in valid
                if item.available_end > global_start
                and item.available_start < global_end
            ]
        else:
            global_start = min(
                (item.available_start for item in valid), default=None
            )
            global_end = max((item.available_end for item in valid), default=None)
            overlap_candidates = valid
        overlap_start = max(
            (item.available_start for item in overlap_candidates), default=None
        )
        overlap_end = min(
            (item.available_end for item in overlap_candidates), default=None
        )
        if (
            overlap_start is None
            or overlap_end is None
            or overlap_start >= overlap_end
        ):
            overlap_start = overlap_end = None
        result = cls(
            detected_global_start=global_start,
            detected_global_end=global_end,
            common_overlap_start=overlap_start,
            common_overlap_end=overlap_end,
            selected_analysis_start=global_start,
            selected_analysis_end=global_end,
            instruments=instruments,
        )
        result._refresh_availability()
        return result

    @property
    def timezone_warnings(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                warning
                for instrument in self.instruments.values()
                for warning in instrument.timezone_warnings
                if "timezone" in warning.casefold() or "utc" in warning.casefold()
            )
        )

    def use_full_detected_interval(self) -> None:
        self._require_detected_range()
        self.set_selected_interval(
            self.detected_global_start, self.detected_global_end
        )

    def use_common_overlap(self) -> None:
        if self.common_overlap_start is None or self.common_overlap_end is None:
            raise ValueError("No common overlapping UTC interval is available")
        self.set_selected_interval(
            self.common_overlap_start, self.common_overlap_end
        )

    def reset_to_detected_limits(self) -> None:
        for instrument in self.instruments.values():
            instrument.override_start = None
            instrument.override_end = None
        self.use_full_detected_interval()

    def set_selected_interval(
        self, start: datetime | None, end: datetime | None
    ) -> None:
        self._require_detected_range()
        normalized_start, normalized_end = _validated_interval(start, end)
        if (
            normalized_end <= self.detected_global_start
            or normalized_start >= self.detected_global_end
        ):
            raise ValueError("Selected interval does not intersect available data")
        self.selected_analysis_start = normalized_start
        self.selected_analysis_end = normalized_end
        self._refresh_availability()

    def set_instrument_override(
        self,
        instrument_id: str,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        try:
            instrument = self.instruments[instrument_id]
        except KeyError as exc:
            raise ValueError(f"Unknown instrument ID: {instrument_id}") from exc
        if start is None and end is None:
            instrument.override_start = None
            instrument.override_end = None
            self._refresh_availability()
            return
        if instrument.available_start is None or instrument.available_end is None:
            raise ValueError(f"{instrument_id} has no confirmed UTC time range")
        normalized_start, normalized_end = _validated_interval(start, end)
        if (
            normalized_end <= instrument.available_start
            or normalized_start >= instrument.available_end
        ):
            raise ValueError(
                f"Override for {instrument_id} does not intersect available data"
            )
        if (
            self.selected_analysis_start is not None
            and self.selected_analysis_end is not None
            and (
                normalized_end <= self.selected_analysis_start
                or normalized_start >= self.selected_analysis_end
            )
        ):
            raise ValueError(
                f"Override for {instrument_id} does not intersect selected interval"
            )
        instrument.override_start = normalized_start
        instrument.override_end = normalized_end
        self._refresh_availability()

    def to_dict(self) -> dict[str, object]:
        return {
            "detected_global_start": _iso(self.detected_global_start),
            "detected_global_end": _iso(self.detected_global_end),
            "common_overlap_start": _iso(self.common_overlap_start),
            "common_overlap_end": _iso(self.common_overlap_end),
            "selected_analysis_start": _iso(self.selected_analysis_start),
            "selected_analysis_end": _iso(self.selected_analysis_end),
            "display_timezone": self.display_timezone,
            "timezone_warnings": list(self.timezone_warnings),
            "instruments": {
                key: {
                    "available_start": _iso(value.available_start),
                    "available_end": _iso(value.available_end),
                    "raw_start": _iso(value.raw_start),
                    "raw_end": _iso(value.raw_end),
                    "override_start": _iso(value.override_start),
                    "override_end": _iso(value.override_end),
                    "effective_start": _iso(value.effective_start),
                    "effective_end": _iso(value.effective_end),
                    "outside_selected_range": value.outside_selected_range,
                    "availability_percentage": value.availability_percentage,
                    "timezone_warnings": list(value.timezone_warnings),
                }
                for key, value in self.instruments.items()
            },
        }

    def _require_detected_range(self) -> None:
        if self.detected_global_start is None or self.detected_global_end is None:
            raise ValueError("No detected UTC interval is available")

    def _refresh_availability(self) -> None:
        selected_start = self.selected_analysis_start
        selected_end = self.selected_analysis_end
        for instrument in self.instruments.values():
            start, end = instrument.effective_start, instrument.effective_end
            if (
                start is None
                or end is None
                or selected_start is None
                or selected_end is None
            ):
                instrument.outside_selected_range = start is None or end is None
                instrument.availability_percentage = None
                continue
            overlap_start = max(start, selected_start)
            overlap_end = min(end, selected_end)
            instrument.outside_selected_range = overlap_start >= overlap_end
            selected_seconds = (selected_end - selected_start).total_seconds()
            if selected_seconds <= 0:
                instrument.outside_selected_range = True
                instrument.availability_percentage = None
                continue
            overlap_seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
            instrument.availability_percentage = round(
                100.0 * overlap_seconds / selected_seconds, 1
            )


def parse_dashboard_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Time values must be non-empty ISO-8601 strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Time values must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validated_interval(
    start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime]:
    if start is None or end is None:
        raise ValueError("Both start and end times are required")
    normalized_start, normalized_end = _utc(start), _utc(end)
    if normalized_start >= normalized_end:
        raise ValueError("Start time must be earlier than end time")
    return normalized_start, normalized_end


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("Time values must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
