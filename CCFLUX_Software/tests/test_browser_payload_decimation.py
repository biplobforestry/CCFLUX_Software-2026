"""What a workspace page is asked to draw must be bounded, and still true.

Clicking FLIR visualisation left the page behind "Preparing FLIR workspace"
with nothing moving. A flight produces one record per frame, and every one was
sent, parsed and drawn: tens of megabytes of JSON, four traces over the whole
series, and thousands of map markers each with its popup text built up front.
None of that can be interrupted once started, so the browser could not repaint
and the page looked hung rather than busy.

The cost is fixed by drawing a reduced series. The risk in doing that is
scientific: an even stride is exactly as likely to drop the frame carrying a
temperature spike as any other. So these tests care less about the size than
about what survives the reduction.
"""

import pytest

from core.browser_payload import DEFAULT_VIEW_LIMIT, decimate_for_view

FIELDS = ("temperature_min_c", "temperature_max_c")


def _series(count, *, spike_at=None, spike=99.75, dip_at=None, dip=-40.5):
    rows = [
        {
            "frame_id": str(index),
            "timestamp_utc": f"2026-07-27T08:00:{index % 60:02d}+00:00",
            "temperature_min_c": 10.0 + (index % 7) * 0.1,
            "temperature_max_c": 40.0 + (index % 11) * 0.1,
        }
        for index in range(count)
    ]
    if spike_at is not None:
        rows[spike_at]["temperature_max_c"] = spike
    if dip_at is not None:
        rows[dip_at]["temperature_min_c"] = dip
    return rows


def test_a_short_series_is_returned_untouched():
    rows = _series(500)

    kept, total = decimate_for_view(rows, extreme_fields=FIELDS)

    assert kept == rows
    assert total == 500


def test_a_long_series_is_brought_within_the_limit():
    kept, total = decimate_for_view(_series(120_000), extreme_fields=FIELDS)

    assert total == 120_000
    assert len(kept) <= DEFAULT_VIEW_LIMIT


def test_the_highest_temperature_survives():
    """The frame an even stride would most likely drop is the one that matters."""
    rows = _series(120_000, spike_at=76_543)

    kept, _ = decimate_for_view(rows, extreme_fields=FIELDS)

    assert max(row["temperature_max_c"] for row in kept) == 99.75


def test_the_lowest_temperature_survives():
    rows = _series(120_000, dip_at=12_345)

    kept, _ = decimate_for_view(rows, extreme_fields=FIELDS)

    assert min(row["temperature_min_c"] for row in kept) == -40.5


def test_a_plain_stride_would_have_lost_the_spike():
    """Establishes that the bucketing is doing real work, not decoration."""
    rows = _series(120_000, spike_at=76_543)
    stride = max(1, len(rows) // DEFAULT_VIEW_LIMIT)
    naive = rows[::stride]

    assert max(row["temperature_max_c"] for row in naive) < 99.75


def test_the_ends_of_the_flight_are_kept():
    rows = _series(120_000)

    kept, total = decimate_for_view(rows, extreme_fields=FIELDS)

    assert kept[0]["frame_id"] == "0"
    assert kept[-1]["frame_id"] == str(total - 1)


def test_order_is_preserved_and_nothing_is_repeated():
    kept, _ = decimate_for_view(_series(50_000), extreme_fields=FIELDS)

    indices = [int(row["frame_id"]) for row in kept]
    assert indices == sorted(indices)
    assert len(indices) == len(set(indices))


def test_records_are_passed_through_unchanged():
    """Decimation selects; it must never rewrite a value."""
    rows = _series(50_000, spike_at=999)

    kept, _ = decimate_for_view(rows, extreme_fields=FIELDS)

    for row in kept:
        assert row is rows[int(row["frame_id"])]


@pytest.mark.parametrize("bad", [None, "n/a", float("nan"), True, {}])
def test_unusable_values_do_not_break_the_reduction(bad):
    rows = _series(20_000)
    rows[5_000]["temperature_max_c"] = bad

    kept, total = decimate_for_view(rows, extreme_fields=FIELDS)

    assert total == 20_000
    assert len(kept) <= DEFAULT_VIEW_LIMIT


def test_a_series_of_only_unusable_values_still_reduces():
    rows = [{"frame_id": str(i), "temperature_max_c": None} for i in range(30_000)]

    kept, total = decimate_for_view(rows, extreme_fields=("temperature_max_c",))

    assert total == 30_000
    assert 0 < len(kept) <= DEFAULT_VIEW_LIMIT


def test_no_extreme_fields_still_bounds_the_series():
    kept, _ = decimate_for_view(_series(80_000))

    assert len(kept) <= DEFAULT_VIEW_LIMIT


def test_a_nonsense_limit_is_refused():
    with pytest.raises(ValueError, match="positive"):
        decimate_for_view(_series(10), limit=0)


def test_the_reduction_is_large_enough_to_matter():
    """The point of the exercise: a payload a browser can actually parse."""
    import json

    rows = _series(120_000)
    kept, total = decimate_for_view(rows, extreme_fields=FIELDS)

    before = len(json.dumps(rows))
    after = len(json.dumps(kept))
    assert after * 20 < before, f"{before/1e6:.1f} MB -> {after/1e6:.1f} MB is not enough"


class TestTheFlirViewBoundsWhatItServes:
    """A project processed before the reduction existed still holds every frame
    in its saved flir_browser.json. Reprocessing a flight so that its own
    workspace will open is not a reasonable thing to ask, so the view bounds
    the series on the way out as well as on the way in."""

    @staticmethod
    def _backend_with(records, tmp_path):
        from app.scan_backend import DashboardScanBackend

        backend = DashboardScanBackend(tmp_path)
        backend._instruments["flir"].quicklook = {
            "available": True, "temperature_available": True,
            "temperature_records": records, "map_points": [],
            "summary": {}, "thumbnails": [], "gaps": [],
        }
        return backend

    def test_an_unreduced_saved_payload_is_bounded_when_served(self, tmp_path):
        backend = self._backend_with(_series(120_000), tmp_path)

        served = backend.flir_view()["data"]

        assert len(served["temperature_records"]) <= DEFAULT_VIEW_LIMIT
        assert served["temperature_records_total"] == 120_000

    def test_the_spike_survives_that_reduction_too(self, tmp_path):
        backend = self._backend_with(_series(120_000, spike_at=76_543), tmp_path)

        served = backend.flir_view()["data"]

        assert max(r["temperature_max_c"] for r in served["temperature_records"]) == 99.75

    def test_the_stored_payload_is_not_modified(self, tmp_path):
        """Serving a reduced view must not discard the full record held in
        memory, which is what the project file is written from."""
        backend = self._backend_with(_series(120_000), tmp_path)

        backend.flir_view()

        assert len(backend._instruments["flir"].quicklook["temperature_records"]) == 120_000

    def test_a_small_payload_is_served_whole(self, tmp_path):
        backend = self._backend_with(_series(200), tmp_path)

        served = backend.flir_view()["data"]

        assert len(served["temperature_records"]) == 200

    def test_an_empty_payload_is_untouched(self, tmp_path):
        backend = self._backend_with([], tmp_path)

        served = backend.flir_view()["data"]

        assert served["temperature_records"] == []
