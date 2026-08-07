from pathlib import Path
from datetime import datetime, timezone
import pytest
import numpy as np
import pandas as pd

from core.detector import InputCandidate
from core.enums import DetectionStatus, ProcessingStatus
from core.scanner import ScanEntry, ScanIndex
from instruments.sif import SifAdapter
from instruments.sif.adapter import _refresh_sza_from_position
from instruments.sif.legacy import noseboom_gimbal_for_sif
from instruments.sif.legacy_bridge import (
    BUNDLED_ESSENTIALS,
    BUNDLED_SOURCE,
    DEFAULT_ESSENTIALS,
    DEFAULT_SOURCE,
)
from core.legacy_paths import legacy_integration_path

SOURCE = legacy_integration_path(
    "Hatchbox", "SIF", "Flight_2123", "_combined", "Flight_2123_FULL.CSV"
)

# These cases read a real AirFLOX delivery from the Flight_2123 campaign tree.
# The distributed legacy_integration/ folder bundles the SIF scripts and
# calibration essentials but not that recording, so on a machine without the
# campaign disk (or CCFLUX_LEGACY_ROOT pointing at it) they are skipped rather
# than reported as failures.
requires_sif_recording = pytest.mark.skipif(
    not SOURCE.is_file(),
    reason=(
        f"AirFLOX reference recording is not available at {SOURCE}. Set "
        "CCFLUX_LEGACY_ROOT to an Integration_code tree that contains "
        "Hatchbox/SIF/Flight_2123 to run these cases."
    ),
)


def _fixture(path: Path) -> Path:
    lines = SOURCE.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    path.write_text("\n".join(lines[:12]) + "\n", encoding="utf-8")
    return path


@requires_sif_recording
def test_sif_detection_validation_processing_and_export(tmp_path: Path):
    source = _fixture(tmp_path / "F_test.CSV")
    adapter = SifAdapter(output_root=tmp_path / "output", flight_name="flight")
    candidate = adapter.detect(ScanIndex(
        tmp_path, (ScanEntry(source, source.stat().st_size, True),)
    ))[0]
    validation = adapter.validate(candidate)
    result = adapter.process_quicklook(
        adapter.load(candidate),
        {"modes": ("FULL",), "raw_min_kb": 0, "static_lat": 50.9, "static_lon": 6.4, "static_alt": 100},
    )
    outputs = adapter.export_results(result, adapter.output_root, ("csv",))
    browser = adapter.export_browser_data(result, adapter.output_root)

    assert validation.detection_status is DetectionStatus.WARNING
    assert result.processing_status is ProcessingStatus.COMPLETE
    assert result.metadata["processed_modes"] == ["FULL"]
    assert len(outputs) == 4
    assert all(item.path.is_file() for item in outputs)
    assert browser.path.is_file()
    assert '"ccflux-sif-browser-v1"' in browser.path.read_text(encoding="utf-8")


@requires_sif_recording
def test_sif_raw_file_size_filter_rejects_only_small_sources(tmp_path: Path):
    source = _fixture(tmp_path / "F_small.CSV")
    adapter = SifAdapter(output_root=tmp_path / "output", flight_name="flight")
    candidate = InputCandidate("sif", (source,), 1.0, "small raw fixture")

    with pytest.raises(ValueError, match="All FULL raw files"):
        adapter.process_quicklook(
            adapter.load(candidate),
            {
                "modes": ("FULL",),
                "raw_min_kb": 1_000_000,
                "static_lat": 50.9,
                "static_lon": 6.4,
                "static_alt": 100,
            },
        )


def test_sif_sza_is_recalculated_from_matched_position():
    frame = pd.DataFrame({
        "datetime [UTC]": ["2026-07-27T09:00:00Z"],
        "Lat": [47.7],
        "Lon": [9.2],
        "SZA": [999.0],
    })
    module = SifAdapter(
        output_root=Path("/private/tmp/sif-sza-test"),
        flight_name="flight",
    ).bridge.module

    result = _refresh_sza_from_position(frame, module)

    assert np.isfinite(result.loc[0, "SZA"])
    assert 0 <= result.loc[0, "SZA"] <= 180
    assert result.loc[0, "SZA"] != 999.0


def test_sif_noseboom_parser_accepts_nanosecond_timestamps():
    value = noseboom_gimbal_for_sif.parse_datetime(
        "2026-07-27 05:19:52.602000000"
    )

    assert value == datetime(
        2026, 7, 27, 5, 19, 52, 602000, tzinfo=timezone.utc
    )


def test_sif_uses_bundled_validated_science_and_essentials():
    assert DEFAULT_SOURCE == BUNDLED_SOURCE
    assert DEFAULT_ESSENTIALS == BUNDLED_ESSENTIALS
    assert BUNDLED_SOURCE.is_file()
    assert (BUNDLED_SOURCE.parent / "noseboom_gimbal_for_sif.py").is_file()
    assert {
        "CAL_FROG_AIRFLOX07_FULL_FZJ_2023-05-31.csv",
        "CAL_FROG_AIRFLOX_FLUO_05FZJ_2023-05-31.csv",
        "Indices_ICOS.txt",
    }.issubset({path.name for path in BUNDLED_ESSENTIALS.iterdir()})


def test_sif_rejects_non_spectral_csv_and_preserves_raw(tmp_path: Path):
    source = tmp_path / "not_sif.csv"
    source.write_text("_time,value\n2026-01-01,1\n")
    adapter = SifAdapter(output_root=tmp_path / "output", flight_name="flight")
    assert adapter.detect(ScanIndex(
        tmp_path, (ScanEntry(source, source.stat().st_size, True),)
    )) == ()
    result = adapter.validate(InputCandidate("sif", (), None, "none"))
    assert result.detection_status is DetectionStatus.FAILED


@requires_sif_recording
def test_sif_airship_time_range_retains_zero_gps_rows_and_uses_record_clock(
    tmp_path: Path,
):
    lines = SOURCE.read_text(
        encoding="utf-8-sig", errors="replace"
    ).splitlines()[:12]
    expected = []
    for row_index in (0, 6):
        values = lines[row_index].split(";")
        values[1] = "260727"
        expected.append(
            datetime.strptime(
                f"{values[1]} {values[2]}", "%y%m%d %H%M%S"
            ).replace(tzinfo=timezone.utc)
        )
        for key, replacement in (
            ("GPS_TIME_UTC=", "000041."),
            ("GPS_date=", "050180"),
            ("GPS_lat=", "0.00000"),
            ("GPS_lon=", "0.00000"),
        ):
            position = values.index(key)
            values[position + 1] = replacement
        lines[row_index] = ";".join(values)
    source = tmp_path / "F_zero_gps.CSV"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    adapter = SifAdapter(output_root=tmp_path / "output", flight_name="flight")
    candidate = InputCandidate("sif", (source,), 1.0, "zero GPS fixture")
    time_range = adapter.extract_time_range(candidate)

    assert time_range.utc_start == min(expected)
    assert time_range.utc_end == max(expected)


class TestTheResultMetadataSurvivesJson:
    """The options block is written to the project, so it must be dumpable.

    Flight_CC0806 ran the whole SIF pipeline - four minutes, both channels,
    every export - and then failed with "Object of type function is not JSON
    serializable" five seconds after "SIF quicklook completed". The terrain
    sampler is a callable the backend passes in options so alt_above_ground_m
    becomes height above ground, and _json_safe returned anything it did not
    recognise unchanged, so the function travelled into json.dump.
    """

    def test_a_callable_option_is_recorded_not_passed_through(self):
        import json

        from instruments.sif.adapter import _json_safe

        def terrain_sampler(latitudes, longitudes):
            return []

        safe = _json_safe({"terrain_sampler": terrain_sampler, "pitch": True})

        json.dumps(safe)
        assert safe["pitch"] is True
        assert "terrain_sampler" in safe["terrain_sampler"]

    def test_the_ordinary_types_are_untouched(self):
        from instruments.sif.adapter import _json_safe

        value = _json_safe(
            {"a": "text", "b": 3, "c": 1.5, "d": True, "e": None, "f": [1, 2]}
        )
        assert value == {"a": "text", "b": 3, "c": 1.5, "d": True, "e": None,
                         "f": [1, 2]}

    def test_anything_else_is_described_rather_than_crashing(self):
        import json

        from instruments.sif.adapter import _json_safe

        class Opaque:
            def __repr__(self):
                return "<opaque>"

        json.dumps(_json_safe({"x": Opaque()}))

    def test_paths_and_times_still_convert(self):
        import json
        from datetime import datetime, timezone
        from pathlib import Path

        from instruments.sif.adapter import _json_safe

        safe = _json_safe({
            "path": Path("a") / "b",
            "when": datetime(2026, 8, 6, tzinfo=timezone.utc),
            "count": np.int64(4),
        })
        json.dumps(safe)
        assert safe["count"] == 4
        assert safe["when"].startswith("2026-08-06")


class TestTheNegativeRetrievalWarningNamesTheCause:
    """iFLD divides by the depth of the oxygen band in the incoming channel.

    A clipped E has no depth left, so the retrieval explodes. That flag is in
    the same table the audit already reads, and Flight_CC0806 sends the
    operator to the calibration date without it. It is a partial cause there -
    71 of 938 negative SIF_A rows - so the message reports the share and leaves
    calibration age as what to check for the rest.
    """

    def _frame(self, saturated, values):
        return pd.DataFrame({
            "SIF_A_ifld [mW m-2nm-1sr-1]": values,
            "sat value E FLUO": saturated,
        })

    def test_saturated_rows_are_counted_among_the_failures(self):
        from instruments.sif.adapter import _sif_retrieval_audit

        audit = _sif_retrieval_audit(
            self._frame([1, 1, 0, 0], [-5.0, -3.0, -1.0, 2.0])
        )
        assert audit["SIF_A_ifld"]["non_positive"] == 3
        assert audit["SIF_A_ifld"]["saturated_incoming"] == 2

    def test_a_saturated_row_with_a_good_retrieval_is_not_counted(self):
        from instruments.sif.adapter import _sif_retrieval_audit

        audit = _sif_retrieval_audit(self._frame([1, 1], [4.0, 5.0]))
        assert audit["SIF_A_ifld"]["non_positive"] == 0

    def test_the_message_names_saturation_and_the_cure(self):
        from instruments.sif.adapter import (
            _sif_retrieval_audit, _sif_retrieval_warnings,
        )

        audit = _sif_retrieval_audit(
            self._frame([1, 1, 0], [-5.0, -3.0, -1.0])
        )
        text = _sif_retrieval_warnings({"FLUO": audit})[0]
        assert "saturated on 2 of them" in text
        assert "integration time" in text

    def test_without_saturation_it_keeps_the_original_advice(self):
        from instruments.sif.adapter import (
            _sif_retrieval_audit, _sif_retrieval_warnings,
        )

        audit = _sif_retrieval_audit(self._frame([0, 0], [-5.0, -3.0]))
        text = _sif_retrieval_warnings({"FLUO": audit})[0]
        assert "calibration age" in text
        assert "saturated on" not in text

    def test_a_delivery_without_the_flag_says_nothing_about_it(self):
        from instruments.sif.adapter import (
            _sif_retrieval_audit, _sif_retrieval_warnings,
        )

        frame = pd.DataFrame({"SIF_A_ifld [mW m-2nm-1sr-1]": [-5.0, -3.0]})
        audit = _sif_retrieval_audit(frame)
        assert audit["SIF_A_ifld"]["saturated_incoming"] is None
        assert "saturated on" not in _sif_retrieval_warnings({"FLUO": audit})[0]

    def test_the_audit_still_survives_an_unreadable_column(self):
        from instruments.sif.adapter import _sif_retrieval_audit

        frame = pd.DataFrame({
            "SIF_A_ifld [mW m-2nm-1sr-1]": [-5.0, -3.0],
            "sat value E FLUO": ["yes", "no"],
        })
        audit = _sif_retrieval_audit(frame)
        assert audit["SIF_A_ifld"]["non_positive"] == 2
        assert audit["SIF_A_ifld"]["saturated_incoming"] == 0
