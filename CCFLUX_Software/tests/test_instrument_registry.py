from __future__ import annotations

import unittest

from core.enums import DetectionStatus, ProcessingStatus
from core.instrument_registry import (
    DEFAULT_INSTRUMENTS,
    InstrumentRegistry,
    PhysicalGroup,
)


EXPECTED_IDS = [
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
]


class InstrumentRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = InstrumentRegistry()

    def test_contains_exactly_the_ten_planned_instruments(self) -> None:
        self.assertEqual(
            [instrument.instrument_id for instrument in self.registry.list_all()],
            EXPECTED_IDS,
        )
        self.assertEqual(len(DEFAULT_INSTRUMENTS), 11)

    def test_default_statuses_do_not_claim_integration(self) -> None:
        for instrument in self.registry.list_all():
            self.assertEqual(
                instrument.detection_status, DetectionStatus.NOT_DETECTED
            )
            self.assertEqual(instrument.processing_status, ProcessingStatus.IDLE)

    def test_find_by_id_returns_registration(self) -> None:
        miro = self.registry.find_by_id("miro")
        self.assertEqual(miro.display_name, "MIRO")
        self.assertEqual(miro.physical_group, PhysicalGroup.MIRO_RACK)
        self.assertEqual(miro.expected_module_path, "instruments.miro")

    def test_unknown_id_raises_descriptive_key_error(self) -> None:
        with self.assertRaisesRegex(KeyError, "Unknown instrument_id"):
            self.registry.find_by_id("unknown")

    def test_update_status_changes_only_requested_status(self) -> None:
        updated = self.registry.update_status(
            "noseboom", detection_status=DetectionStatus.VALIDATING
        )
        self.assertEqual(updated.detection_status, DetectionStatus.VALIDATING)
        self.assertEqual(updated.processing_status, ProcessingStatus.IDLE)

        updated = self.registry.update_status(
            "noseboom", processing_status=ProcessingStatus.QUEUED
        )
        self.assertEqual(updated.detection_status, DetectionStatus.VALIDATING)
        self.assertEqual(updated.processing_status, ProcessingStatus.QUEUED)

    def test_update_status_requires_typed_status(self) -> None:
        with self.assertRaises(TypeError):
            self.registry.update_status(  # type: ignore[arg-type]
                "noseboom", detection_status="ready"
            )
        with self.assertRaises(ValueError):
            self.registry.update_status("noseboom")

    def test_enable_and_disable_override_defaults(self) -> None:
        self.assertFalse(self.registry.find_by_id("micasense").effective_enabled)
        self.assertTrue(self.registry.enable("micasense").effective_enabled)
        self.assertFalse(self.registry.disable("micasense").effective_enabled)

        self.assertTrue(self.registry.find_by_id("noseboom").effective_enabled)
        self.assertFalse(self.registry.disable("noseboom").effective_enabled)

    def test_priority_update_and_validation(self) -> None:
        self.assertEqual(
            self.registry.find_by_id("sif").effective_priority, 2
        )
        self.assertEqual(
            self.registry.update_priority("sif", 1).effective_priority, 1
        )
        with self.assertRaises(ValueError):
            self.registry.update_priority("sif", 0)
        with self.assertRaises(ValueError):
            self.registry.update_priority("sif", 4)

    def test_grouping_matches_physical_systems(self) -> None:
        grouped = self.registry.grouped_by_physical_system()
        self.assertEqual(
            [item.instrument_id for item in grouped[PhysicalGroup.NOSEBOOM]],
            ["noseboom"],
        )
        self.assertEqual(
            [item.instrument_id for item in grouped[PhysicalGroup.MIRO_RACK]],
            ["miro", "picarro"],
        )
        self.assertEqual(
            [item.instrument_id for item in grouped[PhysicalGroup.HATCHBOX]],
            [
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

    def test_sorting_uses_priority_then_registration_order(self) -> None:
        ordered = self.registry.sorted_by_priority()
        self.assertEqual(
            [item.instrument_id for item in ordered],
            EXPECTED_IDS,
        )
        self.registry.update_priority("gopro", 1)
        ordered = self.registry.sorted_by_priority()
        self.assertEqual(
            [item.instrument_id for item in ordered[:8]],
            [
                "noseboom",
                "miro",
                "picarro",
                "opc_hbx4",
                "opc_hbx5",
                "partector",
                "ins_gimbal",
                "gopro",
            ],
        )

    def test_camera_and_detailed_priorities_are_explicit(self) -> None:
        micasense = self.registry.find_by_id("micasense")
        flir = self.registry.find_by_id("flir")
        gopro = self.registry.find_by_id("gopro")
        self.assertTrue(micasense.is_camera_instrument)
        self.assertEqual(micasense.default_priority, 2)
        self.assertEqual(micasense.detailed_processing_priority, 3)
        self.assertEqual(flir.detailed_processing_priority, 3)
        self.assertIsNone(gopro.detailed_processing_priority)
        self.assertFalse(gopro.detailed_processing_supported)

    def test_fast_scientific_set_matches_priority_one_group(self) -> None:
        fast_ids = {
            item.instrument_id
            for item in self.registry.list_all()
            if item.is_fast_scientific_instrument
        }
        self.assertEqual(
            fast_ids,
            {
                "noseboom",
                "miro",
                "picarro",
                "opc_hbx4",
                "opc_hbx5",
                "partector",
                "ins_gimbal",
            },
        )


if __name__ == "__main__":
    unittest.main()
