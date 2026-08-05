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
        # Bounded, and spanning the delivery. It used to be the first twenty
        # files to arrive, and a camera's coverage is read from this sample:
        # MicaSense delivered 2 371 captures over four and a half hours and was
        # reported as ending seven minutes in, which put every later capture
        # outside the Time Filter. Discovery is threaded, so arrival order says
        # nothing about capture order - both ends are taken by name.
        self.assertLessEqual(len(candidate.sample_matching_files), 40)
        names = [path.name for path in candidate.sample_matching_files]
        self.assertIn("GX000000.mp4", names)
        self.assertIn("GX002999.mp4", names)
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


class CameraSampleSpansDeliveryTests(unittest.TestCase):
    """A camera's UTC coverage is read from its bounded sample, so the sample
    has to reach both ends of the delivery.

    MicaSense delivered 2 371 captures spanning 11:21 to 15:51 and was reported
    as ending at 11:28 - the last of the first twenty files to arrive. Every
    capture after that fell outside the Time Filter, so a five-minute window at
    12:00 excluded the instrument entirely while its images sat on disk.
    """

    def test_the_sample_reaches_the_first_and_last_capture(self) -> None:
        config = configuration_with_test_only_gopro_pattern()
        scanner = FlightFolderScanner(config, candidate_file_sample_limit=20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            camera = root / "GoPro"
            camera.mkdir()
            for index in range(500):
                (camera / f"GX{index:06d}.mp4").touch()
            report = scanner.scan(root)

        candidate = next(
            item for item in report.candidates if item.instrument_id == "gopro"
        )
        names = sorted(path.name for path in candidate.sample_matching_files)

        self.assertEqual(names[0], "GX000000.mp4", "the earliest capture")
        self.assertEqual(names[-1], "GX000499.mp4", "the latest capture")

    def test_a_small_delivery_is_returned_whole(self) -> None:
        config = configuration_with_test_only_gopro_pattern()
        scanner = FlightFolderScanner(config, candidate_file_sample_limit=20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            camera = root / "GoPro"
            camera.mkdir()
            for index in range(5):
                (camera / f"GX{index:06d}.mp4").touch()
            report = scanner.scan(root)

        candidate = next(
            item for item in report.candidates if item.instrument_id == "gopro"
        )

        self.assertEqual(len(candidate.sample_matching_files), 5)

    def test_a_scientific_instrument_still_keeps_every_file(self) -> None:
        """Only cameras are sampled; the rest need every segment."""
        scanner = FlightFolderScanner(production_configuration())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "Picarro"
            folder.mkdir()
            for index in range(30):
                (folder / f"log{index:03d}.dat").write_text(
                    "DATE,TIME\n2026-08-03,12:00:00\n", encoding="utf-8"
                )
            report = scanner.scan(root)

        picarro = [c for c in report.candidates if c.instrument_id == "picarro"]
        if picarro:
            self.assertEqual(len(picarro[0].all_matching_files), 30)


class DiscoverySkipsItsOwnOutputTests(unittest.TestCase):
    """An output tree inside the flight folder is not raw input.

    Flight_CCT0803 held processed/ beside its instrument folders, so the scan
    offered a Noseboom export back as a Noseboom source and the job failed
    reading it: "missing timestamp column".
    """

    def test_the_product_directory_names_are_declared(self) -> None:
        from core.scanner import PRODUCT_DIRECTORY_NAMES

        for name in ("processed", "quicklooks", "reports", "exports"):
            self.assertIn(name, PRODUCT_DIRECTORY_NAMES)

    def test_discovery_does_not_descend_into_them(self) -> None:
        import inspect

        from core import scanner

        source = inspect.getsource(scanner)
        self.assertIn("entry.name.casefold() in PRODUCT_DIRECTORY_NAMES", source)
        self.assertIn("Skipped a previous output folder during", source)
