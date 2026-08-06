"""The operator declares what the AirFloX record clock is set to.

FLOX and FULL write campaign local time, and their GPS often never locks, so
nothing in the files says how far that is from UTC. Deriving it from an
unlocked receiver shifted a whole flight by two hours; the declaration is now
the only source, and it is asked for as soon as the scan finds SIF.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "instruments" / "sif" / "legacy"))
import airflox_sif_automation as afx  # noqa: E402

from app.scan_backend import (  # noqa: E402
    SIF_RECORD_CLOCK_TIMEZONES,
    DashboardScanBackend,
)
from core.enums import DetectionStatus  # noqa: E402
from core.logging_manager import ProcessingLogManager  # noqa: E402
from core.scanner import InstrumentCandidate, ScanReport  # noqa: E402
from core.time_extraction import TimestampExtractor  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"

# Two cycles of a raw FLOX file: cycle id, record date, record time, then the
# spectral rows the reader skips. 09:30:00 local, so CEST makes it 07:30 UTC.
RAW_SIF = (
    "1;260803;093000;auto_mode;IT_WR[us]=;10000\n"
    "WR;0;0;0\n"
    "VEG;12;13;14\n"
    "2;260803;093030;auto_mode;IT_WR[us]=;10000\n"
    "WR;0;0;0\n"
    "VEG;15;16;17\n"
)


def _sif_backend(tmp_path: Path) -> tuple[DashboardScanBackend, Path]:
    """A backend whose scan has already found one raw SIF file."""
    raw = tmp_path / "FLOXINSIDE_260803"
    raw.mkdir()
    source = raw / "093000.CSV"
    source.write_text(RAW_SIF, encoding="utf-8")
    backend = DashboardScanBackend(
        Path(__file__).resolve().parents[1],
        logger=ProcessingLogManager(tmp_path / "application-log.jsonl"),
    )
    candidate = InstrumentCandidate(
        instrument_id="sif",
        candidate_path=raw,
        matched_rules=("test",),
        confidence_score=1.0,
        matching_file_count=1,
        sample_matching_files=(source,),
        warnings=(),
        errors=(),
        matching_files=(source,),
    )
    backend._report = ScanReport(
        root=raw,
        candidates=(candidate,),
        files_scanned=1,
        folders_scanned=1,
        inaccessible_path_count=0,
        malformed_file_count=0,
        warnings=(),
        errors=(),
        cancelled=False,
    )
    state = backend._instruments["sif"]
    state.detection_status = DetectionStatus.READY
    # What detection records before the operator has answered: the raw record
    # clock read as UTC.
    result = TimestampExtractor().extract_instrument("sif", (source,))
    state.utc_start_time = result.utc_start_time
    state.utc_end_time = result.utc_end_time
    state.coverage_segments = list(result.coverage_segments)
    return backend, source


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

    def test_the_declaration_travels_with_the_sif_options(self):
        """It used to be pushed into a module the reader never used.

        `from instruments.sif.legacy import airflox_sif_automation` raises
        ImportError - the file imports a sibling that is only importable with
        the legacy directory on sys.path - so the push was silently skipped.
        Even had it succeeded, LegacySifBridge executes that same file from
        source as `ccflux_legacy_sif`, so the global would have been set on a
        different module object. The options the adapter already receives carry
        the declaration instead.
        """
        backend = (Path(__file__).resolve().parents[1] / "app" / "scan_backend.py").read_text(encoding="utf-8")
        assert "_apply_sif_timezone" not in backend
        task = backend[backend.index("def _sif_task("):]
        assert "**sif_options," in task[:2500]

    def test_the_adapter_applies_it_to_the_reader_it_owns(self):
        adapter = (
            Path(__file__).resolve().parents[1] / "instruments" / "sif" / "adapter.py"
        ).read_text(encoding="utf-8")
        assert "self.bridge.module.RECORD_CLOCK_TIMEZONE.update(" in adapter
        process = adapter[adapter.index("def process_quicklook("):]
        assert "self._declare_record_clock(" in process[:600]


class TestDetectionHonoursTheDeclaration:
    """Detection used to read the raw record clock as UTC whatever was declared.

    The card then reported one window while processing wrote another, so an
    interval chosen against the card covered the wrong part of the record. On
    Flight_CCT0803 that showed as 07:19-16:54 detected and a 11:20-13:50
    selection carrying under a fifth of the data it should have.
    """

    def _range(self, path: Path, offset):
        result = TimestampExtractor(
            sif_record_clock_offset_seconds=offset
        ).extract_instrument("sif", (path,))
        return result

    def test_an_undeclared_record_clock_is_still_read_as_utc(self, tmp_path):
        source = tmp_path / "093000.CSV"
        source.write_text(RAW_SIF, encoding="utf-8")
        assert self._range(source, None).utc_start_time == datetime(
            2026, 8, 3, 9, 30, tzinfo=timezone.utc
        )

    def test_a_declared_cest_clock_becomes_utc(self, tmp_path):
        source = tmp_path / "093000.CSV"
        source.write_text(RAW_SIF, encoding="utf-8")
        result = self._range(source, 7200.0)
        assert result.utc_start_time == datetime(
            2026, 8, 3, 7, 30, tzinfo=timezone.utc
        )
        assert result.utc_end_time == datetime(
            2026, 8, 3, 7, 30, 30, tzinfo=timezone.utc
        )

    def test_a_declared_utc_clock_is_unchanged(self, tmp_path):
        source = tmp_path / "093000.CSV"
        source.write_text(RAW_SIF, encoding="utf-8")
        assert self._range(source, 0.0).utc_start_time == datetime(
            2026, 8, 3, 9, 30, tzinfo=timezone.utc
        )

    def test_the_declaration_is_named_in_the_timezone_summary(self, tmp_path):
        source = tmp_path / "093000.CSV"
        source.write_text(RAW_SIF, encoding="utf-8")
        assert "UTC+2" in self._range(source, 7200.0).timezone_information

    def test_coverage_segments_move_with_the_declaration(self, tmp_path):
        source = tmp_path / "093000.CSV"
        source.write_text(RAW_SIF, encoding="utf-8")
        raw = self._range(source, None).coverage_segments
        declared = self._range(source, 7200.0).coverage_segments
        assert raw and declared
        assert declared[0][0] == raw[0][0].replace(hour=7)

    def test_a_processed_file_already_in_utc_is_not_shifted(self, tmp_path):
        """The declaration describes the raw record clock, nothing else."""
        source = tmp_path / "processed.csv"
        source.write_text(
            "datetime [UTC];value\n2026-08-03 09:30:00;1\n", encoding="utf-8"
        )
        assert self._range(source, 7200.0).utc_start_time == datetime(
            2026, 8, 3, 9, 30, tzinfo=timezone.utc
        )


class TestTheDeclarationSurvives:
    def test_answering_corrects_the_card_that_was_filled_in_before_it(
        self, tmp_path
    ):
        backend, _ = _sif_backend(tmp_path)
        before = backend._instruments["sif"].utc_start_time
        assert before == datetime(2026, 8, 3, 9, 30, tzinfo=timezone.utc)
        backend.set_sif_timezone("cest")
        after = backend._instruments["sif"].utc_start_time
        assert after == datetime(2026, 8, 3, 7, 30, tzinfo=timezone.utc)

    def test_a_rescan_reads_sif_under_the_declaration(self, tmp_path):
        """The scan asks once; every later scan must still honour the answer."""
        backend, source = _sif_backend(tmp_path)
        backend.set_sif_timezone("cest")
        extractor = backend._timestamp_extractor()
        assert extractor.extract_instrument(
            "sif", (source,)
        ).utc_start_time == datetime(2026, 8, 3, 7, 30, tzinfo=timezone.utc)

    def test_delivery_selection_uses_the_same_declaration(self, tmp_path):
        backend, source = _sif_backend(tmp_path)
        backend.set_sif_timezone("cest")
        # 07:00-08:00 UTC holds the declared record, not the raw 09:30.
        kept = backend._deliveries_for_interval(
            "sif",
            (source, source.with_name("other.CSV")),
            datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc),
        )
        assert source in kept

    def test_the_answer_is_written_to_the_open_project(self, tmp_path):
        """A reopened project must restore the answer, so it has to be saved."""
        from core.flight_project import FlightProject

        backend, _ = _sif_backend(tmp_path)
        backend._flight_project = FlightProject(
            flight_id="Flight_CCT0803",
            flight_folder_path=tmp_path,
            output_folder_path=tmp_path / "out",
        )
        backend.set_sif_timezone("cest")
        saved = backend._flight_project.instrument_options["sif"]
        assert saved["record_clock_timezone"] == "cest"

    def test_scanning_a_different_flight_asks_again(self, tmp_path):
        """The record clock is a property of one flight, not of the session."""
        backend, _ = _sif_backend(tmp_path)
        first, second = tmp_path / "Flight_A", tmp_path / "Flight_B"
        first.mkdir()
        second.mkdir()
        backend.start_scan(first, include_camera=False)
        backend._worker.join(timeout=10)
        backend.set_sif_timezone("cest")
        assert backend._sif_record_clock_offset_seconds() == 7200.0

        backend.start_scan(second, include_camera=False)
        backend._worker.join(timeout=10)
        assert backend._sif_options["record_clock_timezone"] is None
        assert backend._sif_record_clock_offset_seconds() is None

    def test_rescanning_the_same_flight_keeps_the_answer(self, tmp_path):
        backend, _ = _sif_backend(tmp_path)
        raw = tmp_path / "Flight_A"
        raw.mkdir()
        backend.start_scan(raw, include_camera=False)
        backend._worker.join(timeout=10)
        backend.set_sif_timezone("cest")
        backend.start_scan(raw, include_camera=False)
        backend._worker.join(timeout=10)
        assert backend._sif_record_clock_offset_seconds() == 7200.0

    def test_an_undeclared_flight_reports_that_it_is_undeclared(self, tmp_path):
        backend, _ = _sif_backend(tmp_path)
        prompt = backend.sif_timezone_prompt()
        assert prompt["required"] is True
        assert prompt["chosen"] is None


class TestReapplicationOnRestore:
    backend_source = (
        Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")

    def _body(self, name: str) -> str:
        start = self.backend_source.index(f"def {name}(")
        end = self.backend_source.find("\n    def ", start + 1)
        return self.backend_source[start:end if end != -1 else None]

    def test_answering_rereads_the_sif_coverage(self):
        assert "self._revalidate_sif_times()" in self._body("set_sif_timezone")

    def test_the_answer_is_persisted_for_a_reload(self):
        assert 'instrument_options["sif"]' in self._body("set_sif_timezone")

    def test_every_extractor_is_built_through_the_declaring_helper(self):
        """A bare TimestampExtractor() in the backend would read SIF as UTC."""
        body = self.backend_source[self.backend_source.index("class DashboardScanBackend"):]
        assert "TimestampExtractor()" not in body

    def test_a_reloaded_project_restores_the_declaration(self, tmp_path):
        from core.flight_project import FlightProject, FlightProjectStore

        raw = tmp_path / "raw"
        raw.mkdir()
        project = FlightProject(
            flight_id="Flight_CCT0803",
            flight_folder_path=raw,
            output_folder_path=tmp_path / "out",
            instrument_options={"sif": {"record_clock_timezone": "cest"}},
        )
        store = FlightProjectStore()
        saved = store.save_project(project, overwrite=True)
        reopened = store.open_project(Path(saved)).project
        assert (
            reopened.instrument_options["sif"]["record_clock_timezone"] == "cest"
        )


class TestPromptIsNotLatchedBeforeTheAnswer:
    script = (ASSETS / "dashboard.js").read_text(encoding="utf-8")

    def test_the_flag_is_set_only_after_the_server_accepts(self):
        """Latching first meant a dismissed dialog was never shown again."""
        post = self.script.index("'/api/sif/timezone', {")
        assert "sifTimezoneAsked = true;" in self.script[post:post + 400]

    def test_it_is_cleared_for_each_scan(self):
        polling = self.script.index("function startPolling()")
        assert "sifTimezoneAsked = false;" in self.script[polling:polling + 400]

    def test_the_card_is_refreshed_once_the_answer_is_stored(self):
        post = self.script.index("'/api/sif/timezone', {")
        assert "await pollScan();" in self.script[post:post + 600]


class TestAChannelWithNoPositionUsesTheDeclaration:
    """Flight_CCT0803 failed outright with "time filter removed all rows".

    Neither AirFloX channel ever reported a position, but a few rows carried a
    GPS time only two hours from the record clock - the CEST offset being
    resolved. _gps_is_unusable compares timestamps with a one-day threshold, so
    those rows counted as agreeing, its unanimity test failed, and the file was
    judged to have a usable GPS. The 2080-01-05 power-on dates then reached the
    time filter, which discarded every row of the flight. On a wider interval the
    same defect showed as FLUO keeping 10 rows where 366 were in range.
    """

    def test_no_positional_fix_means_the_gps_is_not_trusted(self):
        source = Path(afx.__file__).read_text(encoding="utf-8")
        block = source[source.index("offset,fixes,spread=measure_record_clock_offset"):]
        assert "_gps_is_unusable(utc,record_clock) or fixes==0" in block[:900]

    def test_the_declaration_still_wins_over_a_measured_hint(self):
        source = Path(afx.__file__).read_text(encoding="utf-8")
        block = source[source.index("if _gps_is_unusable(utc,record_clock) or fixes==0"):]
        declared = block.index("RECORD_CLOCK_TIMEZONE.get('offset_seconds')")
        hint = block.index("RECORD_CLOCK_OFFSET_HINT.get('seconds')")
        assert declared < hint

    def test_a_channel_that_did_lock_is_left_alone(self):
        """fixes>0 keeps the GPS path, so a good flight is unaffected."""
        source = Path(afx.__file__).read_text(encoding="utf-8")
        assert "fixes==0" in source
        assert "RECORD_CLOCK_OFFSET_HINT['seconds']=offset" in source


class TestASkippedBlockDoesNotKillTheChannel:
    """solar() calls timetuple() on whatever it is given.

    A record clock with an unreadable block leaves NaT. The GPS-derived path
    already backfilled from the neighbour; the declared-clock path returned
    early and did not, so declaring the clock turned a skipped block into
    "NaTType does not support timetuple" and lost the whole channel.
    """

    def test_a_missing_time_inherits_the_previous_one(self):
        import pandas as pd

        values = [
            datetime(2026, 8, 3, 11, 30),
            pd.NaT,
            datetime(2026, 8, 3, 11, 32),
        ]
        filled = afx._backfill_missing_times(values)
        assert filled[1] == datetime(2026, 8, 3, 11, 30)

    def test_a_leading_gap_takes_the_first_good_time(self):
        """There is no earlier value to inherit, and 1970 is not the flight."""
        import pandas as pd

        filled = afx._backfill_missing_times(
            [pd.NaT, pd.NaT, datetime(2026, 8, 3, 11, 30)]
        )
        assert filled[0] == datetime(2026, 8, 3, 11, 30)
        assert filled[1] == datetime(2026, 8, 3, 11, 30)

    def test_all_missing_stays_missing_rather_than_inventing_a_time(self):
        import pandas as pd

        filled = afx._backfill_missing_times([pd.NaT, pd.NaT])
        assert all(pd.isna(value) for value in filled)

    def test_good_times_are_untouched(self):
        values = [datetime(2026, 8, 3, 11, 30), datetime(2026, 8, 3, 11, 31)]
        assert afx._backfill_missing_times(values) == values

    def test_every_declared_clock_return_backfills(self):
        source = Path(afx.__file__).read_text(encoding="utf-8")
        block = source[source.index("if _gps_is_unusable(utc,record_clock) or fixes==0"):]
        head = block[:block.index("if offset is not None:")]
        assert head.count("_backfill_missing_times(") == 3


class TestAFailedRetrievalIsReportedNotFatal:
    """Fluorescence is emitted, so iFLD at or below zero did not work.

    Flight_CCT0803 returns negative SIF_A on 307 of 311 FLUO rows, to
    -697 mW m-2 nm-1 sr-1. Those rows are reported and left out of the
    evaluation and the plots; everything else in them is computed independently
    and stays, and the flight is never failed over it.
    """

    def _frame(self, values_a, values_b=None):
        import pandas as pd

        from instruments.sif.adapter import SIF_RETRIEVAL_COLUMNS

        data = {SIF_RETRIEVAL_COLUMNS[0]: values_a}
        if values_b is not None:
            data[SIF_RETRIEVAL_COLUMNS[1]] = values_b
        return pd.DataFrame(data)

    def test_negatives_are_counted_with_their_floor(self):
        from instruments.sif.adapter import _sif_retrieval_audit

        audit = _sif_retrieval_audit(self._frame([-697.2, -1.0, 0.5, 0.2]))
        entry = audit["SIF_A_ifld"]
        assert entry["available"] is True
        assert entry["rows"] == 4
        assert entry["non_positive"] == 2
        assert entry["minimum"] == pytest.approx(-697.2)

    def test_zero_counts_as_a_failed_retrieval(self):
        from instruments.sif.adapter import _sif_retrieval_audit

        assert _sif_retrieval_audit(self._frame([0.0, 1.0]))["SIF_A_ifld"]["non_positive"] == 1

    def test_a_mode_without_the_columns_is_not_applicable(self):
        """FULL carries no iFLD at all, and must not be reported as failing."""
        import pandas as pd

        from instruments.sif.adapter import _sif_retrieval_audit

        audit = _sif_retrieval_audit(pd.DataFrame({"NDVI": [0.4, 0.5]}))
        assert audit["SIF_A_ifld"] == {"available": False}

    def test_only_the_failed_rows_are_excluded(self):
        from instruments.sif.adapter import _positive_sif_mask

        mask = _positive_sif_mask(self._frame([-1.0, 0.5, 0.0, 2.0]))
        assert list(mask) == [False, True, False, True]

    def test_a_row_with_no_retrieval_at_all_is_kept(self):
        """Missing is not failed; FULL rows must survive the same mask."""
        import numpy as np

        from instruments.sif.adapter import _positive_sif_mask

        mask = _positive_sif_mask(self._frame([np.nan, 1.0]))
        assert list(mask) == [True, True]

    def test_both_channels_must_be_positive(self):
        from instruments.sif.adapter import _positive_sif_mask

        mask = _positive_sif_mask(self._frame([1.0, 1.0], [1.0, -3.0]))
        assert list(mask) == [True, False]

    def test_the_warning_names_the_counts_and_the_reason(self):
        from instruments.sif.adapter import _sif_retrieval_warnings

        messages = _sif_retrieval_warnings({
            "FLUO": {"SIF_A_ifld": {"available": True, "rows": 311,
                                    "non_positive": 307, "minimum": -697.2284}}
        })
        assert len(messages) == 1
        assert "307 of 311" in messages[0]
        assert "-697.228" in messages[0]
        assert "did not work" in messages[0]
        assert "left out of the evaluation" in messages[0]

    def test_nothing_is_warned_about_when_every_retrieval_worked(self):
        from instruments.sif.adapter import _sif_retrieval_warnings

        assert _sif_retrieval_warnings({
            "FLUO": {"SIF_A_ifld": {"available": True, "rows": 10,
                                    "non_positive": 0, "minimum": 0.4}}
        }) == []

    def test_the_audit_never_raises_on_odd_input(self):
        """No shape of this data is worth losing a flight over."""
        from instruments.sif.adapter import _positive_sif_mask, _sif_retrieval_audit

        for odd in (None, object()):
            assert _sif_retrieval_audit(odd)["SIF_A_ifld"] == {"available": False}
        assert len(_positive_sif_mask(self._frame([1.0]))) == 1

    def test_the_flight_is_not_failed_over_it(self):
        adapter = (
            Path(__file__).resolve().parents[1] / "instruments" / "sif" / "adapter.py"
        ).read_text(encoding="utf-8")
        for name in ("_sif_retrieval_audit", "_positive_sif_mask",
                     "_sif_retrieval_warnings"):
            start = adapter.index(f"def {name}(")
            end = adapter.index("\ndef ", start + 1)
            statements = [
                line.strip() for line in adapter[start:end].splitlines()
                if line.strip().startswith("raise ")
            ]
            assert not statements, (name, statements)

    def test_the_exported_values_are_left_alone(self):
        """The CSV is the scientific record; only the plots skip a bad row."""
        adapter = (
            Path(__file__).resolve().parents[1] / "instruments" / "sif" / "adapter.py"
        ).read_text(encoding="utf-8")
        assert "exported CSV keeps every value" in adapter

    def test_the_page_says_how_many_were_left_out(self):
        script = (ASSETS / "sif.js").read_text(encoding="utf-8")
        assert "non-positive retrieval(s) left out" in script
        assert "sif_retrieval_audit" in script
