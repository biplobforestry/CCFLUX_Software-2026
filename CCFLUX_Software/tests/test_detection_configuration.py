from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.detection_configuration import load_detection_configuration
from core.exceptions import ConfigurationError


CONFIG_ROOT = Path(__file__).resolve().parents[1] / "configs"
RULES_PATH = CONFIG_ROOT / "instrument_detection.yaml"
PATTERNS_PATH = CONFIG_ROOT / "file_patterns.yaml"


class DetectionConfigurationTests(unittest.TestCase):
    def test_loads_valid_project_detection_configuration(self) -> None:
        with self.assertLogs(
            "core.detection_configuration", level="WARNING"
        ) as captured:
            config = load_detection_configuration(RULES_PATH, PATTERNS_PATH)

        self.assertEqual(config.schema_version, 1)
        self.assertEqual(len(config.rules), 11)
        self.assertEqual(
            [rule.instrument_id for rule in config.rules],
            [
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
            ],
        )
        self.assertEqual(
            config.patterns_for("opc_hbx4").filename_prefixes,
            ("OPC_HBX4",),
        )
        self.assertEqual(
            config.patterns_for("picarro").file_extensions, (".dat",)
        )
        self.assertEqual(
            config.patterns_for("micasense").camera_exif_tags,
            (
                "DateTimeOriginal",
                "SubSecTime",
                "GPSLatitude",
                "GPSLongitude",
                "GPSAltitude",
                "ExposureTime",
                "ISOSpeed",
            ),
        )
        self.assertTrue(config.rule_for("micasense").requires_confirmation)
        self.assertFalse(config.rule_for("sif").requires_confirmation)
        self.assertGreaterEqual(len(captured.output), 1)
        self.assertTrue(
            any("Incomplete detection rule" in line for line in captured.output)
        )

    def test_missing_required_configuration_field_is_clear(self) -> None:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        patterns = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
        del patterns["pattern_sets"][0]["file_extensions"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules_file = root / "rules.json"
            patterns_file = root / "patterns.json"
            rules_file.write_text(json.dumps(rules), encoding="utf-8")
            patterns_file.write_text(json.dumps(patterns), encoding="utf-8")
            with self.assertRaisesRegex(
                ConfigurationError, "missing required fields: file_extensions"
            ):
                load_detection_configuration(rules_file, patterns_file)

    def test_invalid_instrument_id_is_rejected(self) -> None:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules["rules"][0]["instrument_id"] = "invented_instrument"

        with tempfile.TemporaryDirectory() as directory:
            rules_file = Path(directory) / "rules.json"
            rules_file.write_text(json.dumps(rules), encoding="utf-8")
            with self.assertRaisesRegex(
                ConfigurationError,
                "invalid instrument_id 'invented_instrument'",
            ):
                load_detection_configuration(rules_file, PATTERNS_PATH)

    def test_duplicate_detection_rules_are_rejected(self) -> None:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules["rules"].append(dict(rules["rules"][0]))

        with tempfile.TemporaryDirectory() as directory:
            rules_file = Path(directory) / "rules.json"
            rules_file.write_text(json.dumps(rules), encoding="utf-8")
            with self.assertRaisesRegex(
                ConfigurationError,
                "Duplicate detection rule for instrument_id: noseboom",
            ):
                load_detection_configuration(rules_file, PATTERNS_PATH)

    def test_duplicate_pattern_sets_are_rejected(self) -> None:
        patterns = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
        patterns["pattern_sets"].append(dict(patterns["pattern_sets"][0]))

        with tempfile.TemporaryDirectory() as directory:
            patterns_file = Path(directory) / "patterns.json"
            patterns_file.write_text(json.dumps(patterns), encoding="utf-8")
            with self.assertRaisesRegex(
                ConfigurationError, "Duplicate detection pattern: noseboom"
            ):
                load_detection_configuration(RULES_PATH, patterns_file)


if __name__ == "__main__":
    unittest.main()
