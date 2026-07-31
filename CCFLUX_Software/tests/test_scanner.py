from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from core.configuration import load_detection_configuration
from core.detection_configuration import DetectionConfiguration
from core.scanner import (
    FlightFolderScanner,
    ScanCancellationToken,
    ScanProgress,
)


CONFIG_ROOT = Path(__file__).resolve().parents[1] / "configs"


def production_configuration() -> DetectionConfiguration:
    return load_detection_configuration(
        CONFIG_ROOT / "instrument_detection.yaml",
        CONFIG_ROOT / "file_patterns.yaml",
    )


def configuration_with_test_only_gopro_pattern() -> DetectionConfiguration:
    """Exercise generic camera matching without inventing a production rule."""
    config = production_configuration()
    patterns = dict(config.pattern_sets)
    patterns["gopro"] = replace(
        patterns["gopro"],
        likely_folder_names=("GoPro",),
        file_extensions=(".mp4",),
    )
    return DetectionConfiguration(
        schema_version=config.schema_version,
        rules=config.rules,
        pattern_sets=MappingProxyType(patterns),
    )


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def create_opc(path: Path, instrument_id: str, config: DetectionConfiguration) -> None:
    columns = config.patterns_for(instrument_id).required_csv_columns
    write_text(path, ",".join(columns) + "\n" + ",".join("0" for _ in columns))


def populate_all_instruments(root: Path, config: DetectionConfiguration) -> None:
    flight = root / "20260726_204943"
    write_text(
        flight / "Noseboom" / "influxdb" / "Noseboom.csv",
        "Airflow_UTCcorr_Nanoseconds_ns,TIMESTAMP,WIND_vWind_m/s\n"
        "1,2026-07-26 20:49:43.000,3.0\n",
    )
    write_text(
        flight / "MIRO" / "trace.txt",
        "t-stamp;CO wet;VValve 0\n26.07.2026 20:49:43,000;0,1;0\n",
    )
    write_text(
        flight / "Picarro" / "trace.dat",
        "DATE TIME CO2_sync\n2026-07-26 20:49:43.000 401.0\n",
    )
    hatchbox = flight / "HATCH-BOX"
    create_opc(hatchbox / "OPC_HBX4.csv", "opc_hbx4", config)
    create_opc(hatchbox / "OPC_HBX5.csv", "opc_hbx5", config)
    write_text(
        hatchbox / "partector.csv",
        "_time,time__1,number_1,diam_1,LDSA_1,flow_1\n"
        "2026-07-26T20:49:43Z,1,10,20,30,0.5\n",
    )
    gimbal_columns = config.patterns_for("ins_gimbal").required_csv_columns
    write_text(
        hatchbox / "Gremsy_T3V3_Gimbal.csv",
        ",".join(gimbal_columns)
        + "\n"
        + ",".join(
            "2026-07-26T20:49:43Z" if value == "_time" else "0"
            for value in gimbal_columns
        )
        + "\n",
    )
    write_text(
        hatchbox / "FLOXINSIDE" / "FLOX" / "F001.CSV",
        "1;260726;204943;FROG 2.21c AIRFLOX FULL;GPS_TIME_UTC=;204943\n",
    )
    touch(hatchbox / "MicaSense" / "capture.zip")
    write_text(hatchbox / "FLIR" / "metadata.json", "{}")
    touch(hatchbox / "GoPro" / "GX010001.mp4")


class FlightFolderScannerTests(unittest.TestCase):
    def test_all_instruments_present_with_test_only_gopro_rule(self) -> None:
        config = configuration_with_test_only_gopro_pattern()
        scanner = FlightFolderScanner(config)
        events: list[ScanProgress] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            populate_all_instruments(root, config)
            report = scanner.scan(root, progress_callback=events.append)

        self.assertFalse(report.cancelled)
        self.assertEqual(
            set(report.detected_instrument_ids),
            {
                "noseboom",
                "miro",
                "picarro",
                "opc_hbx4",
                "opc_hbx5",
                "partector",
                "ins_gimbal",
                "sif",
                "micasense",
                "flir",
                "gopro",
            },
        )
        self.assertTrue(any(event.current_folder is not None for event in events))
        self.assertTrue(any(event.current_file is not None for event in events))
        self.assertEqual(events[-1].phase, "complete")
        self.assertEqual(events[-1].progress, 100.0)
        file_events = [event for event in events if event.phase == "scanning_file"]
        self.assertTrue(file_events)
        self.assertTrue(all(event.progress is not None for event in file_events))
        self.assertTrue(all(0.0 < event.progress <= 100.0 for event in file_events))
        self.assertTrue(any(event.phase == "inventory" for event in events))
        self.assertIn("miro", events[-1].detected_candidate_instruments)

    def test_very_large_camera_folder_is_streamed_and_samples_are_capped(self) -> None:
        config = configuration_with_test_only_gopro_pattern()
        scanner = FlightFolderScanner(config, candidate_file_sample_limit=20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            camera = root / "GoPro"
            camera.mkdir()
            for index in range(3000):
                (camera / f"GX{index:06d}.mp4").touch()
            report = scanner.scan(root)

        candidate = next(
            item for item in report.candidates if item.instrument_id == "gopro"
        )
        self.assertEqual(candidate.matching_file_count, 3000)
        self.assertEqual(len(candidate.sample_matching_files), 20)
        self.assertEqual(report.malformed_file_count, 0)

    def test_large_scan_throttles_gui_progress_without_losing_final_count(self) -> None:
        scanner = FlightFolderScanner(
            production_configuration(),
            progress_interval_seconds=60.0,
        )
        events: list[ScanProgress] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(250):
                touch(root / "unrelated" / f"{index:04d}.bin")
            report = scanner.scan(root, progress_callback=events.append)

        file_events = [event for event in events if event.phase == "scanning_file"]
        self.assertEqual(report.files_scanned, 250)
        self.assertEqual([event.files_scanned for event in file_events[:5]], [1, 2, 3, 4, 5])
        self.assertEqual(file_events[-1].files_scanned, 250)
        self.assertLessEqual(len(file_events), 6)
        self.assertEqual(events[-1].phase, "complete")
        self.assertEqual(events[-1].progress, 100.0)

    def test_only_some_instruments_present(self) -> None:
        config = production_configuration()
        scanner = FlightFolderScanner(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_text(
                root / "MIRO" / "one.txt",
                "t-stamp;CO wet\n26.07.2026 20:49:43,000;0,1\n",
            )
            write_text(
                root / "Picarro" / "one.dat",
                "DATE TIME CO2_sync\n2026-07-26 20:49:43 400\n",
            )
            report = scanner.scan(root)
        self.assertEqual(set(report.detected_instrument_ids), {"miro", "picarro"})
        for candidate in report.candidates:
            self.assertEqual(
                len(candidate.all_matching_files), candidate.matching_file_count
            )
        miro = next(
            item for item in report.candidates if item.instrument_id == "miro"
        )
        self.assertFalse(
            any(
                "TDMS" in warning or "requires confirmation" in warning
                for warning in miro.warnings
            )
        )

    def test_all_scientific_segments_are_retained_beyond_sample_limit(self) -> None:
        scanner = FlightFolderScanner(
            production_configuration(), candidate_file_sample_limit=2
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(5):
                write_text(
                    root / "MIRO" / f"segment_{index}.txt",
                    "t-stamp;CO wet\n"
                    f"26.07.2026 20:{40 + index}:43,000;0,1\n",
                )
            report = scanner.scan(root)
        candidate = next(
            item for item in report.candidates if item.instrument_id == "miro"
        )
        self.assertEqual(candidate.matching_file_count, 5)
        self.assertEqual(len(candidate.sample_matching_files), 2)
        self.assertEqual(len(candidate.all_matching_files), 5)

    def test_partector_does_not_claim_other_hatchbox_time_series(self) -> None:
        scanner = FlightFolderScanner(production_configuration())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hatchbox = root / "20260726_204943" / "HATCH-BOX"
            write_text(
                hatchbox / "partector.csv",
                "_time,LDSA\n2026-07-26T10:00:00Z,1\n",
            )
            write_text(
                hatchbox / "Gremsy_T3V3_Gimbal.csv",
                "_time,gimbal_acc_x_counts\n2026-07-26T10:00:00Z,1\n",
            )
            report = scanner.scan(root)
        candidate = next(
            item for item in report.candidates if item.instrument_id == "partector"
        )
        self.assertEqual(candidate.matching_file_count, 1)
        self.assertEqual(
            candidate.all_matching_files,
            ((hatchbox / "partector.csv").resolve(),),
        )

    def test_empty_folder(self) -> None:
        scanner = FlightFolderScanner(production_configuration())
        with tempfile.TemporaryDirectory() as directory:
            report = scanner.scan(Path(directory))
        self.assertEqual(report.files_scanned, 0)
        self.assertEqual(report.candidates, ())
        self.assertFalse(report.cancelled)

    def test_no_instrument_detected(self) -> None:
        scanner = FlightFolderScanner(production_configuration())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_text(root / "unrelated" / "notes.md", "nothing to detect")
            touch(root / "unrelated" / "blob.bin")
            report = scanner.scan(root)
        self.assertEqual(report.files_scanned, 2)
        self.assertEqual(report.detected_instrument_ids, ())

    def test_ambiguous_folders_are_retained(self) -> None:
        scanner = FlightFolderScanner(production_configuration())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for parent, name in (("source_a", "MIRO"), ("source_b", "miro")):
                write_text(
                    root / parent / name / f"{name}.txt",
                    "t-stamp;CO wet\n26.07.2026 20:49:43,000;0,1\n",
                )
            report = scanner.scan(root)

        candidates = [
            item for item in report.candidates if item.instrument_id == "miro"
        ]
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(item.ambiguous for item in candidates))
        self.assertTrue(
            all(
                any("Multiple candidate paths" in warning for warning in item.warnings)
                for item in candidates
            )
        )

    def test_sif_full_and_flox_are_one_complementary_candidate(self) -> None:
        scanner = FlightFolderScanner(production_configuration())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "FLOXINSIDE_260727"
            write_text(
                source / "Full" / "260727" / "F071941.CSV",
                "1;260727;071941;FROG 2.21c AIRFLOX FULL;"
                "GPS_TIME_UTC=;071941\n",
            )
            write_text(
                source / "Flox" / "260727" / "F071939.CSV",
                "1;260727;071939;FROG 2.21c AIRFLOX FLUO;"
                "GPS_TIME_UTC=;071939\n",
            )
            report = scanner.scan(root)

        candidates = [
            item for item in report.candidates if item.instrument_id == "sif"
        ]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].candidate_path, source.resolve())
        self.assertEqual(candidates[0].matching_file_count, 2)
        self.assertFalse(candidates[0].ambiguous)
        self.assertFalse(
            any("requires confirmation" in value for value in candidates[0].warnings)
        )

    def test_flir_json_metadata_does_not_require_exif(self) -> None:
        scanner = FlightFolderScanner(production_configuration())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_text(
                root / "FLIR" / "camera.FLIR_Zeppelin.json",
                '[{"timestamp":{"$date":"2026-07-27T05:24:17Z"},'
                '"raw_stats":{},"calibration":{},"raw":[]}]\n',
            )
            report = scanner.scan(root)

        candidate = next(
            item for item in report.candidates if item.instrument_id == "flir"
        )
        self.assertFalse(
            any("EXIF" in warning for warning in candidate.warnings)
        )

    def test_inaccessible_file_is_reported_without_stopping_scan(self) -> None:
        config = production_configuration()
        scanner = FlightFolderScanner(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "20260726_204943" / "HATCH-BOX" / "OPC_HBX4.csv"
            touch(target)
            original = __import__("core.scanner", fromlist=["_read_bounded"])._read_bounded

            def inaccessible(path: Path, limit: int):
                if path.name == target.name:
                    raise PermissionError("synthetic permission denial")
                return original(path, limit)

            with patch("core.scanner._read_bounded", side_effect=inaccessible):
                report = scanner.scan(root)

        self.assertEqual(report.inaccessible_path_count, 1)
        candidate = next(
            item for item in report.candidates if item.instrument_id == "opc_hbx4"
        )
        self.assertTrue(any("Cannot read file header" in error for error in candidate.errors))

    def test_malformed_csv_is_a_candidate_with_errors(self) -> None:
        scanner = FlightFolderScanner(production_configuration())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_text(
                root / "20260726_204943" / "HATCH-BOX" / "OPC_HBX4.csv",
                '"_time,unterminated\n',
            )
            report = scanner.scan(root)

        self.assertEqual(report.malformed_file_count, 1)
        candidate = next(
            item for item in report.candidates if item.instrument_id == "opc_hbx4"
        )
        self.assertTrue(candidate.errors)
        self.assertTrue(
            any("missing required columns" in error for error in candidate.errors)
        )

    def test_cancelled_scanning_returns_partial_report(self) -> None:
        scanner = FlightFolderScanner(production_configuration())
        token = ScanCancellationToken()
        events: list[ScanProgress] = []

        def cancel_after_two_files(event: ScanProgress) -> None:
            events.append(event)
            if event.files_scanned >= 2:
                token.cancel()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(20):
                write_text(root / "unrelated" / f"{index:02d}.log", "data")
            report = scanner.scan(
                root,
                cancellation=token,
                progress_callback=cancel_after_two_files,
            )

        self.assertTrue(report.cancelled)
        self.assertLess(report.files_scanned, 20)
        self.assertEqual(events[-1].phase, "cancelled")
        self.assertTrue(events[-1].cancelled)


if __name__ == "__main__":
    unittest.main()
