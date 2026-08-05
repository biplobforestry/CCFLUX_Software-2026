"""Downloading Noseboom from a .ccflux project.

Reopening a project restores no scan report, so a download that consulted only
the report failed on a project the operator had just opened - and the message
blamed the Time Filter, which was applied. The raw CSV is tens of gigabytes and
stays on the acquisition machine, so a project carries a 10 Hz table instead:
enough for any request between 1 and 10 Hz, wherever the project is opened.
"""
import inspect
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


class TestArchiveCarriesNavigation:
    """A project must be able to reapply the straight-flight criteria alone."""

    source = inspect.getsource(DashboardScanBackend._archive_noseboom_10hz)

    def test_the_navigation_columns_travel_with_the_download_columns(self):
        assert "resample_navigation(data, rule)" in self.source

    def test_the_archive_keeps_the_internal_column_names(self):
        """Resampling renames columns for the operator's download; renamed
        once, the archive could not be read back as a source."""
        assert "module.EXPORT_COLUMNS.items()" in self.source
        assert "table.rename(" in self.source

    def test_a_naive_index_is_localised_rather_than_converted(self):
        assert "navigation.index.tz is None" in self.source

    def test_the_criteria_can_be_reapplied_from_the_archive(self):
        preview = inspect.getsource(
            DashboardScanBackend.preview_noseboom_straight_settings
        )
        assert "_one_hz_from_archive(archive" in preview
        assert "self._archived_noseboom_table()" in preview

    def test_the_old_guard_no_longer_blames_the_time_filter(self):
        """The message said to apply the Time Filter when it was applied."""
        preview = inspect.getsource(
            DashboardScanBackend.preview_noseboom_straight_settings
        )
        assert "Complete Initial Check, apply the Time Filter" not in preview
        assert "Apply the Time Filter before changing the criteria" in preview

    def test_an_archive_without_navigation_says_what_is_missing(self):
        helper = inspect.getsource(DashboardScanBackend._one_hz_from_archive)
        assert "written before the" in helper
        assert "missing:" in helper

    def test_the_one_hz_grid_comes_from_the_stored_ten_hertz(self):
        helper = inspect.getsource(DashboardScanBackend._one_hz_from_archive)
        assert 'resample_navigation(window, "1s")' in helper


def test_the_navigation_resampling_is_shared_with_the_one_hertz_builder():
    """one_hz must stay the 1 s case of the same function, so the table the
    archive rebuilds is the table the raw delivery would have produced."""
    from instruments.noseboom.legacy_bridge import LegacyNoseboomBridge

    module = LegacyNoseboomBridge().module
    assert hasattr(module, "resample_navigation")
    assert "resample_navigation(data,'1s')" in inspect.getsource(module.one_hz)
