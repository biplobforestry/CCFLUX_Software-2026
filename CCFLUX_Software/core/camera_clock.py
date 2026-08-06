"""Measure what a camera's clock is set to, against one that records UTC.

A GoPro writes EXIF acquisition times with no timezone field: no OffsetTime, no
OffsetTimeOriginal, nothing in the file that says how far its clock is from UTC.
CC-FLUX used to assume Europe/Berlin for every campaign, which is a guess about
hardware rather than a reading of it. On Flight_CCT0803 the guess was wrong by
two hours, and it moved every GoPro frame two hours before the flight.

The other cameras on the gondola do record UTC, and they were switched on with
the GoPro. So the offset can be measured rather than assumed: shift the camera's
stamps by each candidate offset and see which shift lands them on the reference
camera's flight. On Flight_CCT0803 the UTC candidate puts 187 of 188 frames
inside the MicaSense window with the first frame 5 s away from the first
capture; the Europe/Berlin candidate puts none of them inside and is 7,205 s
out. That is not a close call, and it is evidence an operator can check.

The measurement is offered to the operator, never applied silently. A campaign
can always be the case the measurement gets wrong - one camera left running
overnight, a reference delivery from the wrong day - and a timestamp that
decides where every frame lands is not something to infer without being told.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Sequence

# What a campaign camera clock is plausibly set to. Central European Summer Time
# is the campaign's local time; UTC is what a correctly configured camera holds.
# CET (+1) is not offered: the campaign flies in summer, and offering a winter
# offset invites picking it by accident.
CAMERA_RECORD_CLOCK_TIMEZONES: Mapping[str, Mapping[str, object]] = {
    "utc": {"label": "UTC", "offset_seconds": 0},
    "cest": {"label": "CEST (UTC+2)", "offset_seconds": 7200},
}

# Below this the two cameras were plainly started together and the candidate is
# the answer. Two hours of ambiguity is what is being resolved, so the threshold
# only has to be far inside that.
CONFIDENT_ALIGNMENT_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ClockCandidate:
    """One hypothesis about the camera clock, and how well it fits."""

    key: str
    label: str
    offset_seconds: float
    frames_on_reference_day: int
    frames_inside_reference: int
    seconds_to_reference_start: float | None

    @property
    def inside_fraction(self) -> float:
        if not self.frames_on_reference_day:
            return 0.0
        return self.frames_inside_reference / self.frames_on_reference_day

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "offset_seconds": self.offset_seconds,
            "frames_on_reference_day": self.frames_on_reference_day,
            "frames_inside_reference": self.frames_inside_reference,
            "inside_fraction": round(self.inside_fraction, 4),
            "seconds_to_reference_start": (
                None
                if self.seconds_to_reference_start is None
                else round(self.seconds_to_reference_start, 1)
            ),
        }


@dataclass(frozen=True, slots=True)
class ClockMeasurement:
    """What comparing the camera against a UTC reference showed."""

    reference_instrument: str | None
    reference_start: datetime | None
    reference_end: datetime | None
    reference_day: date | None
    candidates: tuple[ClockCandidate, ...]
    best_key: str | None
    confident: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reference_instrument": self.reference_instrument,
            "reference_start": (
                self.reference_start.isoformat() if self.reference_start else None
            ),
            "reference_end": (
                self.reference_end.isoformat() if self.reference_end else None
            ),
            "reference_day": self.reference_day.isoformat() if self.reference_day else None,
            "candidates": [item.to_dict() for item in self.candidates],
            "best_key": self.best_key,
            "confident": self.confident,
            "reason": self.reason,
        }


def _aware(value: datetime) -> datetime:
    """Naive camera stamps are compared as though already in the target zone."""
    return value


def measure_camera_clock_offset(
    camera_times: Sequence[datetime],
    reference_times: Sequence[datetime],
    *,
    reference_instrument: str | None = None,
    candidates: Mapping[str, Mapping[str, object]] | None = None,
) -> ClockMeasurement:
    """Which candidate offset puts the camera on the reference camera's flight.

    ``camera_times`` are the camera's own stamps, exactly as recorded, with no
    offset applied. ``reference_times`` are UTC. Both may be naive; they are
    compared as wall-clock values, which is what the question is about.

    Only the reference camera's busiest day is used. A card holding two flights
    - Flight_CCT0803's GoPro holds 188 frames from 3 August and 1,911 from the
    4th - would otherwise be judged against a reference that only flew one of
    them, and the other day's frames would count against every candidate.
    """
    table = candidates or CAMERA_RECORD_CLOCK_TIMEZONES
    camera = sorted(value for value in camera_times if value is not None)
    reference = sorted(value for value in reference_times if value is not None)
    if not camera:
        return ClockMeasurement(
            None, None, None, None, (), None, False,
            "The camera delivered no readable acquisition time.",
        )
    if not reference:
        return ClockMeasurement(
            None, None, None, None, (), None, False,
            "No instrument recording UTC was found for this flight, so the "
            "camera clock cannot be measured against anything.",
        )

    day_counts: dict[date, int] = {}
    for value in reference:
        day_counts[value.date()] = day_counts.get(value.date(), 0) + 1
    reference_day = max(day_counts, key=lambda key: (day_counts[key], key))
    same_day = [value for value in reference if value.date() == reference_day]
    reference_start, reference_end = same_day[0], same_day[-1]

    measured: list[ClockCandidate] = []
    for key, entry in table.items():
        offset = float(entry["offset_seconds"])
        shifted = [value - timedelta(seconds=offset) for value in camera]
        on_day = [value for value in shifted if value.date() == reference_day]
        inside = sum(
            1 for value in on_day if reference_start <= value <= reference_end
        )
        gap = (
            (min(on_day) - reference_start).total_seconds() if on_day else None
        )
        measured.append(
            ClockCandidate(
                key=str(key),
                label=str(entry["label"]),
                offset_seconds=offset,
                frames_on_reference_day=len(on_day),
                frames_inside_reference=inside,
                seconds_to_reference_start=gap,
            )
        )

    # The payload is powered on once, so the two cameras start together: on
    # Flight_CCT0803 the GoPro's first frame is 5 s from MicaSense's first
    # capture read as UTC, and 7,205 s from it read as CEST. How closely the two
    # recordings *start* is therefore the discriminator, not how many frames land
    # inside the window - a short camera burst sits inside a long reference
    # window under either candidate, and counting frames then picks the wrong one
    # by a single boundary sample. A candidate that places nothing on the
    # reference day is still excluded outright.
    ranked = sorted(
        measured,
        key=lambda item: (
            item.frames_inside_reference == 0,
            abs(item.seconds_to_reference_start)
            if item.seconds_to_reference_start is not None
            else float("inf"),
        ),
    )
    best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    if not best.frames_inside_reference:
        return ClockMeasurement(
            reference_instrument, reference_start, reference_end, reference_day,
            tuple(measured), None, False,
            "No candidate timezone puts any camera frame inside the "
            f"{reference_instrument or 'reference'} recording window. The card "
            "may hold a different flight than the one being processed.",
        )
    # Confident when exactly one candidate puts the two power-ons within a few
    # minutes of each other. Two candidates that both do would be ambiguous, and
    # none that does means the cameras were not started together.
    runner_up_gap = (
        abs(runner_up.seconds_to_reference_start)
        if runner_up is not None and runner_up.seconds_to_reference_start is not None
        else float("inf")
    )
    confident = bool(
        best.seconds_to_reference_start is not None
        and abs(best.seconds_to_reference_start) <= CONFIDENT_ALIGNMENT_SECONDS
        and runner_up_gap > CONFIDENT_ALIGNMENT_SECONDS
    )
    reference_name = reference_instrument or "the reference camera"
    reason = (
        f"Read as {best.label}, the camera starts "
        f"{abs(best.seconds_to_reference_start):.0f} s from {reference_name} on "
        f"{reference_day}, and {best.frames_inside_reference} of "
        f"{best.frames_on_reference_day} frame(s) fall inside its window "
        f"({reference_start:%H:%M:%S}-{reference_end:%H:%M:%S} UTC)"
    )
    if runner_up is not None:
        reason += (
            f". Read as {runner_up.label}, it starts "
            + (
                f"{abs(runner_up.seconds_to_reference_start):.0f} s away"
                if runner_up.seconds_to_reference_start is not None
                else "on another day"
            )
            + f" with {runner_up.frames_inside_reference} inside"
        )
    return ClockMeasurement(
        reference_instrument, reference_start, reference_end, reference_day,
        tuple(measured), best.key, confident, reason + ".",
    )


def flight_days(values: Iterable[datetime]) -> list[tuple[date, int]]:
    """How many stamps fall on each day, most frames first.

    A camera card is not emptied between flights. Reporting the days it spans
    is what stops an operator processing a whole card as one flight: only 188 of
    Flight_CCT0803's 2,099 GoPro frames belong to that flight.
    """
    counts: dict[date, int] = {}
    for value in values:
        if value is None:
            continue
        counts[value.date()] = counts.get(value.date(), 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
