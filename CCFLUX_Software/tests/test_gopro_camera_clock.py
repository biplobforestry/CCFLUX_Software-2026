"""The GoPro camera clock is measured and declared, not assumed.

GoPro EXIF carries no OffsetTime or OffsetTimeOriginal, so nothing in the file
says what its clock is set to. CC-FLUX assumed Europe/Berlin for every campaign.
On Flight_CCT0803 that was wrong by two hours: the GoPro clock was UTC, and
subtracting two hours moved every frame before the flight started.

The other gondola cameras record UTC and are switched on with the GoPro, so the
offset can be measured against them and shown to the operator to confirm.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.scan_backend import (
    GOPRO_RECORD_CLOCK_TIMEZONES,
    DashboardScanBackend,
)
from core.camera_clock import (
    CONFIDENT_ALIGNMENT_SECONDS,
    flight_days,
    measure_camera_clock_offset,
)
from core.gopro_georeference import camera_local_to_utc
from core.logging_manager import ProcessingLogManager

# Flight_CCT0803 as measured: the GoPro card starts at 11:21:32 on its own
# clock, and MicaSense - which records UTC - starts at 11:21:37.
REFERENCE_START = datetime(2026, 8, 3, 11, 21, 37)
REFERENCE_END = datetime(2026, 8, 3, 15, 51, 35)


def _reference_times(count=40):
    step = (REFERENCE_END - REFERENCE_START) / max(1, count - 1)
    return [REFERENCE_START + step * index for index in range(count)]


def _camera_times(first, count=40, spacing_seconds=5):
    return [first + timedelta(seconds=spacing_seconds * i) for i in range(count)]


class TestChoices:
    def test_utc_and_cest_are_offered(self):
        assert set(GOPRO_RECORD_CLOCK_TIMEZONES) == {"utc", "cest"}

    def test_cest_is_two_hours_ahead(self):
        assert GOPRO_RECORD_CLOCK_TIMEZONES["cest"]["offset_seconds"] == 7200
        assert GOPRO_RECORD_CLOCK_TIMEZONES["utc"]["offset_seconds"] == 0


class TestMeasurement:
    def test_a_utc_camera_clock_is_identified_as_utc(self):
        """The Flight_CCT0803 case: the clock was UTC, not campaign local."""
        measurement = measure_camera_clock_offset(
            _camera_times(datetime(2026, 8, 3, 11, 21, 32)),
            _reference_times(),
            reference_instrument="MicaSense",
        )
        assert measurement.best_key == "utc"
        assert measurement.confident is True

    def test_a_cest_camera_clock_is_identified_as_cest(self):
        """A camera really left on campaign local time must still be found."""
        measurement = measure_camera_clock_offset(
            _camera_times(datetime(2026, 8, 3, 13, 21, 32)),
            _reference_times(),
            reference_instrument="MicaSense",
        )
        assert measurement.best_key == "cest"
        assert measurement.confident is True

    def test_the_losing_candidate_places_nothing_on_the_flight(self):
        measurement = measure_camera_clock_offset(
            _camera_times(datetime(2026, 8, 3, 11, 21, 32)),
            _reference_times(),
            reference_instrument="MicaSense",
        )
        cest = next(c for c in measurement.candidates if c.key == "cest")
        assert cest.frames_inside_reference == 0

    def test_the_evidence_is_reported_for_every_candidate(self):
        measurement = measure_camera_clock_offset(
            _camera_times(datetime(2026, 8, 3, 11, 21, 32)),
            _reference_times(),
            reference_instrument="MicaSense",
        )
        assert len(measurement.candidates) == 2
        for candidate in measurement.candidates:
            assert candidate.frames_on_reference_day > 0
            assert candidate.seconds_to_reference_start is not None
        assert "MicaSense" in measurement.reason

    def test_only_the_reference_flight_day_is_judged(self):
        """A card holding two flights must not be judged on the other one."""
        camera = (
            _camera_times(datetime(2026, 8, 3, 11, 21, 32), count=10)
            + _camera_times(datetime(2026, 8, 4, 12, 16, 10), count=200)
        )
        measurement = measure_camera_clock_offset(
            camera, _reference_times(), reference_instrument="MicaSense"
        )
        assert measurement.reference_day == REFERENCE_START.date()
        assert measurement.best_key == "utc"
        utc = next(c for c in measurement.candidates if c.key == "utc")
        assert utc.frames_on_reference_day == 10

    def test_no_reference_means_no_measurement_and_no_guess(self):
        measurement = measure_camera_clock_offset(
            _camera_times(datetime(2026, 8, 3, 11, 21, 32)), []
        )
        assert measurement.best_key is None
        assert measurement.confident is False
        assert "recording UTC" in measurement.reason

    def test_no_camera_stamps_means_no_measurement(self):
        measurement = measure_camera_clock_offset([], _reference_times())
        assert measurement.best_key is None
        assert measurement.confident is False

    def test_a_card_from_another_flight_is_not_forced_onto_this_one(self):
        """Neither candidate fits, so nothing is suggested."""
        measurement = measure_camera_clock_offset(
            _camera_times(datetime(2026, 7, 1, 9, 0, 0)),
            _reference_times(),
            reference_instrument="MicaSense",
        )
        assert measurement.best_key is None
        assert "different flight" in measurement.reason

    def test_a_loose_alignment_is_not_reported_as_confident(self):
        offset = timedelta(seconds=CONFIDENT_ALIGNMENT_SECONDS + 600)
        measurement = measure_camera_clock_offset(
            _camera_times(REFERENCE_START + offset),
            _reference_times(),
            reference_instrument="MicaSense",
        )
        assert measurement.best_key == "utc"
        assert measurement.confident is False


class TestFlightDays:
    def test_the_busiest_day_comes_first(self):
        values = (
            _camera_times(datetime(2026, 8, 3, 11, 0), count=5)
            + _camera_times(datetime(2026, 8, 4, 11, 0), count=50)
        )
        days = flight_days(values)
        assert days[0] == (datetime(2026, 8, 4).date(), 50)
        assert days[1] == (datetime(2026, 8, 3).date(), 5)


class TestConversion:
    def test_a_declared_utc_clock_is_not_shifted(self):
        assert camera_local_to_utc(
            datetime(2026, 8, 3, 11, 21, 32), 0
        ) == datetime(2026, 8, 3, 11, 21, 32, tzinfo=timezone.utc)

    def test_a_declared_cest_clock_loses_two_hours(self):
        assert camera_local_to_utc(
            datetime(2026, 8, 3, 13, 21, 32), 7200
        ) == datetime(2026, 8, 3, 11, 21, 32, tzinfo=timezone.utc)

    def test_an_undeclared_clock_falls_back_to_the_campaign_zone(self):
        """Unchanged behaviour for a flight nobody has answered for."""
        assert camera_local_to_utc(
            datetime(2026, 8, 3, 13, 21, 32)
        ) == datetime(2026, 8, 3, 11, 21, 32, tzinfo=timezone.utc)

    def test_winter_still_resolves_through_the_named_zone(self):
        assert camera_local_to_utc(
            datetime(2026, 1, 15, 12)
        ) == datetime(2026, 1, 15, 11, tzinfo=timezone.utc)


class TestDeclaration:
    @pytest.fixture()
    def backend(self, tmp_path):
        return DashboardScanBackend(
            tmp_path,
            logger=ProcessingLogManager(tmp_path / "application-log.jsonl"),
        )

    def test_an_undetected_gopro_is_not_asked_about(self, backend):
        assert backend.gopro_timezone_prompt()["required"] is False

    def test_the_offset_is_none_until_declared(self, backend):
        assert backend._gopro_record_clock_offset_seconds() is None

    def test_declaring_utc_gives_a_zero_offset(self, backend):
        backend.set_gopro_timezone("utc")
        assert backend._gopro_record_clock_offset_seconds() == 0.0

    def test_declaring_cest_gives_two_hours(self, backend):
        backend.set_gopro_timezone("cest")
        assert backend._gopro_record_clock_offset_seconds() == 7200.0

    def test_a_manual_offset_is_accepted(self, backend):
        """The operator can decline both offered answers."""
        backend.set_gopro_timezone("manual", 3600)
        assert backend._gopro_record_clock_offset_seconds() == 3600.0
        assert backend.gopro_timezone_prompt()["chosen"] == "manual"

    def test_a_manual_offset_needs_a_number(self, backend):
        with pytest.raises(ValueError, match="number of seconds"):
            backend.set_gopro_timezone("manual", None)

    def test_a_manual_offset_beyond_a_day_is_refused(self, backend):
        with pytest.raises(ValueError, match="within a day"):
            backend.set_gopro_timezone("manual", 200000)

    def test_an_unknown_choice_is_refused(self, backend):
        with pytest.raises(ValueError, match="must be one of"):
            backend.set_gopro_timezone("bst")

    def test_the_extractor_is_built_with_the_declaration(self, backend):
        backend.set_gopro_timezone("utc")
        extractor = backend._timestamp_extractor()
        assert extractor._gopro_record_clock_offset_seconds == 0.0

    def test_the_declaration_is_written_to_the_open_project(self, backend, tmp_path):
        from core.flight_project import FlightProject

        raw = tmp_path / "raw"
        raw.mkdir()
        backend._flight_project = FlightProject(
            flight_id="Flight_CCT0803",
            flight_folder_path=raw,
            output_folder_path=tmp_path / "out",
        )
        backend.set_gopro_timezone("utc")
        saved = backend._flight_project.instrument_options["gopro"]
        assert saved["record_clock_timezone"] == "utc"

    def test_the_label_says_when_nothing_was_declared(self, backend):
        assert "assumed" in backend._gopro_camera_timezone_label()

    def test_the_label_names_the_declaration_once_made(self, backend):
        backend.set_gopro_timezone("utc")
        assert backend._gopro_camera_timezone_label() == (
            "declared camera clock UTC"
        )

    def test_a_new_flight_is_asked_again(self, backend, tmp_path):
        """The clock is a property of one flight, not of the session.

        This camera is UTC on some flights and campaign local on others, so an
        answer given for one must never be carried silently into the next.
        """
        first, second = tmp_path / "Flight_A", tmp_path / "Flight_B"
        first.mkdir()
        second.mkdir()
        backend.start_scan(first, include_camera=False)
        backend._worker.join(timeout=10)
        backend.set_gopro_timezone("utc")
        assert backend._gopro_record_clock_offset_seconds() == 0.0

        backend.start_scan(second, include_camera=False)
        backend._worker.join(timeout=10)
        assert backend._gopro_options["record_clock_timezone"] is None
        assert backend._gopro_record_clock_offset_seconds() is None

    def test_rescanning_the_same_flight_keeps_the_answer(self, backend, tmp_path):
        raw = tmp_path / "Flight_A"
        raw.mkdir()
        backend.start_scan(raw, include_camera=False)
        backend._worker.join(timeout=10)
        backend.set_gopro_timezone("cest")
        backend.start_scan(raw, include_camera=False)
        backend._worker.join(timeout=10)
        assert backend._gopro_record_clock_offset_seconds() == 7200.0


class TestCameraProductsCanBeSelected:
    """The dialog was disabled when the cameras lived in the Flight Folder.

    Flight_CCT0803 keeps GoPro, FLIR and MicaSense inside the Flight Folder, so
    no separate Camera Folder scan ever runs. Requiring one left every camera
    product unselectable and told the operator to add a folder that would only
    rescan the same files.
    """

    def _backend(self, tmp_path):
        return DashboardScanBackend(
            tmp_path,
            logger=ProcessingLogManager(tmp_path / "application-log.jsonl"),
        )

    def test_an_idle_session_is_not_ready(self, tmp_path):
        backend = self._backend(tmp_path)
        assert backend.snapshot()["camera_scan_ready"] is False

    def test_a_finished_flight_scan_that_found_cameras_is_ready(self, tmp_path):
        from core.enums import DetectionStatus

        backend = self._backend(tmp_path)
        backend._scan_channels["flight"]["phase"] = "complete"
        backend._instruments["gopro"].detection_status = DetectionStatus.READY
        assert backend.snapshot()["camera_scan_ready"] is True

    def test_a_finished_flight_scan_with_no_camera_is_not_ready(self, tmp_path):
        backend = self._backend(tmp_path)
        backend._scan_channels["flight"]["phase"] = "complete"
        assert backend.snapshot()["camera_scan_ready"] is False

    def test_a_failed_flight_scan_is_not_ready(self, tmp_path):
        from core.enums import DetectionStatus

        backend = self._backend(tmp_path)
        backend._scan_channels["flight"]["phase"] = "complete"
        backend._scan_channels["flight"]["error"] = "disk went away"
        backend._instruments["gopro"].detection_status = DetectionStatus.READY
        assert backend.snapshot()["camera_scan_ready"] is False


class TestPrompt:
    script = (
        Path(__file__).resolve().parents[1] / "app" / "assets" / "dashboard.js"
    ).read_text(encoding="utf-8")

    def test_it_is_asked_when_the_scan_finishes(self):
        assert "await askGoproTimezone();" in self.script

    def test_it_is_skipped_when_the_flight_has_no_gopro(self):
        block = self.script[self.script.index("async function askGoproTimezone"):]
        assert "if (!prompt.required) return;" in block[:400]

    def test_the_measurement_is_shown_as_evidence(self):
        block = self.script[self.script.index("async function askGoproTimezone"):]
        assert "measurement.reason" in block
        assert "frames_inside_reference" in block

    def test_only_a_confident_measurement_is_preselected(self):
        """The clock varies between flights, so a half-aligned guess must not
        sit pre-ticked inviting the operator to accept it unread."""
        block = self.script[self.script.index("async function askGoproTimezone"):]
        assert "measurement.confident ? measurement.best_key : null" in block

    def test_the_operator_is_told_the_setting_varies_between_flights(self):
        block = self.script[self.script.index("async function askGoproTimezone"):]
        assert "set to UTC on some flights" in block

    def test_an_inconclusive_measurement_says_so(self):
        block = self.script[self.script.index("async function askGoproTimezone"):]
        assert "not conclusive" in block

    def test_the_operator_can_decline_and_enter_an_offset(self):
        block = self.script[self.script.index("async function askGoproTimezone"):]
        assert "goproManualOffset" in block
        assert "manual_offset_seconds" in block

    def test_the_flag_latches_only_after_the_server_accepts(self):
        block = self.script[self.script.index("async function askGoproTimezone"):]
        post = block.index("'/api/gopro/timezone', { method: 'POST'")
        assert "goproTimezoneAsked = true;" in block[post:post + 300]

    def test_it_is_cleared_for_each_scan(self):
        polling = self.script.index("function startPolling()")
        assert "goproTimezoneAsked = false;" in self.script[polling:polling + 500]

    def test_the_card_is_refreshed_once_the_answer_is_stored(self):
        block = self.script[self.script.index("async function askGoproTimezone"):]
        post = block.index("'/api/gopro/timezone', { method: 'POST'")
        assert "await pollScan();" in block[post:post + 500]

    def test_the_two_flight_days_on_the_card_are_named(self):
        block = self.script[self.script.index("async function askGoproTimezone"):]
        assert "camera_days" in block


class TestRoutes:
    server = (
        Path(__file__).resolve().parents[1] / "app" / "server.py"
    ).read_text(encoding="utf-8")

    def test_the_prompt_and_the_answer_are_served(self):
        assert self.server.count('path == "/api/gopro/timezone"') == 2

    def test_the_manual_offset_reaches_the_backend(self):
        assert 'body.get("manual_offset_seconds")' in self.server


class TestProcessingUsesTheSameDeclaration:
    backend_source = (
        Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")

    def test_the_adapter_is_given_the_declared_offset(self):
        start = self.backend_source.index("adapter = GoProLevel1Adapter(")
        block = self.backend_source[start:start + 900]
        assert "record_clock_offset_seconds=self._gopro_record_clock_offset_seconds()" in block

    def test_the_adapter_converts_with_it(self):
        adapter = (
            Path(__file__).resolve().parents[1]
            / "instruments" / "gopro" / "adapter.py"
        ).read_text(encoding="utf-8")
        assert "camera_local_to_utc(local_time, offset_seconds)" in adapter
        assert "self.record_clock_offset_seconds" in adapter

    def test_the_card_no_longer_hard_codes_the_zone(self):
        assert '"camera_timezone": "Europe/Berlin (CET/CEST)"' not in self.backend_source
        assert "Time corrected during detection: Europe/Berlin → UTC" not in self.backend_source

    def test_the_raw_clock_reader_applies_no_offset(self):
        """Measuring an offset from values that already have one is circular."""
        extraction = (
            Path(__file__).resolve().parents[1] / "core" / "time_extraction.py"
        ).read_text(encoding="utf-8")
        block = extraction[extraction.index("def read_gopro_camera_clock_times"):]
        assert "no offset applied" in block[:600]
        assert "circular" in block[:900]
