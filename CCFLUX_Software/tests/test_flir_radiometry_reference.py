"""Teledyne FLIR radiometry tests, carried over with the bundled science.

These are the reference author's own forward/inverse and streaming tests. They
are kept verbatim apart from the import, which now loads the copies bundled in
legacy_integration/FLIR through the same bridge the application uses, so they
fail if that bundled science is edited.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from instruments.flir.level2_bridge import LegacyFlirLevel2Bridge

_bridge = LegacyFlirLevel2Bridge()
_health = _bridge.health
_radiometry = _bridge.radiometry

acquisition_summary = _health.acquisition_summary
inspect_all_headers = _health.inspect_all_headers
load_timestamp_index = _health.load_timestamp_index
object_spans = _health.object_spans
process_one_temperature = _health.process_one_temperature
scan_timestamps = _health.scan_timestamps
write_timestamp_index = getattr(_health, 'write_timestamp_index', None)

CorrectionInputs = _radiometry.CorrectionInputs
atmospheric_transmission = _radiometry.atmospheric_transmission
blackbody_pseudo_radiance = _radiometry.blackbody_pseudo_radiance
counts_to_temperature = _radiometry.counts_to_temperature


CALIBRATION = {
    "R": 23844.2734375,
    "B": 1537.800048828125,
    "F": 1.0499999523162842,
    "J0": 19528,
    "J1": 35.69045639038086,
    "X": 0.7319999933242798,
    "alpha1": 1.2390000136974777e-8,
    "alpha2": 1.1094999585736787e-8,
    "beta1": 0.0031799999997019768,
    "beta2": 0.0031802000012248755,
}


def blackbody_counts(temperature_c: float) -> float:
    radiance = float(
        blackbody_pseudo_radiance(temperature_c + 273.15, CALIBRATION)
    )
    return CALIBRATION["J0"] + CALIBRATION["J1"] * radiance


class RadiometryTest(unittest.TestCase):
    def test_apparent_temperature_inverts_factory_curve(self) -> None:
        target_c = np.array([[0.0, 20.0], [35.0, 80.0]])
        radiance = blackbody_pseudo_radiance(target_c + 273.15, CALIBRATION)
        counts = CALIBRATION["J0"] + CALIBRATION["J1"] * radiance
        recovered, diagnostics = counts_to_temperature(counts, CALIBRATION)
        np.testing.assert_allclose(recovered, target_c, atol=1e-10)
        self.assertEqual(
            diagnostics["method"], "apparent_blackbody_temperature"
        )

    def test_full_correction_recovers_forward_model_temperature(self) -> None:
        inputs = CorrectionInputs(
            emissivity=0.94,
            object_distance_m=12.0,
            atmospheric_temperature_c=18.0,
            reflected_apparent_temperature_c=22.0,
            relative_humidity_percent=63.0,
            external_optics_transmission=0.92,
            external_optics_temperature_c=19.0,
        )
        tau, _ = atmospheric_transmission(inputs, CALIBRATION)
        target_c = 42.0
        object_radiance = float(
            blackbody_pseudo_radiance(target_c + 273.15, CALIBRATION)
        )
        reflected = float(
            blackbody_pseudo_radiance(
                inputs.reflected_apparent_temperature_c + 273.15,
                CALIBRATION,
            )
        )
        atmosphere = float(
            blackbody_pseudo_radiance(
                inputs.atmospheric_temperature_c + 273.15, CALIBRATION
            )
        )
        optics = float(
            blackbody_pseudo_radiance(
                inputs.external_optics_temperature_c + 273.15,
                CALIBRATION,
            )
        )
        k2 = (
            (1 - inputs.emissivity) / inputs.emissivity * reflected
            + (1 - tau) / (inputs.emissivity * tau) * atmosphere
            + (1 - inputs.external_optics_transmission)
            / (
                inputs.emissivity
                * tau
                * inputs.external_optics_transmission
            )
            * optics
        )
        measured_radiance = (
            object_radiance + k2
        ) * inputs.emissivity * tau * inputs.external_optics_transmission
        counts = CALIBRATION["J0"] + CALIBRATION["J1"] * measured_radiance
        recovered, _ = counts_to_temperature(
            np.array([[counts]]), CALIBRATION, inputs
        )
        self.assertAlmostEqual(float(recovered[0, 0]), target_c, places=9)


class StreamingPipelineTest(unittest.TestCase):
    def test_index_health_and_temperature_processing(self) -> None:
        target_counts = round(blackbody_counts(25.0))
        frames = [
            {
                "timestamp": {"$date": "2026-07-15T13:21:11.000Z"},
                "calibration": CALIBRATION,
                "raw_stats": {"min": 0, "max": 0, "mean": 0},
                "raw": [[0] * 4 for _ in range(3)],
            },
            {
                "timestamp": {"$date": "2026-07-15T13:21:13.000Z"},
                "calibration": CALIBRATION,
                "raw_stats": {
                    "min": target_counts,
                    "max": target_counts,
                    "mean": target_counts,
                },
                "raw": [[target_counts] * 4 for _ in range(3)],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.FLIR_Zeppelin.json"
            path.write_text(json.dumps(frames, indent=2), encoding="utf-8")
            entries, _ = scan_timestamps([path], workers=1, chunk_mb=1)
            index_path = Path(directory) / "timestamp_index.csv"
            write_timestamp_index(index_path, entries)
            cached_entries = load_timestamp_index(index_path)
            health, _ = inspect_all_headers(entries, workers=1)
            spans = object_spans(entries)
            result = process_one_temperature(
                (2, health[1], entries[1], spans[1]),
                correction=None,
                roi=None,
                save_directory=None,
                valid_range=None,
            )
            summary, gaps = acquisition_summary(
                health, expected_rate_hz=0.5, gap_seconds=2.5
            )

        self.assertEqual(len(entries), 2)
        self.assertEqual(cached_entries, entries)
        self.assertTrue(health[0]["raw_all_zero"])
        self.assertFalse(health[1]["raw_all_zero"])
        self.assertEqual(result["temperature_status"], "FAIL")
        self.assertEqual(result["dimension_status"], "FAIL")
        self.assertAlmostEqual(
            result["temperature_c_mean"], 25.0, delta=0.02
        )
        self.assertEqual(summary["all_zero_frame_count"], 1)
        self.assertEqual(gaps, [])


if __name__ == "__main__":
    unittest.main()
