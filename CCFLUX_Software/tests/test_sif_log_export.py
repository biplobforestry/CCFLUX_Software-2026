"""The SIF Log button hands out the telemetry log SIF processing itself uses.

AirFloX spectra carry no usable position on this campaign - the FLOX GPS
frequently never acquires a fix - so SIF builds its geometry by matching Gremsy
gimbal attitude to Noseboom latitude, longitude and altitude. An analysis in R
needs that same file, with the same rows and the same column names; rebuilding
it there would be a second implementation of the campaign's navigation.
"""

from __future__ import annotations

import csv
import importlib.util
import time
from pathlib import Path

import pytest

from app.sif_log_export import (
    LOG_FILENAME_SUFFIX,
    OUTPUT_TIMEZONES,
    SIF_LOG_COLUMNS,
    SifLogExportManager,
    _readable_notes,
    output_timezone_choice,
    shift_time_column,
)
from core.logging_manager import ProcessingLogManager


def test_the_columns_are_the_original_sif_log_columns():
    """Declared twice - here and in the legacy writer - so they must agree."""
    path = (
        Path(__file__).resolve().parents[1]
        / "instruments" / "sif" / "legacy" / "noseboom_gimbal_for_sif.py"
    )
    spec = importlib.util.spec_from_file_location("ccflux_test_sif_nav", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert list(SIF_LOG_COLUMNS) == list(module.SIF_COLUMNS)
    assert list(SIF_LOG_COLUMNS) == [
        "lat", "lon", "alt_above_ground_m", "date_time_utc", "pitch", "roll", "yaw",
    ]


def test_the_filename_is_the_one_discovery_excludes():
    """Detection excludes *_noseboom_gimbal_sif_log.csv so the product this
    writes is never offered back as a Noseboom or Gimbal input."""
    patterns = (
        Path(__file__).resolve().parents[1] / "configs" / "file_patterns.yaml"
    ).read_text(encoding="utf-8")

    assert LOG_FILENAME_SUFFIX == "_noseboom_gimbal_sif_log.csv"
    assert "*_noseboom_gimbal_sif_log.csv" in patterns


class _Bridge:
    """Stands in for the legacy navigation routine."""

    def __init__(self, target: Path, rows: int = 3, chatter: str = ""):
        self.target = target
        self.rows = rows
        self.chatter = chatter
        self.calls: list[dict] = []
        self.module = self

    def prepare_sif_log_from_hatchbox(
        self, flight_root, output_dir, custom_log=None,
        altitude_filter=False, max_position_gap_sec=0.2,
        pitch_from_nadir=False, terrain_sampler=None,
    ):
        self.calls.append({
            "flight_root": Path(flight_root), "output_dir": Path(output_dir),
            "custom_log": custom_log, "altitude_filter": altitude_filter,
            "max_position_gap_sec": max_position_gap_sec,
            "pitch_from_nadir": pitch_from_nadir,
            "terrain_sampler": terrain_sampler,
        })
        if self.chatter:
            print(self.chatter)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        lines = [",".join(SIF_LOG_COLUMNS)]
        for index in range(self.rows):
            lines.append(
                f"51.4075{index},6.9454{index},166.8,2026-08-06 08:18:5{index},"
                f"-84.9,-0.58,0.21"
            )
        self.target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.target


def _run(manager: SifLogExportManager, **kwargs) -> dict:
    manager.start(**kwargs)
    for _ in range(200):
        state = manager.snapshot()
        if state["status"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("the export never finished")


@pytest.fixture
def manager(tmp_path):
    return SifLogExportManager(ProcessingLogManager(tmp_path / "log.jsonl"))


class TestBuildingTheLog:
    def _patch(self, monkeypatch, bridge):
        import instruments.sif.legacy_bridge as legacy

        monkeypatch.setattr(legacy, "LegacySifBridge", lambda *a, **k: bridge)

    def test_it_starts_idle(self, manager):
        state = manager.snapshot()
        assert state["status"] == "idle"
        assert state["files"] == []
        assert list(state["columns"]) == list(SIF_LOG_COLUMNS)

    def test_it_produces_a_downloadable_log(self, monkeypatch, manager, tmp_path):
        out = tmp_path / "exports" / "sif"
        bridge = _Bridge(out / "_combined" / f"Flight_CC0806{LOG_FILENAME_SUFFIX}")
        self._patch(monkeypatch, bridge)

        state = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                     flight_name="Flight_CC0806")

        assert state["status"] == "complete"
        assert state["rows_written"] == 3
        assert list(state["columns"]) == list(SIF_LOG_COLUMNS)
        assert state["reused_existing"] is False
        name = state["files"][0]["name"]
        assert name == f"Flight_CC0806{LOG_FILENAME_SUFFIX}"
        assert state["files"][0]["url"] == f"/api/sif/log/download/{name}"
        # Offered beside the project's other exports, not inside _combined.
        assert manager.file(name) == out / name
        assert manager.file(name).is_file()

    def test_the_options_reach_the_validated_routine(self, monkeypatch, manager, tmp_path):
        out = tmp_path / "out"
        bridge = _Bridge(out / "_combined" / f"F{LOG_FILENAME_SUFFIX}")
        self._patch(monkeypatch, bridge)

        _run(manager, flight_root=tmp_path / "raw", output_dir=out,
             flight_name="F", altitude_filter=True, max_position_gap_seconds=0.5)

        call = bridge.calls[0]
        assert call["altitude_filter"] is True
        assert call["max_position_gap_sec"] == 0.5
        # Never a custom log: the button builds from Noseboom and Gimbal.
        assert call["custom_log"] is None
        # The two campaign conventions, decided against the AirFloX reference.
        assert call["pitch_from_nadir"] is True
        assert callable(call["terrain_sampler"]), (
            "without a terrain sampler alt_above_ground_m stays an ellipsoid "
            "height and the SIF footprint radius is computed from it"
        )

    def test_an_existing_delivery_log_is_reused_and_copied_out(
        self, monkeypatch, manager, tmp_path
    ):
        """The routine returns a log already in the read-only delivery."""
        delivery = tmp_path / "raw" / "HATCH-BOX" / "log_conv_ang.csv"
        bridge = _Bridge(delivery)
        self._patch(monkeypatch, bridge)
        out = tmp_path / "out"

        state = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                     flight_name="F")

        assert state["reused_existing"] is True
        assert "Reused" in state["step"]
        # Copied out; the raw delivery is never served or modified.
        assert (out / "log_conv_ang.csv").is_file()
        assert delivery.is_file()

    def test_the_routines_own_chatter_is_captured_not_printed(
        self, monkeypatch, manager, tmp_path, capsys
    ):
        """It reports on stdout, which no operator ever sees."""
        out = tmp_path / "out"
        bridge = _Bridge(
            out / "_combined" / f"F{LOG_FILENAME_SUFFIX}",
            chatter="warning=Noseboom position was discovered outside HATCH-BOX: X\n"
                    "rows_written=28033\nrows_skipped=0",
        )
        self._patch(monkeypatch, bridge)

        state = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                     flight_name="F")

        assert any("outside HATCH-BOX" in note for note in state["notes"])
        assert any("Rows written: 28033" in note for note in state["notes"])
        assert "rows_written=" not in capsys.readouterr().out

    def test_a_failure_is_reported_not_raised(self, monkeypatch, manager, tmp_path):
        class _Broken:
            module = property(lambda self: (_ for _ in ()).throw(
                FileNotFoundError("No HATCH-BOX folder found")))

        import instruments.sif.legacy_bridge as legacy
        monkeypatch.setattr(legacy, "LegacySifBridge", lambda *a, **k: _Broken())

        state = _run(manager, flight_root=tmp_path / "raw",
                     output_dir=tmp_path / "out", flight_name="F")

        assert state["status"] == "failed"
        assert "HATCH-BOX" in state["error"]

    def test_two_builds_do_not_overlap(self, monkeypatch, manager, tmp_path):
        out = tmp_path / "out"
        bridge = _Bridge(out / "_combined" / f"F{LOG_FILENAME_SUFFIX}", rows=200)
        self._patch(monkeypatch, bridge)
        manager.start(flight_root=tmp_path / "raw", output_dir=out, flight_name="F")
        try:
            with pytest.raises(RuntimeError, match="already being built"):
                manager.start(
                    flight_root=tmp_path / "raw", output_dir=out, flight_name="F"
                )
        finally:
            for _ in range(200):
                if manager.snapshot()["status"] != "running":
                    break
                time.sleep(0.02)

    def test_an_unknown_file_is_refused(self, manager):
        with pytest.raises(ValueError, match="not available"):
            manager.file("somebody_elses.csv")


class TestTheOutputClock:
    """Noseboom and the Gimbal are UTC; the operator may want the SIF clock."""

    def _patch(self, monkeypatch, bridge):
        import instruments.sif.legacy_bridge as legacy

        monkeypatch.setattr(legacy, "LegacySifBridge", lambda *a, **k: bridge)

    def test_utc_is_the_default_and_changes_nothing(self, monkeypatch, manager, tmp_path):
        out = tmp_path / "out"
        bridge = _Bridge(out / "_combined" / f"F{LOG_FILENAME_SUFFIX}")
        self._patch(monkeypatch, bridge)

        state = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                     flight_name="F")

        assert state["output_timezone"] == "utc"
        assert state["time_shift_seconds"] == 0
        rows = list(csv.DictReader(
            manager.file(state["files"][0]["name"]).open(encoding="utf-8-sig")))
        assert rows[0]["date_time_utc"] == "2026-08-06 08:18:50"

    def test_the_local_clock_moves_the_timestamps(self, monkeypatch, manager, tmp_path):
        out = tmp_path / "out"
        bridge = _Bridge(out / "_combined" / f"F{LOG_FILENAME_SUFFIX}")
        self._patch(monkeypatch, bridge)

        state = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                     flight_name="F", output_timezone="cest")

        assert state["output_timezone"] == "cest"
        assert state["time_shift_seconds"] == 7200
        rows = list(csv.DictReader(
            manager.file(state["files"][0]["name"]).open(encoding="utf-8-sig")))
        assert rows[0]["date_time_utc"] == "2026-08-06 10:18:50"
        assert rows[1]["date_time_utc"] == "2026-08-06 10:18:51"

    def test_the_shifted_file_is_named_for_its_clock(self, monkeypatch, manager, tmp_path):
        """A column called date_time_utc that is not UTC must be obvious."""
        out = tmp_path / "out"
        self._patch(monkeypatch, _Bridge(out / "_combined" / f"F{LOG_FILENAME_SUFFIX}"))

        state = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                     flight_name="F", output_timezone="cest")

        assert state["files"][0]["name"].endswith("_CEST.csv")
        assert any("holds CEST" in note for note in state["notes"])
        assert "CEST" in state["step"]

    def test_the_column_names_never_change(self, monkeypatch, manager, tmp_path):
        """The whole point of the export is that the schema matches."""
        out = tmp_path / "out"
        self._patch(monkeypatch, _Bridge(out / "_combined" / f"F{LOG_FILENAME_SUFFIX}"))

        state = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                     flight_name="F", output_timezone="cest")

        assert list(state["columns"]) == list(SIF_LOG_COLUMNS)

    def test_only_the_time_column_is_touched(self, monkeypatch, manager, tmp_path):
        """Positions and angles must not be re-rounded on the way through."""
        out = tmp_path / "out"
        self._patch(monkeypatch, _Bridge(out / "_combined" / f"F{LOG_FILENAME_SUFFIX}"))

        utc = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                   flight_name="F")
        untouched = list(csv.DictReader(
            manager.file(utc["files"][0]["name"]).open(encoding="utf-8-sig")))
        cest = _run(manager, flight_root=tmp_path / "raw", output_dir=out,
                    flight_name="F", output_timezone="cest")
        moved = list(csv.DictReader(
            manager.file(cest["files"][0]["name"]).open(encoding="utf-8-sig")))

        for before, after in zip(untouched, moved):
            for column in SIF_LOG_COLUMNS:
                if column != "date_time_utc":
                    assert before[column] == after[column], column

    def test_an_unknown_clock_is_refused(self, manager, tmp_path):
        with pytest.raises(ValueError, match="must be one of"):
            manager.start(flight_root=tmp_path, output_dir=tmp_path,
                          flight_name="F", output_timezone="pacific")

    @pytest.mark.parametrize("value,expected", [
        ("2026-08-06 08:18:51", "2026-08-06 10:18:51"),
        ("2026-08-06 23:30:00", "2026-08-07 01:30:00"),      # over midnight
        ("2026-08-06 08:18:51.250", "2026-08-06 10:18:51.250"),
    ])
    def test_the_written_format_is_preserved(self, tmp_path, value, expected):
        source = tmp_path / "in.csv"
        source.write_text(
            "lat,date_time_utc\n1.0," + value + "\n", encoding="utf-8"
        )
        target = tmp_path / "out.csv"

        shifted, unreadable = shift_time_column(source, target, 7200)

        assert (shifted, unreadable) == (1, 0)
        assert target.read_text(encoding="utf-8").splitlines()[1] == f"1.0,{expected}"

    def test_an_unreadable_timestamp_is_passed_through_and_counted(self, tmp_path):
        source = tmp_path / "in.csv"
        source.write_text(
            "lat,date_time_utc\n1.0,not-a-time\n2.0,2026-08-06 08:00:00\n",
            encoding="utf-8",
        )
        target = tmp_path / "out.csv"

        shifted, unreadable = shift_time_column(source, target, 7200)

        assert (shifted, unreadable) == (1, 1)
        assert "not-a-time" in target.read_text(encoding="utf-8")

    def test_a_log_without_the_column_is_refused(self, tmp_path):
        source = tmp_path / "in.csv"
        source.write_text("lat,lon\n1.0,2.0\n", encoding="utf-8")

        with pytest.raises(ValueError, match="no date_time_utc column"):
            shift_time_column(source, tmp_path / "out.csv", 7200)


class TestHeightAboveGround:
    """alt_above_ground_m is filled from the INS ellipsoid height.

    SIF turns it straight into the footprint radius (Alt x tan 11.5 deg), so it
    has to mean what its name says. On Flight_CC0806 the ground sits near 165 m
    ellipsoid, so uncorrected the column reported a 34 m footprint for a
    spectrometer standing on the ground.
    """

    def _module(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "instruments" / "sif" / "legacy" / "noseboom_gimbal_for_sif.py"
        )
        spec = importlib.util.spec_from_file_location("ccflux_test_nav_agl", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _log(self, tmp_path, heights):
        path = tmp_path / "log.csv"
        lines = ["lat,lon,alt_above_ground_m,date_time_utc,pitch,roll,yaw"]
        for index, height in enumerate(heights):
            lines.append(
                f"51.4075,6.9454,{height},2026-08-06 08:18:5{index},-84.9,-0.5,0.2"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_the_geoid_separation_and_the_ground_are_both_removed(self, tmp_path):
        module = self._module()
        path = self._log(tmp_path, [166.82, 500.0])

        converted, unchanged, below = module.to_height_above_ground(
            path, 46.43, lambda lats, lons: [120.0] * len(lats)
        )

        assert (converted, unchanged, below) == (2, 0, 0)
        rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
        # 166.82 - 46.43 geoid - 120.0 ground
        assert float(rows[0]["alt_above_ground_m"]) == pytest.approx(0.39, abs=0.01)
        assert float(rows[1]["alt_above_ground_m"]) == pytest.approx(333.57, abs=0.01)

    def test_the_schema_and_every_other_column_survive(self, tmp_path):
        module = self._module()
        path = self._log(tmp_path, [200.0])
        before = list(csv.DictReader(path.open(encoding="utf-8-sig")))[0]

        module.to_height_above_ground(path, 46.0, lambda lats, lons: [50.0])

        after = list(csv.DictReader(path.open(encoding="utf-8-sig")))[0]
        assert list(after.keys()) == list(SIF_LOG_COLUMNS)
        for name in SIF_LOG_COLUMNS:
            if name != "alt_above_ground_m":
                assert before[name] == after[name], name

    def test_a_row_with_no_terrain_keeps_what_it_had(self, tmp_path):
        """A guessed ground is worse than an uncorrected altitude."""
        module = self._module()
        path = self._log(tmp_path, [166.82, 300.0])

        converted, unchanged, _ = module.to_height_above_ground(
            path, 46.43, lambda lats, lons: [float("nan"), 120.0]
        )

        assert (converted, unchanged) == (1, 1)
        rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
        assert float(rows[0]["alt_above_ground_m"]) == pytest.approx(166.82)

    def test_rows_below_the_terrain_raster_are_counted_not_clamped(self, tmp_path):
        """The reference log reaches -6.6 m; a floor of zero invents altitude."""
        module = self._module()
        path = self._log(tmp_path, [160.0])

        converted, _, below = module.to_height_above_ground(
            path, 46.43, lambda lats, lons: [120.0]
        )

        assert (converted, below) == (1, 1)
        rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
        assert float(rows[0]["alt_above_ground_m"]) < 0

    def test_the_separation_is_measured_from_the_receiver_that_reports_both(
        self, tmp_path
    ):
        module = self._module()
        noseboom = tmp_path / "NoseBoom.csv"
        noseboom.write_text(
            "TIMESTAMP,GNSSRecv1_LLHPos_ElipsoidHeight_m,GNSSRecv1_LLHPos_MSLHeight_m\n"
            "2026-08-06T08:00:00Z,177.126,130.696\n"
            "2026-08-06T08:00:01Z,178.000,131.560\n",
            encoding="utf-8",
        )

        assert module.median_geoid_separation(noseboom) == pytest.approx(46.43, abs=0.01)

    def test_a_noseboom_without_sea_level_height_is_reported_not_guessed(
        self, tmp_path
    ):
        module = self._module()
        noseboom = tmp_path / "NoseBoom.csv"
        noseboom.write_text(
            "TIMESTAMP,INS_Filter_LLHPos_ElipsoidHeight_m\n"
            "2026-08-06T08:00:00Z,176.238\n",
            encoding="utf-8",
        )

        assert module.median_geoid_separation(noseboom) is None

    def test_the_correction_is_opt_in(self):
        """Without a sampler the routine behaves exactly as it always did."""
        source = (
            Path(__file__).resolve().parents[1]
            / "instruments" / "sif" / "legacy" / "noseboom_gimbal_for_sif.py"
        ).read_text(encoding="utf-8")
        assert "terrain_sampler=None" in source
        assert "if terrain_sampler is not None:" in source


class TestThePitchConvention:
    """The AirFloX manual's "Log file angle definition":

        pitch  0 = aircraft is level, <0 = towards ground, >0 = to sky

    The Gremsy publishes gimbal pitch relative to the horizon and reads -90 at
    nadir, where it sits for 99.5% of Flight_CC0806, so the raw column would
    describe a Zeppelin diving vertically for the whole flight.
    """

    def _module(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "instruments" / "sif" / "legacy" / "noseboom_gimbal_for_sif.py"
        )
        spec = importlib.util.spec_from_file_location("ccflux_test_nav_pitch", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_nadir_is_level(self):
        module = self._module()
        assert module.NADIR_GIMBAL_PITCH_DEG == -90.0
        assert module.pitch_off_level(-90.0) == pytest.approx(0.0)

    def test_tilting_up_off_nadir_reads_towards_the_sky(self):
        """The gimbal's first Flight_CC0806 sample, 5 deg up off nadir."""
        module = self._module()
        assert module.pitch_off_level(-84.92) == pytest.approx(5.08)

    def test_tilting_past_nadir_reads_towards_the_ground(self):
        module = self._module()
        assert module.pitch_off_level(-90.08) == pytest.approx(-0.08)

    def test_the_campaign_converts_rather_than_inverting(self):
        from app.sif_log_export import PITCH_FROM_NADIR

        assert PITCH_FROM_NADIR is True

    def test_a_flight_at_nadir_comes_out_on_zero(self):
        """A bare sign flip left the column on -90; the 2024 reference is near 0."""
        module = self._module()
        recorded = [-89.93, -89.97, -90.0, -90.02, -90.08]

        converted = [module.pitch_off_level(v) for v in recorded]

        assert max(abs(v) for v in converted) < 1.0
        # Neither the raw column nor an inverted one is anywhere near level.
        assert min(abs(v) for v in recorded) > 89.0
        assert min(abs(-v) for v in recorded) > 89.0

    def test_the_two_pitch_options_are_alternatives(self):
        """Both applied in sequence would undo the reflection's sign change."""
        source = (
            Path(__file__).resolve().parents[1]
            / "instruments" / "sif" / "legacy" / "noseboom_gimbal_for_sif.py"
        ).read_text(encoding="utf-8")
        assert source.count("elif args.invert_pitch and pitch is not None:") == 2
        assert source.count("if args.pitch_from_nadir and pitch is not None:") == 2

    def test_sif_processing_gets_the_same_two_corrections(self):
        """The footprint radius is computed inside the pipeline, not only here."""
        backend = (
            Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
        ).read_text(encoding="utf-8")
        adapter = (
            Path(__file__).resolve().parents[1]
            / "instruments" / "sif" / "adapter.py"
        ).read_text(encoding="utf-8")

        assert '"terrain_sampler": terrain_sampler(' in backend
        assert '"pitch_from_nadir": PITCH_FROM_NADIR' in backend
        assert 'terrain_sampler=options.get("terrain_sampler")' in adapter
        assert 'pitch_from_nadir=bool(options.get("pitch_from_nadir", False))' in adapter

    def test_the_terrain_model_is_the_noseboom_one(self):
        """One DTM, so an altitude here and there mean the same thing."""
        source = (
            Path(__file__).resolve().parents[1] / "app" / "sif_log_export.py"
        ).read_text(encoding="utf-8")
        assert "LegacyNoseboomBridge" in source
        assert "sample_terrarium" in source


def test_the_choice_uses_the_campaigns_own_vocabulary():
    """The same keys the SIF record-clock question already asks with."""
    from core.time_manager import SIF_RECORD_CLOCK_TIMEZONES

    assert set(OUTPUT_TIMEZONES) == set(SIF_RECORD_CLOCK_TIMEZONES)
    assert output_timezone_choice("utc")[1] == 0
    assert output_timezone_choice("cest")[1] == 7200
    assert output_timezone_choice(None)[0] == "utc"


def test_the_dashboard_offers_the_choice():
    script = (
        Path(__file__).resolve().parents[1] / "app" / "assets" / "dashboard.js"
    ).read_text(encoding="utf-8")

    assert "name=\"sifLogTz\"" in script
    assert "output_timezone: sifLogTimezone" in script
    assert "sifLogTimezone = 'utc'" in script
    # It must say what shifting does to a column that keeps its name.
    assert "not UTC" in script


def test_notes_are_read_from_the_routines_key_value_lines():
    notes = _readable_notes(
        "rows_written=28033\nrows_skipped=0\n"
        "warning=Some Gimbal timestamps had no Noseboom sample\n"
        "lat_min=51.4 lat_max=51.6\n"
    )
    assert notes == [
        "Rows written: 28033",
        "Rows skipped: 0",
        "Warning: Some Gimbal timestamps had no Noseboom sample",
    ]


class TestTheBackendRefusesWhatItCannotDo:
    def test_it_needs_a_flight_folder(self, tmp_path):
        from app.scan_backend import DashboardScanBackend

        backend = DashboardScanBackend(tmp_path)
        with pytest.raises(ValueError, match="Select the Flight Folder"):
            backend.start_sif_log_export({})

    def test_it_needs_an_output_folder(self, tmp_path):
        from app.scan_backend import DashboardScanBackend

        backend = DashboardScanBackend(tmp_path)
        raw = tmp_path / "raw"
        raw.mkdir()
        backend._selected_folder = raw
        with pytest.raises(ValueError, match="Output Folder"):
            backend.start_sif_log_export({})


def test_the_routes_and_the_button_are_wired():
    root = Path(__file__).resolve().parents[1]
    server = (root / "app" / "server.py").read_text(encoding="utf-8")
    backend = (root / "app" / "scan_backend.py").read_text(encoding="utf-8")
    markup = (root / "app" / "assets" / "dashboard.html").read_text(encoding="utf-8")
    script = (root / "app" / "assets" / "dashboard.js").read_text(encoding="utf-8")

    assert '"/api/sif/log"' in server
    assert '"/api/sif/log/progress"' in server
    assert '"/api/sif/log/download/"' in server
    for name in ("start_sif_log_export", "sif_log_export_progress",
                 "sif_log_export_file"):
        assert f"def {name}" in backend, name
    assert 'id="sifLogBtn"' in markup
    assert ">SIF Log<" in markup
    assert "getElementById('sifLogBtn')" in script
    # The page states the schema the R analysis will read.
    assert "'alt_above_ground_m', 'date_time_utc'" in script


def test_the_log_travels_with_the_project():
    backend = (
        Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")

    assert "def _save_sif_log_exports" in backend
    assert 'output_locations["sif_log"]' in backend
