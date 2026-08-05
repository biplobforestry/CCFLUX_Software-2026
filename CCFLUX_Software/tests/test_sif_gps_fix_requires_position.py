"""A GPS time is trusted only where the receiver reported a position.

On Flight_CCT0803 every AirFloX row of both channels reads GPS_lat=0.00000 -
the receiver never locked - while GPS_TIME_UTC sits at the 23:59:5x/00:00:xx
rollover, with a handful of rows about two hours from the record clock.
Judging a fix by the timestamp alone accepted those rows and shifted the whole
flight by 7219 s. FLOX and FULL carry no GPS, so their record clock is UTC.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "instruments" / "sif" / "legacy"))
import airflox_sif_automation as afx


class Raw:
    def __init__(self, lat, lon):
        self.gps_lat, self.gps_lon = lat, lon


class TestPositionMask:
    def test_a_receiver_that_never_locked_reports_no_position(self):
        mask = afx.gps_position_mask(Raw(["0.00000"] * 5, ["0.00000"] * 5))
        assert not mask.any()

    def test_a_real_position_is_accepted(self):
        mask = afx.gps_position_mask(Raw(["51.4075 N"], ["6.9451 E"]))
        assert mask.all()

    def test_a_southern_or_western_position_is_accepted(self):
        mask = afx.gps_position_mask(Raw(["33.9 S"], ["18.4 E"]))
        assert mask.all()

    def test_an_impossible_coordinate_is_refused(self):
        mask = afx.gps_position_mask(Raw(["95.0", "51.4"], ["6.9", "200.0"]))
        assert not mask.any()

    def test_a_file_without_coordinates_does_not_veto(self):
        """Older deliveries carry no GPS_lat; the date test still decides."""
        assert afx.gps_position_mask(Raw([], [])) is None
        assert afx.gps_position_mask(None) is None


class TestFixMask:
    def _times(self, count):
        import pandas as pd

        base = pd.Timestamp("2026-08-03 11:00:00")
        return [base] * count, [base] * count

    def test_a_matching_time_without_a_position_is_not_a_fix(self):
        utc, clock = self._times(3)
        mask = afx.real_gps_fix_mask(utc, clock, Raw(["0.00000"] * 3, ["0.00000"] * 3))
        assert not mask.any()

    def test_a_matching_time_with_a_position_is_a_fix(self):
        utc, clock = self._times(2)
        mask = afx.real_gps_fix_mask(utc, clock, Raw(["51.4 N"] * 2, ["6.9 E"] * 2))
        assert mask.all()

    def test_without_a_raw_frame_the_date_test_still_applies(self):
        utc, clock = self._times(2)
        assert afx.real_gps_fix_mask(utc, clock).all()

    def test_no_offset_can_be_measured_without_a_position(self):
        utc, clock = self._times(4)
        offset, count, _spread = afx.measure_record_clock_offset(
            utc, clock, Raw(["0.00000"] * 4, ["0.00000"] * 4)
        )
        assert offset is None and count == 0


@pytest.mark.skipif(
    not Path("/Volumes/SSD/Flight_CCT0803/FLOXINSIDE_260803").is_dir(),
    reason="the campaign delivery is not attached",
)
def test_flight_cct0803_has_no_position_in_either_channel():
    root = Path("/Volumes/SSD/Flight_CCT0803/FLOXINSIDE_260803")
    for channel in ("Flox", "Full"):
        for path in sorted((root / channel / "260803").glob("*.CSV")):
            if path.name.startswith("._"):
                continue
            latitudes, longitudes = [], []
            for row in path.read_text(errors="replace").splitlines():
                parts = row.split(";")
                fields = {
                    p.rstrip("="): parts[index + 1].strip()
                    for index, p in enumerate(parts)
                    if p.endswith("=") and index + 1 < len(parts)
                }
                if "GPS_lat" in fields:
                    latitudes.append(fields["GPS_lat"])
                    longitudes.append(fields.get("GPS_lon", "0"))
            if not latitudes:
                continue
            mask = afx.gps_position_mask(Raw(latitudes, longitudes))
            assert not mask.any(), f"{path.name} unexpectedly reports a position"
