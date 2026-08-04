"""Downloading Noseboom from a .ccflux project.

Reopening a project restores no scan report, so a download that consulted only
the report failed on a project the operator had just opened - and the message
blamed the Time Filter, which was applied. The raw CSV is tens of gigabytes and
stays on the acquisition machine, so a project carries a 10 Hz table instead:
enough for any request between 1 and 10 Hz, wherever the project is opened.
"""
import re
from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"


class TestCustodians:
    def test_the_three_custodians_are_named(self):
        custodians = DashboardScanBackend.DATA_CUSTODIANS
        for name in ("Eva Y. Pfannerstill", "Georgios I. Gkatzelis", "Biplob Dey"):
            assert name in custodians

    def test_the_archive_is_ten_hertz(self):
        assert DashboardScanBackend.ARCHIVED_EXPORT_HZ == 10.0


class TestDownloadDialog:
    script = (ASSETS / "noseboom.js").read_text(encoding="utf-8")
    markup = (ASSETS / "noseboom.html").read_text(encoding="utf-8")

    def test_the_dialog_offers_frequencies_from_one_to_ten(self):
        block = re.search(r'id="downloadFrequency".*?</select>', self.markup, re.S).group(0)
        offered = {value for value in re.findall(r'<option value="(\d+)"', block)}
        assert {"1", "5", "10"} <= offered

    def test_the_dialog_is_told_what_the_project_can_serve(self):
        assert "payload?.download||{}" in self.script
        assert "download.source==='project'" in self.script

    def test_a_frequency_beyond_the_archive_is_disabled(self):
        assert "Number(option.value)>ceiling" in self.script

    def test_the_full_variable_set_is_disabled_on_a_project(self):
        assert "fullOption.disabled=fromProject" in self.script

    def test_the_note_names_who_to_contact(self):
        assert "download.custodians" in self.script

    def test_the_dialog_is_synced_once_the_payload_arrives(self):
        """Syncing only on open left the dialog wrong until it was opened."""
        assert "syncDownloadOptions();log('Noseboom browser data rendered" in self.script
        assert "function openDownload(){syncDownloadOptions();" in self.script

    def test_a_disabled_choice_falls_back_to_one_hertz(self):
        assert "if(!chosen||chosen.disabled)frequency.value='1'" in self.script


class TestExportGuards:
    """The refusal must say which limit was hit, and who can lift it."""

    def _backend(self, tmp_path, monkeypatch, *, raw, archive):
        backend = DashboardScanBackend.__new__(DashboardScanBackend)
        monkeypatch.setattr(
            DashboardScanBackend, "_noseboom_source_paths",
            lambda self: (raw,) if raw else (),
        )
        monkeypatch.setattr(
            DashboardScanBackend, "_archived_noseboom_table",
            lambda self: archive,
        )
        return backend

    def test_the_archive_is_looked_for_only_when_the_raw_file_is_gone(self, tmp_path, monkeypatch):
        raw = tmp_path / "NoseBoom.csv"
        raw.write_text("time_ns\n1\n", encoding="utf-8")
        backend = self._backend(tmp_path, monkeypatch, raw=raw, archive=None)
        assert backend._noseboom_source_paths() == (raw,)

    @pytest.mark.parametrize("frequency", [10.0, 5.0, 1.0])
    def test_a_frequency_the_archive_covers_is_allowed(self, frequency):
        assert 1.0 <= frequency <= DashboardScanBackend.ARCHIVED_EXPORT_HZ

    @pytest.mark.parametrize("frequency", [20.0, 50.0, 100.0])
    def test_a_frequency_beyond_the_archive_is_not(self, frequency):
        assert frequency > DashboardScanBackend.ARCHIVED_EXPORT_HZ


class TestArchiveTravelsWithTheProject:
    def test_the_archive_sits_in_a_bundled_directory(self):
        """A product outside these directories never enters the .ccflux."""
        from core.flight_project import PRODUCT_DIRECTORIES
        import inspect

        source = inspect.getsource(DashboardScanBackend._archive_noseboom_10hz)
        assert '"processed" / "noseboom" / "noseboom_10hz.csv"' in source
        assert "processed" in PRODUCT_DIRECTORIES

    def test_a_failed_archive_does_not_fail_the_flight(self):
        import inspect

        source = inspect.getsource(DashboardScanBackend._archive_noseboom_10hz)
        assert "return None" in source
        assert "capture_exception" in source
