"""One narrower interval, applied to everything a page shows.

A histogram, a spectrum and a wind comparison over a whole flight say nothing
about ten minutes inside it, so an investigation interval recomputes what it
narrows rather than cropping a picture of the whole. It never changes the main
GUI Time Filter or the saved project.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.scan_backend import DashboardScanBackend, _parse_investigation_time

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
START = datetime(2026, 8, 3, 11, 20, tzinfo=timezone.utc)
END = datetime(2026, 8, 3, 13, 50, tzinfo=timezone.utc)


class TestReadingAnIntervalEnd:
    def test_a_bare_stamp_is_utc(self):
        """The workspace states its times in UTC; a local guess moves the window."""
        assert _parse_investigation_time("2026-08-03 12:00:00", "start") == datetime(
            2026, 8, 3, 12, 0, tzinfo=timezone.utc
        )

    def test_an_offset_is_honoured(self):
        assert _parse_investigation_time(
            "2026-08-03T14:00:00+02:00", "start"
        ) == datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    def test_a_z_suffix_is_honoured(self):
        assert _parse_investigation_time("2026-08-03T12:00:00Z", "end") == datetime(
            2026, 8, 3, 12, 0, tzinfo=timezone.utc
        )

    def test_an_empty_value_is_refused(self):
        with pytest.raises(ValueError, match="Give the investigation start"):
            _parse_investigation_time("", "start")

    def test_nonsense_is_refused(self):
        with pytest.raises(ValueError, match="not a valid time"):
            _parse_investigation_time("yesterday", "end")


@pytest.fixture()
def backend(tmp_path):
    made = DashboardScanBackend(tmp_path)
    made._instruments["noseboom"].quicklook = {"available": True, "points": []}
    made._time_state.selected_analysis_start = START
    made._time_state.selected_analysis_end = END
    return made


class TestTheIntervalStaysInsideTheMainTimeFilter:
    def test_the_allowed_bounds_are_the_main_filter(self, backend):
        bounds = backend.noseboom_investigation_bounds()

        assert bounds["start"] == START.isoformat()
        assert bounds["end"] == END.isoformat()
        assert bounds["active"] is None

    def test_a_window_starting_before_the_filter_is_refused(self, backend):
        with pytest.raises(ValueError, match="must stay inside the main Time"):
            backend.start_noseboom_investigation(
                {"start": "2026-08-03 10:00:00", "end": "2026-08-03 12:00:00"}
            )

    def test_a_window_ending_after_the_filter_is_refused(self, backend):
        with pytest.raises(ValueError, match="must stay inside the main Time"):
            backend.start_noseboom_investigation(
                {"start": "2026-08-03 12:00:00", "end": "2026-08-03 23:00:00"}
            )

    def test_a_backwards_window_is_refused(self, backend):
        with pytest.raises(ValueError, match="end must be after its start"):
            backend.start_noseboom_investigation(
                {"start": "2026-08-03 12:30:00", "end": "2026-08-03 12:00:00"}
            )

    def test_an_unprocessed_instrument_is_refused(self, backend):
        backend._instruments["noseboom"].quicklook = {}
        with pytest.raises(ValueError, match="Process Noseboom before"):
            backend.start_noseboom_investigation(
                {"start": "2026-08-03 12:00:00", "end": "2026-08-03 12:30:00"}
            )

    def test_no_main_filter_is_refused(self, backend):
        backend._time_state.selected_analysis_start = None
        with pytest.raises(ValueError, match="main Time Filter"):
            backend.start_noseboom_investigation(
                {"start": "2026-08-03 12:00:00", "end": "2026-08-03 12:30:00"}
            )


class TestWhatTheIntervalReaches:
    """One filter, every panel and the export - not the plots alone."""

    def _apply(self, backend):
        window = {
            "start": (START + timedelta(minutes=40)).isoformat(),
            "end": (START + timedelta(minutes=70)).isoformat(),
        }
        backend._noseboom_investigation = {
            "interval": window,
            "data": {"available": True, "points": [{"time": "t"}], "marker": "window"},
            "quality_control": {"available": True, "marker": "window"},
        }
        return window

    def test_the_view_serves_the_recomputed_products(self, backend):
        backend._instruments["noseboom"].quicklook = {
            "available": True, "points": [], "marker": "whole flight",
        }
        window = self._apply(backend)

        view = backend.noseboom_view()

        assert view["data"]["marker"] == "window"
        assert view["investigation"]["active"] is True
        assert view["investigation"]["interval"] == window
        assert view["investigation"]["bounds"]["start"] == START.isoformat()

    def test_the_quality_check_follows_it(self, backend):
        self._apply(backend)

        qc = backend.noseboom_qc_view()

        assert qc["ready"] is True
        assert qc["data"]["marker"] == "window"

    def test_a_quality_check_that_could_not_be_recomputed_says_so(self, backend):
        self._apply(backend)
        backend._noseboom_investigation["quality_control"] = None

        qc = backend.noseboom_qc_view()

        assert qc["ready"] is False
        assert "investigation interval" in qc["message"]

    def test_clearing_it_returns_the_whole_flight(self, backend):
        backend._instruments["noseboom"].quicklook = {
            "available": True, "points": [], "marker": "whole flight",
        }
        self._apply(backend)

        backend.clear_noseboom_investigation()

        view = backend.noseboom_view()
        assert view["data"]["marker"] == "whole flight"
        assert view["investigation"]["active"] is False

    def test_the_export_reads_the_window_not_the_flight(self, backend):
        import inspect

        source = inspect.getsource(backend.start_noseboom_statistics_export)
        assert "_noseboom_investigation" in source


class TestTheMiroRackMapFiltersEveryInstrument:
    def test_every_layer_read_goes_through_the_filter(self):
        script = (ASSETS / "miro_rack_map.js").read_text(encoding="utf-8")

        assert "function insideInvestigation(point)" in script
        assert "function layerPoints(instrument, gas)" in script
        # No raw layer read is left: each one must be filtered, otherwise a
        # layer would still draw concentrations from outside the interval.
        assert "payload.layers[item.instrument]?.[item.gas]" not in script
        # The colour range comes from what survives, so the scale is not still
        # stretched by concentrations that are no longer drawn.
        assert "flatMap(item => layerPoints(item.instrument,item.gas))" in script
        # The flight track and the exported header follow it too.
        assert "insideInvestigation(point))" in script

    def test_the_page_offers_the_interval_and_an_update(self):
        page = (ASSETS / "miro_rack_map.html").read_text(encoding="utf-8")

        assert 'id="investigationStart"' in page
        assert 'id="investigationEnd"' in page
        assert 'id="investigationApply"' in page
        assert 'id="investigationClear"' in page

    def test_the_filter_card_is_not_treated_as_a_layer(self):
        """It carries no instrument select, and treating it as one threw."""
        script = (ASSETS / "miro_rack_map.js").read_text(encoding="utf-8")

        assert ".selection:not(.investigation)" in script


class TestTheFlirFilterReachesEverySubpage:
    def test_each_view_reads_through_the_filter(self):
        script = (ASSETS / "flir.js").read_text(encoding="utf-8")

        for accessor in (
            "function temperatureRows()", "function mapPoints()",
            "function acquisitionIntervals()", "function acquisitionGaps()",
        ):
            assert accessor in script
        # The summary, the frame statistics, the distribution, the variability
        # and the map all read the filtered arrays.
        assert "temperatures=temperatureRows(),mapped=mapPoints()" in script
        assert "const rows=temperatureRows()" in script
        assert "const intervals=acquisitionIntervals();" in script
        assert "const gaps=acquisitionGaps()" in script
        assert "points=mapPoints()" in script
        assert "(payload.temperature_records||[]).filter(row=>row.timestamp_utc)" not in script

    def test_the_page_offers_the_interval_and_an_update(self):
        page = (ASSETS / "flir.html").read_text(encoding="utf-8")

        assert 'id="investigationStart"' in page
        assert 'id="investigationApply"' in page
        assert 'id="investigationClear"' in page


class TestTheFlirMapExport:
    def test_the_page_offers_the_formats_and_1500_dpi(self):
        page = (ASSETS / "flir.html").read_text(encoding="utf-8")

        assert 'id="mapExportBtn"' in page
        for suffix in ("pdf", "png", "svg"):
            assert f'<option value="{suffix}"' in page
        assert '<option value="1500"' in page

    def test_the_export_follows_the_interval(self):
        script = (ASSETS / "flir.js").read_text(encoding="utf-8")

        assert "/api/flir/map/export" in script
        assert "interval:window_?" in script
        # The exported picture is the filtered one.
        assert "points=mapPoints().filter" in script
