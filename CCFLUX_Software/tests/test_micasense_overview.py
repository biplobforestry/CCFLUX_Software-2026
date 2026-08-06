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
    RADIANS_AS_RECORDED,
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

    def test_nothing_is_converted(self):
        """micasense_scientific_overview.py stores every one of these raw, so a
        value here and the same value in its CSV must be the same number."""
        values = _qa_fields({
            "SolarElevation": "0.97441876",
            "IrradianceYaw": "-27.2997",
            "Irradiance": "9.685",
        })
        assert values["solar_elevation"] == pytest.approx(0.97441876)
        assert values["dls_yaw"] == pytest.approx(-27.2997)
        assert values["dls_irradiance"] == pytest.approx(9.685)

    def test_the_names_match_the_reference_script(self):
        """dls_yaw, solar_elevation and the rest, as the script calls them."""
        values = _qa_fields({})
        for name in ("dls_yaw", "dls_pitch", "dls_roll", "dls_irradiance",
                     "dls_horizontal_irradiance", "dls_direct_irradiance",
                     "dls_scattered_irradiance", "solar_elevation",
                     "solar_azimuth"):
            assert name in values, name

    def test_solar_geometry_is_recorded_in_radians(self):
        """Documented, not converted: 0.9744 rad is 55.8 deg, which is right for
        13:00 local in August at 51.4 N."""
        assert "SolarElevation" in RADIANS_AS_RECORDED

    def test_runs_of_capitals_are_one_word_in_the_field_name(self):
        assert _qa_record_key("GPSXYAccuracy") == "gps_xy_accuracy"
        assert _qa_record_key("GPSZAccuracy") == "gps_z_accuracy"
        assert _qa_record_key("ImagerTemperatureC") == "imager_temperature_c"

    def test_no_field_name_claims_a_conversion(self):
        """A _deg suffix on a radian value would be a lie about the number."""
        for name in NUMERIC_QA_FIELDS:
            assert not _qa_record_key(name).endswith("_deg"), name

    def test_a_missing_or_unparsable_value_is_none_not_zero(self):
        values = _qa_fields({"Irradiance": "", "SolarElevation": "not a number"})
        assert values["dls_irradiance"] is None
        assert values["solar_elevation"] is None

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
            {**{field: None for field in CAPTURE_CONDITION_FIELDS}, "dls_irradiance": 9.7},
        ]
        assert _capture_conditions(group)["dls_irradiance"] == 9.7

    def test_an_absent_condition_stays_none(self):
        group = [{field: None for field in CAPTURE_CONDITION_FIELDS}]
        assert _capture_conditions(group)["dls_irradiance"] is None


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

    def _captures(self, elevations_deg, hours=4.0):
        """Recorded in radians, as the camera writes them."""
        from math import radians

        start = datetime(2026, 8, 3, 11, 21, tzinfo=timezone.utc)
        step = timedelta(hours=hours) / max(1, len(elevations_deg) - 1)
        return [
            {"trigger_time": start + step * index,
             "solar_elevation": radians(value)}
            for index, value in enumerate(elevations_deg)
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
             "solar_elevation": None}
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


class TestTheValuesMatchTheReferenceScript:
    """micasense_scientific_overview.py is the reference for this delivery.

    It stores every DLS and solar field raw - as_float(xmp.get(...)) and no
    arithmetic - so this page must too, or a cross-check between the page and
    its CSV compares two different numbers. Converting the attitude as radians
    put yaw at +/-2000 degrees; converting solar geometry to degrees disagreed
    with the CSV.
    """

    def test_the_attitude_is_left_in_the_degrees_it_is_recorded_in(self):
        from instruments.micasense.adapter import _qa_fields

        values = _qa_fields({
            "IrradianceYaw": "-27.2997",
            "IrradiancePitch": "-1.1582",
            "IrradianceRoll": "89.0400",
        })
        assert values["dls_yaw"] == pytest.approx(-27.2997)
        assert values["dls_pitch"] == pytest.approx(-1.1582)
        assert values["dls_roll"] == pytest.approx(89.04)

    def test_solar_geometry_is_left_in_the_radians_it_is_recorded_in(self):
        from instruments.micasense.adapter import _qa_fields

        values = _qa_fields({"SolarElevation": "0.97441876", "SolarAzimuth": "3.2772"})
        assert values["solar_elevation"] == pytest.approx(0.97441876)
        assert values["solar_azimuth"] == pytest.approx(3.2772)

    def test_a_yaw_stays_inside_a_compass(self):
        """The check that caught the mistake: a yaw is not 2000 degrees."""
        from instruments.micasense.adapter import _qa_fields

        for raw in ("-27.2997", "32.2161", "-4.8197"):
            assert -180 <= _qa_fields({"IrradianceYaw": raw})["dls_yaw"] <= 180

    def test_the_solar_axis_states_the_unit_it_is_in(self):
        script = (ASSETS / "micasense.js").read_text(encoding="utf-8")
        assert "Elevation [rad, as recorded]" in script

    def test_the_caption_says_it_matches_the_reference_csv(self):
        html = (ASSETS / "micasense.html").read_text(encoding="utf-8")
        assert "match the standalone MicaSense overview CSV value for value" in html

    def test_the_static_geometry_check_still_reads_in_degrees(self):
        """The recorded value is radians; a spread threshold has to be an angle
        a person can judge."""
        adapter = (
            Path(__file__).resolve().parents[1]
            / "instruments" / "micasense" / "adapter.py"
        ).read_text(encoding="utf-8")
        block = adapter[adapter.index("def _static_solar_geometry_warning"):]
        assert "degrees(value) for _, value in points" in block


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




class TestPortedFromTheReferenceQaModule:
    """micasense_postflight_qa.py's definitions, so the two agree.

    Its make_dashboard plots against image number and derives cadence from the
    boot clock divided by the image-number gap. Both matter on Flight_CCT0803:
    the EXIF clock is unreliable on 24 captures, and a gap in the numbering
    would otherwise read as a cadence spike.
    """

    def test_sharpness_is_the_normalised_squared_gradient(self):
        """Normalising by the 1-99 spread makes captures of different brightness
        comparable, which is the point of the reference's formula."""
        import io

        from instruments.micasense.adapter import _sharpness_and_exposure

        flat = np.full((64, 64), 8000, dtype=np.uint16)
        buffer = io.BytesIO()
        Image.fromarray(flat, mode="I;16").save(buffer, format="TIFF")
        quiet = _sharpness_and_exposure(buffer.getvalue())

        # Stripes wider than the 4x decimation the reference applies, or every
        # sampled column lands on the same phase and the gradient reads zero.
        edges = flat.copy()
        edges[:, ::16] = 30000
        edges[:, 1::16] = 30000
        buffer = io.BytesIO()
        Image.fromarray(edges, mode="I;16").save(buffer, format="TIFF")
        busy = _sharpness_and_exposure(buffer.getvalue())

        assert busy["sharpness"] > quiet["sharpness"]
        for key in ("dark_fraction", "saturated_fraction", "p01", "p50", "p99"):
            assert key in busy

    def test_the_capture_number_comes_from_the_img_name(self):
        """Not from the XMP CaptureId, which is a random token: reading digits
        out of mxPsuh847qgEuwZespyY returned 847 and collapsed the axis."""
        from instruments.micasense.adapter import _capture_number

        assert _capture_number("IMG_0294_1.tif") == 294
        assert _capture_number("mxPsuh847qgEuwZespyY") is None
        assert _capture_number(None, "/a/IMG_1500.zip::IMG_1500_1.tif") == 1500

    def test_cadence_is_normalised_by_the_image_number_gap(self):
        """A gap in the numbering is one slow interval, not a spike."""
        from instruments.micasense.adapter import _normalised_intervals

        captures = [
            {"image_number": 0, "boot_timestamp": 100.0},
            {"image_number": 1, "boot_timestamp": 106.0},
            # Five captures missing: 30 s of boot clock over 5 images.
            {"image_number": 6, "boot_timestamp": 136.0},
        ]
        intervals = _normalised_intervals(captures)
        assert [round(value, 3) for _, value in intervals] == [6.0, 6.0]

    def test_cadence_ignores_a_pair_that_cannot_be_ordered(self):
        from instruments.micasense.adapter import _normalised_intervals

        assert _normalised_intervals([
            {"image_number": 5, "boot_timestamp": 100.0},
            {"image_number": 5, "boot_timestamp": 106.0},
        ]) == []

    def test_the_summary_carries_the_reference_metrics(self):
        from instruments.micasense.adapter import _capture_quality_summary

        captures = [
            {"image_number": index, "boot_timestamp": 100.0 + 6.0 * index,
             "sharpness": 0.05, "saturated_fraction": 0.0}
            for index in range(20)
        ]
        summary = _capture_quality_summary(captures, None)
        assert summary["median_capture_interval_seconds"] == pytest.approx(6.0)
        assert summary["capture_frequency_hz"] == pytest.approx(1 / 6.0)
        assert summary["cadence_warning_threshold_seconds"] == pytest.approx(9.0)
        assert summary["sharpness_samples"] == 20
        assert summary["saturation_flags_over_1_percent"] == 0

    def test_the_warning_line_follows_the_camera_when_none_is_stated(self):
        """The reference CLI defaults to 2.5 s. This camera triggers every
        6.26 s, which put 229 of 237 intervals past the threshold and reported a
        steady camera as almost entirely late."""
        from instruments.micasense.adapter import _capture_quality_summary

        captures = [
            {"image_number": index, "boot_timestamp": 100.0 + 6.26 * index}
            for index in range(30)
        ]
        measured = _capture_quality_summary(captures, None)
        assert measured["cadence_outliers"] == 0
        stated = _capture_quality_summary(captures, 2.5)
        assert stated["cadence_outliers"] > 20

    def test_a_stated_expected_interval_is_honoured(self):
        from instruments.micasense.adapter import _capture_quality_summary

        captures = [
            {"image_number": index, "boot_timestamp": 100.0 + 2.5 * index}
            for index in range(10)
        ]
        summary = _capture_quality_summary(captures, 2.5)
        assert summary["expected_interval_seconds"] == pytest.approx(2.5)
        assert summary["cadence_warning_threshold_seconds"] == pytest.approx(3.75)

    def test_sharpness_is_sampled_not_exhaustive(self):
        """Band 6 is 10 MB and a flight holds thousands."""
        from instruments.micasense.adapter import SHARPNESS_SAMPLE_STEP

        assert SHARPNESS_SAMPLE_STEP == 10

    def test_the_page_plots_all_three(self):
        html = (ASSETS / "micasense.html").read_text(encoding="utf-8")
        script = (ASSETS / "micasense.js").read_text(encoding="utf-8")
        for element in ("integrityPlot", "sharpnessPlot", "cadencePlot"):
            assert f'id="{element}"' in html
            assert element in script
        assert "Normalized capture interval" in script
        assert "Bottom 5% of this flight" in script
