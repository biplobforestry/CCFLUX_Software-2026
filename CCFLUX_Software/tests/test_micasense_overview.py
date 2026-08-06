"""The MicaSense workspace shows the flight, not nine blank boxes.

Two separate defects made the sample-image strip useless. A RedEdge-P band is a
16-bit single-channel TIFF, and PIL's I;16 -> RGB conversion clips every value
above 255 instead of scaling, so the five 1456x1088 bands came out uniformly
white. PIL also cannot resize an I;16 image at all, so the 2464x2056
panchromatic band raised out of Image.thumbnail and was swallowed by the
caller's except/continue, producing no file.

The page also had nothing to plot, because the acquisition metadata the camera
records - downwelling irradiance, sensor attitude, solar geometry, imager
temperature, barometric altitude, position quality - was parsed and then
discarded.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from instruments.micasense.adapter import (
    CAPTURE_CONDITION_FIELDS,
    NUMERIC_QA_FIELDS,
    RADIAN_QA_FIELDS,
    MINIMUM_CAPTURE_YEAR,
    STATIC_SOLAR_MAX_SPREAD_DEG,
    THUMBNAIL_STRETCH_PERCENTILES,
    TRACEABILITY_FIELDS,
    _camera_identity,
    _capture_conditions,
    _eight_bit_for_display,
    _qa_fields,
    _qa_record_key,
    _static_solar_geometry_warning,
    _write_thumbnail,
    _xmp_metadata,
)

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"


def _band(width=1456, height=1088, low=4064, high=65504):
    """A 16-bit band whose signal sits where a real RedEdge-P band's does."""
    rows = np.linspace(low, high, width, dtype=np.uint16)
    return Image.fromarray(np.tile(rows, (height, 1)), mode="I;16")


class TestSixteenBitBandsBecomeVisible:
    def test_a_sixteen_bit_band_is_scaled_not_clipped(self):
        """convert('RGB') returned 255 everywhere: a white box."""
        converted = _eight_bit_for_display(_band())
        values = np.asarray(converted)
        assert converted.mode == "L"
        assert values.min() < 40
        assert values.max() > 215

    def test_the_old_conversion_is_what_produced_white(self):
        """Guards the claim: the previous code really did clip to white."""
        clipped = np.asarray(_band().convert("RGB"))
        assert clipped.min() == 255 and clipped.max() == 255

    def test_the_panchromatic_band_can_now_be_resized(self, tmp_path):
        """Image.thumbnail raises "image has wrong mode" on I;16."""
        with pytest.raises(ValueError):
            _band(2464, 2056).thumbnail((480, 360))
        target = tmp_path / "panchro.jpg"
        _write_thumbnail(_band(2464, 2056), target)
        assert target.is_file()

    def test_a_written_thumbnail_is_not_blank(self, tmp_path):
        target = tmp_path / "band.jpg"
        _write_thumbnail(_band(), target)
        with Image.open(target) as saved:
            values = np.asarray(saved)
        assert values.min() != values.max()

    def test_a_thumbnail_is_bounded_in_size(self, tmp_path):
        target = tmp_path / "band.jpg"
        _write_thumbnail(_band(2464, 2056), target)
        with Image.open(target) as saved:
            assert saved.size[0] <= 480 and saved.size[1] <= 360

    def test_a_uniform_band_is_mid_grey_not_a_divide_by_zero(self):
        flat = Image.fromarray(np.full((32, 32), 9000, dtype=np.uint16), mode="I;16")
        values = np.asarray(_eight_bit_for_display(flat))
        assert values.min() == values.max() == 128

    def test_an_eight_bit_image_is_left_alone(self):
        grey = Image.new("L", (8, 8), color=57)
        assert _eight_bit_for_display(grey) is grey

    def test_the_stretch_is_a_percentile_one(self):
        """A min/max stretch is set by single hot or dead pixels."""
        assert THUMBNAIL_STRETCH_PERCENTILES == (2.0, 98.0)

    def test_a_hot_pixel_does_not_flatten_the_scene(self):
        samples = np.full((64, 64), 8000, dtype=np.uint16)
        samples[:, :32] = 20000
        samples[0, 0] = 65535
        values = np.asarray(_eight_bit_for_display(Image.fromarray(samples, mode="I;16")))
        # The two real levels stay far apart despite the outlier.
        assert abs(int(values[10, 10]) - int(values[10, 40])) > 100


class TestTheCameraMetadataIsKept:
    def test_the_qa_fields_survive_xmp_parsing(self):
        payload = (
            b'<x><Irradiance>9.68</Irradiance><SolarElevation>0.974</SolarElevation>'
            b'<ImagerTemperatureC>32.9</ImagerTemperatureC>'
            b'<PressureAlt>136.49</PressureAlt><BandName>Blue</BandName></x>'
        )
        values = _xmp_metadata(payload)
        assert values["Irradiance"] == "9.68"
        assert values["SolarElevation"] == "0.974"
        assert values["ImagerTemperatureC"] == "32.9"
        assert values["PressureAlt"] == "136.49"
        assert values["BandName"] == "Blue"

    def test_radian_fields_are_converted_to_degrees(self):
        """The camera writes solar geometry in radians."""
        values = _qa_fields({"SolarElevation": "0.97441876"})
        assert values["solar_elevation_deg"] == pytest.approx(55.83, abs=0.01)

    def test_non_radian_fields_are_not_converted(self):
        values = _qa_fields({"Irradiance": "9.685"})
        assert values["irradiance"] == pytest.approx(9.685)

    def test_runs_of_capitals_are_one_word_in_the_field_name(self):
        assert _qa_record_key("GPSXYAccuracy") == "gps_xy_accuracy"
        assert _qa_record_key("GPSZAccuracy") == "gps_z_accuracy"
        assert _qa_record_key("ImagerTemperatureC") == "imager_temperature_c"

    def test_every_radian_field_is_named_as_degrees(self):
        for name in RADIAN_QA_FIELDS:
            assert _qa_record_key(name).endswith("_deg")

    def test_a_missing_or_unparsable_value_is_none_not_zero(self):
        values = _qa_fields({"Irradiance": "", "SolarElevation": "not a number"})
        assert values["irradiance"] is None
        assert values["solar_elevation_deg"] is None

    def test_every_plotted_field_is_produced_somewhere(self):
        """The page reads CAPTURE_CONDITION_FIELDS; they must all exist."""
        produced = set(_qa_fields({}))
        produced |= {
            "gps_latitude", "gps_longitude", "gps_altitude", "gps_dop",
            "exposure_time", "iso_speed",
        }
        assert not set(CAPTURE_CONDITION_FIELDS) - produced

    def test_numeric_and_traceability_fields_do_not_overlap(self):
        assert not set(NUMERIC_QA_FIELDS) & set(TRACEABILITY_FIELDS)


class TestConditionsBelongToTheCapture:
    def test_the_read_band_supplies_the_capture_conditions(self):
        """Only one band per capture is decompressed; the rest are listed."""
        group = [
            {field: None for field in CAPTURE_CONDITION_FIELDS},
            {**{field: None for field in CAPTURE_CONDITION_FIELDS}, "irradiance": 9.7},
        ]
        assert _capture_conditions(group)["irradiance"] == 9.7

    def test_an_absent_condition_stays_none(self):
        group = [{field: None for field in CAPTURE_CONDITION_FIELDS}]
        assert _capture_conditions(group)["irradiance"] is None


class TestCameraIdentity:
    def test_one_rig_is_reported_plainly(self):
        records = [{"rig_name": "RedEdge-P", "dls_serial": "DA05-2209718-WC"}] * 3
        identity = _camera_identity(records)
        assert identity["rig_name"] == "RedEdge-P"
        assert identity["dls_serial"] == "DA05-2209718-WC"

    def test_a_mixed_delivery_reports_every_value(self):
        """A mid-campaign sensor swap is invisible in the imagery."""
        records = [{"dls_serial": "A"}, {"dls_serial": "B"}]
        assert _camera_identity(records)["dls_serial"] == "A, B"

    def test_an_absent_field_is_omitted(self):
        assert _camera_identity([{"rig_name": None}]) == {}


class TestStaticSolarGeometryIsReported:
    """On Flight_CCT0803 the DLS froze solar elevation at 55.83 degrees."""

    def _captures(self, elevations, hours=4.0):
        start = datetime(2026, 8, 3, 11, 21, tzinfo=timezone.utc)
        step = timedelta(hours=hours) / max(1, len(elevations) - 1)
        return [
            {"trigger_time": start + step * index, "solar_elevation_deg": value}
            for index, value in enumerate(elevations)
        ]

    def test_a_frozen_elevation_over_hours_is_warned_about(self):
        warning = _static_solar_geometry_warning(self._captures([55.83] * 20))
        assert warning is not None
        assert "stopped updating" in warning
        assert "irradiance readings themselves are unaffected" in warning

    def test_a_sun_that_moves_is_not_warned_about(self):
        moving = [56.0 - 6.0 * index / 19 for index in range(20)]
        assert _static_solar_geometry_warning(self._captures(moving)) is None

    def test_a_short_flight_is_not_warned_about(self):
        """Over ten minutes the sun really does barely move."""
        captures = self._captures([55.83] * 20, hours=0.15)
        assert _static_solar_geometry_warning(captures) is None

    def test_the_threshold_is_a_degree(self):
        assert STATIC_SOLAR_MAX_SPREAD_DEG == 1.0

    def test_captures_without_geometry_are_not_warned_about(self):
        captures = [
            {"trigger_time": datetime(2026, 8, 3, 11, tzinfo=timezone.utc),
             "solar_elevation_deg": None}
        ]
        assert _static_solar_geometry_warning(captures) is None


class TestPreCampaignCapturesAreDropped:
    """A frame dated before the campaign was dated by an unlocked camera GPS.

    Including them made MicaSense claim it recorded from 1970-01-01 - 56 years
    of coverage for a four-hour flight - and put a 56-year gap in the trigger
    intervals. They are dropped from every calculation and reported instead.
    """

    adapter_source = (
        Path(__file__).resolve().parents[1]
        / "instruments" / "micasense" / "adapter.py"
    ).read_text(encoding="utf-8")

    def test_the_threshold_is_the_first_campaign_year(self):
        assert MINIMUM_CAPTURE_YEAR == 2025

    def test_it_matches_the_standalone_overview_default(self):
        """The two must agree on which captures are usable."""
        assert "--minimum-year default" in self.adapter_source

    def test_they_are_removed_before_anything_is_calculated(self):
        records = self.adapter_source.index("pre_campaign = [")
        grouping = self.adapter_source.index("self._captures = _capture_rows(")
        assert records < grouping

    def test_the_reason_named_is_the_gps(self):
        assert "GPS had not" in self.adapter_source

    def test_they_are_counted_and_reported(self):
        assert "image(s) are dated before " in self.adapter_source
        assert "excluded from the coverage, capture " in self.adapter_source

    def test_the_delivery_itself_is_not_touched(self):
        assert "images \\n" not in self.adapter_source
        assert "untouched on the delivery" in self.adapter_source

    def test_an_undated_image_is_still_kept_and_explained(self):
        """No stamp at all is a different case from a wrong stamp."""
        assert "no usable acquisition time" in self.adapter_source


class TestThePageShowsThePlots:
    html = (ASSETS / "micasense.html").read_text(encoding="utf-8")
    script = (ASSETS / "micasense.js").read_text(encoding="utf-8")

    @pytest.mark.parametrize("element", [
        "cadencePlot", "trackPlot", "altitudePlot", "irradiancePlot",
        "exposurePlot", "orientationPlot", "solarPlot", "temperaturePlot",
        "completenessPlot",
    ])
    def test_each_panel_has_a_target_and_a_renderer(self, element):
        assert f'id="{element}"' in self.html
        assert element in self.script

    def test_plotly_is_loaded(self):
        assert "/vendor/plotly.min.js" in self.html

    def test_the_plots_are_rendered_on_load(self):
        assert "renderPlots(data)" in self.script

    def test_a_gap_is_a_break_not_a_zero(self):
        assert "Number.isFinite(Number(value)) ? Number(value) : null" in self.script

    def test_boot_time_captures_are_kept_off_the_time_axis(self):
        assert "startsWith('19')" in self.script

    def test_an_empty_series_says_so_instead_of_drawing_nothing(self):
        assert "function noData" in self.script
        assert "recorded no irradiance" in self.script

    def test_the_solar_panel_explains_a_flat_line(self):
        assert "stopped recomputing" in self.html

    def test_the_sample_strip_says_the_stretch_is_for_display(self):
        assert "contrast-stretched" in self.html
        assert "stored counts are untouched" in self.html

    def test_the_page_still_states_no_reflectance_work_is_done(self):
        assert "Radiometry, reflectance and vegetation indices are done by the" in self.html

    def test_camera_identity_is_shown(self):
        assert 'id="traceability"' in self.html
        assert "renderTraceability" in self.script


class TestTheCaptureRowsReachThePage:
    """The plots read data.captures, which was always empty.

    _micasense_browser_payload took the rows from getattr(result, "captures"),
    but InstrumentResult carries only the summary counts - the adapter keeps the
    rows on itself. So the key was always [] and every panel on the page drew
    nothing, however good the data was.
    """

    backend_source = (
        Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")

    def test_the_rows_come_from_the_adapter_not_the_result(self):
        assert 'getattr(result, "captures"' not in self.backend_source
        assert "captures=adapter.capture_rows()" in self.backend_source

    def test_the_adapter_exposes_them(self):
        from instruments.micasense.adapter import MicaSenseLevel1Adapter

        assert hasattr(MicaSenseLevel1Adapter, "capture_rows")

    def test_a_trigger_time_becomes_an_iso_string(self):
        from app.scan_backend import _json_safe_capture

        row = {"trigger_time": datetime(2026, 8, 3, 11, 30, tzinfo=timezone.utc)}
        assert _json_safe_capture(row)["trigger_time"].startswith("2026-08-03T11:30")

    def test_band_sets_become_lists(self):
        from app.scan_backend import _json_safe_capture

        assert _json_safe_capture({"found_bands": {3, 1, 2}})["found_bands"] == [1, 2, 3]

    def test_an_exif_rational_becomes_a_float(self):
        """PIL hands exposure time back as IFDRational, which json refuses."""
        from PIL.TiffImagePlugin import IFDRational

        from app.scan_backend import _json_safe_capture

        value = _json_safe_capture({"exposure_time": IFDRational(1, 723)})
        assert isinstance(value["exposure_time"], float)
        assert value["exposure_time"] == pytest.approx(1 / 723)

    def test_the_whole_row_survives_json(self):
        import json

        from PIL.TiffImagePlugin import IFDRational

        from app.scan_backend import _json_safe_capture

        row = {
            "trigger_time": datetime(2026, 8, 3, 11, 30, tzinfo=timezone.utc),
            "found_bands": {1, 2, 3, 4, 5, 6},
            "exposure_time": IFDRational(1, 723),
            "irradiance": 9.685,
            "complete": True,
            "capture_id": "mxPsuh847qgEuwZespyY",
            "missing_bands": [],
        }
        json.dumps(_json_safe_capture(row))

    def test_an_unencodable_value_is_kept_as_text_not_dropped(self):
        from app.scan_backend import _json_safe_capture

        class Odd:
            def __str__(self):
                return "odd"

        assert _json_safe_capture({"x": Odd()})["x"] == "odd"


class TestMacResourceForksAreNotDeliveries:
    """Two warnings on Flight_CCT0803 were about files that are not data.

    Copying a MicaSense folder through a non-HFS volume leaves a 4 KB
    "._IMG_0000.zip" beside each real archive. It is an AppleDouble stub, not a
    ZIP, so opening it raised BadZipFile and the capture was counted as a
    corrupt archive - two "corrupt or unreadable" warnings on a delivery where
    nothing was wrong.
    """

    def test_a_stub_is_recognised(self):
        from instruments.micasense.adapter import _is_apple_double

        assert _is_apple_double(Path("._IMG_0000.zip")) is True
        assert _is_apple_double(Path("/a/b/._IMG_0488.zip")) is True

    def test_a_real_archive_is_not(self):
        from instruments.micasense.adapter import _is_apple_double

        assert _is_apple_double(Path("IMG_0000.zip")) is False
        assert _is_apple_double(Path("/a/b/IMG_0000_1.tif")) is False

    def test_a_stub_beside_a_real_archive_is_skipped(self, tmp_path):
        import io
        import zipfile

        from PIL import Image

        from instruments.micasense.adapter import _all_images, release_archive_handle

        real = tmp_path / "IMG_0000.zip"
        with zipfile.ZipFile(real, "w") as bundle:
            for band in range(1, 7):
                buffer = io.BytesIO()
                Image.new("L", (8, 8), color=band).save(buffer, format="TIFF")
                bundle.writestr(f"IMG_0000_{band}.tif", buffer.getvalue())
        # What macOS leaves behind: not a ZIP at all.
        (tmp_path / "._IMG_0000.zip").write_bytes(b"\x00\x05\x16\x07" + b"\x00" * 64)

        images = _all_images((tmp_path,))
        release_archive_handle()

        assert len(images) == 6
        assert not [
            item for item in images
            if getattr(item, "member", "") == "__CORRUPT_ARCHIVE__.tif"
        ]

    def test_a_genuinely_broken_archive_is_still_reported(self, tmp_path):
        """Skipping stubs must not hide a real unreadable delivery."""
        from instruments.micasense.adapter import _all_images, release_archive_handle

        (tmp_path / "IMG_0001.zip").write_bytes(b"not a zip at all")
        images = _all_images((tmp_path,))
        release_archive_handle()

        assert [
            item for item in images
            if getattr(item, "member", "") == "__CORRUPT_ARCHIVE__.tif"
        ]

    def test_a_stub_tiff_is_skipped_too(self, tmp_path):
        from instruments.micasense.adapter import _image_files

        (tmp_path / "IMG_0000_1.tif").write_bytes(b"II*\x00")
        (tmp_path / "._IMG_0000_1.tif").write_bytes(b"\x00\x05\x16\x07")
        found = _image_files(tuple(tmp_path.iterdir()))
        assert [p.name for p in found] == ["IMG_0000_1.tif"]


class TestTheFiguresActuallyDraw:
    """Found by rendering the page in a headless browser and looking at it.

    Three defects no data check would have caught: nine WebGL panels showed
    "WebGL is not supported by your browser" instead of figures, every title
    printed on top of its legend, and the band-count axis auto-ranged a single
    category across 5.6-6.4 as one full-width bar.
    """

    script = (ASSETS / "micasense.js").read_text(encoding="utf-8")

    def test_no_panel_needs_webgl(self):
        """A VM, a remote desktop or an old driver has no WebGL, and a browser
        caps its contexts well below nine."""
        assert "type:'scattergl'" not in self.script
        assert "type:'scatter'" in self.script

    def test_the_title_has_room_above_the_legend(self):
        assert "margin:{l:78,r:58,t:86,b:64}" in self.script
        assert "legend:{orientation:'h',y:1.06,x:0,yanchor:'bottom'}" in self.script

    def test_band_counts_are_a_category_axis(self):
        assert "{xaxis:{type:'category'},bargap:.6}" in self.script


class TestTheDlsAttitudeIsAlreadyInDegrees:
    """Rendering showed yaw at +/-2000 degrees, which is not a yaw.

    Solar geometry is written in radians - SolarElevation 0.9744 is 55.8 deg,
    right for 13:00 local in August at 51.4 N. The DLS attitude is not: across
    Flight_CCT0803 yaw runs -27 to +32 and roll reaches 89, already degrees.
    Converting them multiplied everything by 57.3.
    """

    def test_solar_geometry_is_converted(self):
        from instruments.micasense.adapter import RADIAN_QA_FIELDS, _qa_fields

        assert "SolarElevation" in RADIAN_QA_FIELDS
        values = _qa_fields({"SolarElevation": "0.97441876"})
        assert values["solar_elevation_deg"] == pytest.approx(55.83, abs=0.01)

    def test_the_attitude_is_not_converted(self):
        from instruments.micasense.adapter import DEGREE_QA_FIELDS, _qa_fields

        assert "IrradianceYaw" in DEGREE_QA_FIELDS
        values = _qa_fields({
            "IrradianceYaw": "-27.2997",
            "IrradiancePitch": "-1.1582",
            "IrradianceRoll": "89.0400",
        })
        assert values["irradiance_yaw_deg"] == pytest.approx(-27.2997)
        assert values["irradiance_pitch_deg"] == pytest.approx(-1.1582)
        assert values["irradiance_roll_deg"] == pytest.approx(89.04)

    def test_the_two_sets_do_not_overlap(self):
        from instruments.micasense.adapter import (
            DEGREE_QA_FIELDS,
            RADIAN_QA_FIELDS,
        )

        assert not RADIAN_QA_FIELDS & DEGREE_QA_FIELDS

    def test_both_sets_are_still_named_in_degrees(self):
        from instruments.micasense.adapter import (
            DEGREE_QA_FIELDS,
            RADIAN_QA_FIELDS,
            _qa_record_key,
        )

        for name in RADIAN_QA_FIELDS | DEGREE_QA_FIELDS:
            assert _qa_record_key(name).endswith("_deg"), name

    def test_a_plausible_yaw_stays_inside_a_compass(self):
        """The check that would have caught it: a yaw is not 2000 degrees."""
        from instruments.micasense.adapter import _qa_fields

        for raw in ("-27.2997", "32.2161", "-4.8197"):
            value = _qa_fields({"IrradianceYaw": raw})["irradiance_yaw_deg"]
            assert -180 <= value <= 180, raw
