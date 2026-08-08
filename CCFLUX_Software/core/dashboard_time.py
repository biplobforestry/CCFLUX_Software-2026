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

# The remote-sensing instruments are scanned and selected on their own, against
# their own coverage, so they take no part in the flight-instrument global
# minimum and maximum shown on the dashboard.
CAMERA_INSTRUMENTS = frozenset({"flir", "gopro", "micasense"})

# Instruments whose per-file coverage does not speak for the whole delivery, so
# the stretches between what they report are not gaps in the recording.
SAMPLED_COVERAGE_INSTRUMENTS = frozenset({"flir", "gopro", "micasense"})

# Instruments whose recorded window itself is an estimate. A separate question
# from the one above: MicaSense's per-capture instants still say nothing about
# the stretches between them, but its first and last capture are now read from
# every archive rather than from a sample of 147, so the window it reports is
# what the camera recorded. FLIR is read from the two edges of a JSON export and
# GoPro from a bounded sample, so theirs remain estimates.
ESTIMATED_WINDOW_INSTRUMENTS = frozenset({"flir", "gopro"})


def recorded_coverage_segments(
    instrument_id: str,
    segments: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Per-file coverage, but only where it speaks for the whole delivery.

    A camera's timestamps are read from a bounded sample of its files and a
    FLIR export from its two edges, so the stretches missing from what they
    report are not gaps in the recording. Reading them as gaps took MicaSense
    out of the selectable jobs for an interval it had in fact been capturing.
    """
    if instrument_id in SAMPLED_COVERAGE_INSTRUMENTS:
        return []
    return [(start, end) for start, end in segments]


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
    # Whether that percentage was measured or estimated. A camera's window comes
    # from a bounded sample of its deliveries - 147 of MicaSense's 9 999 - so it
    # is the extent of what was read, not of what was recorded. Shown as a bare
    # figure it reads as "a third of your flight is missing", which on
    # Flight_CC0807 was not true: processing re-reads every file and found the
    # captures the sample had not been given. The distinction travels with the
    # number so the page can say which it is.
    coverage_is_estimated: bool = False
    # The intervals the source files actually cover. Empty means the coverage
    # was never measured per file, and the envelope below is all there is.
    coverage_segments: tuple[tuple[datetime, datetime], ...] = ()

    @property
    def effective_start(self) -> datetime | None:
        return self.override_start or self.available_start

    @property
    def effective_end(self) -> datetime | None:
        return self.override_end or self.available_end

    def intersects(self, start: datetime, end: datetime) -> bool:
        """Whether any recorded coverage falls inside the interval.

        A capture is an instant, so the comparison is closed at both ends: a
        single MicaSense frame taken inside the window counts as coverage.
        """
        if not self.coverage_segments:
            first, last = self.effective_start, self.effective_end
            return first is not None and last is not None and first <= end and last >= start
        return any(
            segment_start <= end and segment_end >= start
            for segment_start, segment_end in self.coverage_segments
        )

    def covered_seconds_within(self, start: datetime, end: datetime) -> float:
        """Seconds of real coverage inside the interval, gaps excluded."""
        window_start = start
        window_end = end
        if self.effective_start is not None:
            window_start = max(window_start, self.effective_start)
        if self.effective_end is not None:
            window_end = min(window_end, self.effective_end)
        if window_start >= window_end:
            return 0.0
        if not self.coverage_segments:
            return (window_end - window_start).total_seconds()
        total = 0.0
        for segment_start, segment_end in self.coverage_segments:
            overlap_start = max(segment_start, window_start)
            overlap_end = min(segment_end, window_end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds()
        return total


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
        coverage_segments: Mapping[
            str, Iterable[tuple[datetime, datetime]]
        ] | None = None,
    ) -> "DashboardTimeState":
        segments_by_instrument = dict(coverage_segments or {})
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
                coverage_segments=tuple(
                    (utc_start, utc_end)
                    for utc_start, utc_end in (
                        (_utc(first), _utc(last))
                        for first, last in segments_by_instrument.get(instrument_id, ())
                    )
                    if utc_start is not None and utc_end is not None
                ),
                coverage_is_estimated=instrument_id in ESTIMATED_WINDOW_INSTRUMENTS,
            )
        valid = [
            item
            for item in instruments.values()
            if item.available_start is not None
            and item.available_end is not None
            and item.instrument_id not in CAMERA_INSTRUMENTS
        ]
        # The detected global minimum and maximum are the envelope of every
        # flight instrument that reported coverage - the earliest start any of
        # them saw and the latest end. It used to be taken from the Noseboom
        # alone whenever an anchor was named, which reported the anchor's own
        # window as though it were the campaign's: on Flight_2707 that showed
        # 05:21 - 10:20 while MIRO actually covered 26 Jul 00:00 - 27 Jul 17:03.
        global_start = min((item.available_start for item in valid), default=None)
        global_end = max((item.available_end for item in valid), default=None)
        # The anchor still decides which instruments count towards the common
        # overlap, because an instrument that does not meet the navigation
        # reference cannot be analysed against it.
        anchor = instruments.get(analysis_anchor_id) if analysis_anchor_id else None
        if (
            anchor is not None
            and anchor.available_start is not None
            and anchor.available_end is not None
        ):
            overlap_candidates = [
                item
                for item in valid
                if item.available_end > anchor.available_start
                and item.available_start < anchor.available_end
            ]
        else:
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
                    "coverage_is_estimated": value.coverage_is_estimated,
                    "timezone_warnings": list(value.timezone_warnings),
                    "coverage_segments": [
                        [_iso(first), _iso(last)]
                        for first, last in value.coverage_segments
                    ],
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
            envelope_percentage = round(100.0 * overlap_seconds / selected_seconds, 1)
            if not instrument.coverage_segments:
                instrument.availability_percentage = envelope_percentage
                continue
            # The envelope spans the gaps between source files. SIF ran from
            # 07:19 to 16:54 but recorded nothing between 11:05 and 13:21, and a
            # selection inside that gap was offered as fully available, so
            # processing accepted it and then failed with no file covering it.
            if not instrument.intersects(selected_start, selected_end):
                instrument.outside_selected_range = True
                instrument.availability_percentage = 0.0
                continue
            covered = instrument.covered_seconds_within(selected_start, selected_end)
            # A camera's coverage is a set of instants, so measuring it as a
            # duration reads as nothing at all; the envelope is what it has.
            instrument.availability_percentage = (
                round(100.0 * covered / selected_seconds, 1)
                if covered > 0
                else envelope_percentage
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
