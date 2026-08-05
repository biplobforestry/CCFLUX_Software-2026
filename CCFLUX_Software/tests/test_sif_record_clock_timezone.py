"""The operator declares what the AirFloX record clock is set to.

FLOX and FULL write campaign local time, and their GPS often never locks, so
nothing in the files says how far that is from UTC. Deriving it from an
unlocked receiver shifted a whole flight by two hours; the declaration is now
the only source, and it is asked for as soon as the scan finds SIF.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "instruments" / "sif" / "legacy"))
import airflox_sif_automation as afx  # noqa: E402

from app.scan_backend import SIF_RECORD_CLOCK_TIMEZONES  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"


class TestChoices:
    def test_utc_and_cest_are_offered(self):
        assert set(SIF_RECORD_CLOCK_TIMEZONES) == {"utc", "cest"}

    def test_cest_is_two_hours_ahead_of_utc(self):
        assert SIF_RECORD_CLOCK_TIMEZONES["cest"]["offset_seconds"] == 7200
        assert SIF_RECORD_CLOCK_TIMEZONES["utc"]["offset_seconds"] == 0

    def test_the_label_names_the_offset(self):
        """CET is +1 in winter; the campaign clock is CEST, so say so."""
        assert SIF_RECORD_CLOCK_TIMEZONES["cest"]["label"] == "CEST (UTC+2)"


class TestConversion:
    @pytest.mark.parametrize("key,recorded,expected", [
        ("cest", 10, 8),
        ("utc", 10, 10),
    ])
    def test_a_recorded_time_becomes_utc(self, key, recorded, expected):
        offset = SIF_RECORD_CLOCK_TIMEZONES[key]["offset_seconds"]
        converted = afx._apply_record_clock_offset(
            [datetime(2026, 8, 4, recorded, 30)], offset
        )
        assert converted[0].hour == expected
        assert converted[0].minute == 30


class TestDeclarationIsAuthoritative:
    source = Path(afx.__file__).read_text(encoding="utf-8")

    def test_the_reader_holds_the_declaration(self):
        assert "RECORD_CLOCK_TIMEZONE" in self.source
        assert afx.RECORD_CLOCK_TIMEZONE.keys() >= {"offset_seconds", "label"}

    def test_it_is_used_before_any_measured_hint(self):
        block = self.source[self.source.index("if _gps_is_unusable("):]
        declared = block.index("RECORD_CLOCK_TIMEZONE.get('offset_seconds')")
        hint = block.index("RECORD_CLOCK_OFFSET_HINT.get('seconds')")
        assert declared < hint

    def test_an_undeclared_flight_falls_back_as_before(self):
        assert "declared is not None" in self.source


class TestPrompt:
    script = (ASSETS / "dashboard.js").read_text(encoding="utf-8")

    def test_it_is_asked_when_the_scan_finishes(self):
        assert "await askSifTimezone();" in self.script

    def test_it_is_asked_only_once(self):
        assert "if (sifTimezoneAsked) return;" in self.script

    def test_it_is_skipped_when_the_flight_has_no_sif(self):
        assert "if (!prompt.required) return;" in self.script

    def test_the_choice_is_sent_to_the_server(self):
        assert "'/api/sif/timezone'" in self.script
        assert "timezone: picked.value" in self.script

    def test_the_operator_is_told_what_is_not_changed(self):
        assert "Gimbal and Noseboom are recorded in UTC" in self.script


class TestRoutes:
    server = (Path(__file__).resolve().parents[1] / "app" / "server.py").read_text(encoding="utf-8")

    def test_the_prompt_and_the_answer_are_served(self):
        assert self.server.count('path == "/api/sif/timezone"') == 2

    def test_the_declaration_reaches_the_reader_before_sif_runs(self):
        backend = (Path(__file__).resolve().parents[1] / "app" / "scan_backend.py").read_text(encoding="utf-8")
        task = backend[backend.index("def _sif_task("):]
        assert "self._apply_sif_timezone()" in task[:400]
