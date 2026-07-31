"""Central registry of CCFLUX Zeppelin instrument definitions and state.

This module records intended application modules and scheduling metadata only.
It does not import an adapter and does not imply that an instrument processor is
integrated.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from .compat import StrEnum
from threading import RLock

from .enums import DetectionStatus, ProcessingStatus


class PhysicalGroup(StrEnum):
    NOSEBOOM = "NOSEBOOM"
    MIRO_RACK = "MIRO RACK"
    HATCHBOX = "HATCHBOX"


@dataclass(frozen=True, slots=True)
class InstrumentRegistration:
    instrument_id: str
    display_name: str
    physical_group: PhysicalGroup
    expected_module_path: str
    is_fast_scientific_instrument: bool
    is_camera_instrument: bool
    detailed_processing_supported: bool
    default_priority: int
    default_enabled: bool
    detection_status: DetectionStatus = DetectionStatus.NOT_DETECTED
    processing_status: ProcessingStatus = ProcessingStatus.IDLE
    enabled: bool | None = None
    priority: int | None = None
    detailed_processing_priority: int | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id cannot be blank")
        if not self.display_name.strip():
            raise ValueError("display_name cannot be blank")
        if not self.expected_module_path.strip():
            raise ValueError("expected_module_path cannot be blank")
        _validate_priority(self.default_priority)
        if self.priority is not None:
            _validate_priority(self.priority)
        if self.detailed_processing_priority is not None:
            _validate_priority(self.detailed_processing_priority)
            if not self.detailed_processing_supported:
                raise ValueError(
                    "detailed_processing_priority requires detailed support"
                )

    @property
    def effective_enabled(self) -> bool:
        return self.default_enabled if self.enabled is None else self.enabled

    @property
    def effective_priority(self) -> int:
        return self.default_priority if self.priority is None else self.priority


def _validate_priority(priority: int) -> None:
    if isinstance(priority, bool) or priority not in {1, 2, 3}:
        raise ValueError("priority must be 1, 2, or 3")


DEFAULT_INSTRUMENTS: tuple[InstrumentRegistration, ...] = (
    InstrumentRegistration(
        instrument_id="noseboom",
        display_name="Noseboom",
        physical_group=PhysicalGroup.NOSEBOOM,
        expected_module_path="instruments.noseboom",
        is_fast_scientific_instrument=True,
        is_camera_instrument=False,
        detailed_processing_supported=True,
        default_priority=1,
        default_enabled=True,
    ),
    InstrumentRegistration(
        instrument_id="miro",
        display_name="MIRO",
        physical_group=PhysicalGroup.MIRO_RACK,
        expected_module_path="instruments.miro",
        is_fast_scientific_instrument=True,
        is_camera_instrument=False,
        detailed_processing_supported=True,
        default_priority=1,
        default_enabled=True,
    ),
    InstrumentRegistration(
        instrument_id="picarro",
        display_name="Picarro",
        physical_group=PhysicalGroup.MIRO_RACK,
        expected_module_path="instruments.picarro",
        is_fast_scientific_instrument=True,
        is_camera_instrument=False,
        detailed_processing_supported=True,
        default_priority=1,
        default_enabled=True,
    ),
    InstrumentRegistration(
        instrument_id="opc_hbx4",
        display_name="OPC HBX-4",
        physical_group=PhysicalGroup.HATCHBOX,
        expected_module_path="instruments.opc_hbx4",
        is_fast_scientific_instrument=True,
        is_camera_instrument=False,
        detailed_processing_supported=True,
        default_priority=1,
        default_enabled=True,
    ),
    InstrumentRegistration(
        instrument_id="opc_hbx5",
        display_name="OPC HBX-5",
        physical_group=PhysicalGroup.HATCHBOX,
        expected_module_path="instruments.opc_hbx5",
        is_fast_scientific_instrument=True,
        is_camera_instrument=False,
        detailed_processing_supported=True,
        default_priority=1,
        default_enabled=True,
    ),
    InstrumentRegistration(
        instrument_id="partector",
        display_name="Partector Pro",
        physical_group=PhysicalGroup.HATCHBOX,
        expected_module_path="instruments.partector",
        is_fast_scientific_instrument=True,
        is_camera_instrument=False,
        detailed_processing_supported=True,
        default_priority=1,
        default_enabled=True,
    ),
    InstrumentRegistration(
        instrument_id="ins_gimbal",
        display_name="INS Gimbal",
        physical_group=PhysicalGroup.HATCHBOX,
        expected_module_path="instruments.ins_gimbal",
        is_fast_scientific_instrument=True,
        is_camera_instrument=False,
        detailed_processing_supported=False,
        default_priority=1,
        default_enabled=True,
    ),
    InstrumentRegistration(
        instrument_id="sif",
        display_name="SIF",
        physical_group=PhysicalGroup.HATCHBOX,
        expected_module_path="instruments.sif",
        is_fast_scientific_instrument=False,
        is_camera_instrument=False,
        detailed_processing_supported=True,
        default_priority=2,
        default_enabled=True,
    ),
    InstrumentRegistration(
        instrument_id="micasense",
        display_name="MicaSense",
        physical_group=PhysicalGroup.HATCHBOX,
        expected_module_path="instruments.micasense",
        is_fast_scientific_instrument=False,
        is_camera_instrument=True,
        detailed_processing_supported=True,
        default_priority=2,
        default_enabled=False,
        detailed_processing_priority=3,
    ),
    InstrumentRegistration(
        instrument_id="flir",
        display_name="FLIR",
        physical_group=PhysicalGroup.HATCHBOX,
        expected_module_path="instruments.flir",
        is_fast_scientific_instrument=False,
        is_camera_instrument=True,
        detailed_processing_supported=True,
        default_priority=2,
        default_enabled=False,
        detailed_processing_priority=3,
    ),
    InstrumentRegistration(
        instrument_id="gopro",
        display_name="GoPro",
        physical_group=PhysicalGroup.HATCHBOX,
        expected_module_path="instruments.gopro",
        is_fast_scientific_instrument=False,
        is_camera_instrument=True,
        detailed_processing_supported=False,
        default_priority=2,
        default_enabled=False,
    ),
)


class InstrumentRegistry:
    """Thread-safe mutable runtime state over immutable registrations."""

    def __init__(
        self,
        instruments: tuple[InstrumentRegistration, ...] = DEFAULT_INSTRUMENTS,
    ) -> None:
        self._lock = RLock()
        self._instruments: OrderedDict[str, InstrumentRegistration] = OrderedDict()
        for instrument in instruments:
            if instrument.instrument_id in self._instruments:
                raise ValueError(
                    f"Duplicate instrument_id: {instrument.instrument_id}"
                )
            self._instruments[instrument.instrument_id] = instrument

    def list_all(self) -> tuple[InstrumentRegistration, ...]:
        with self._lock:
            return tuple(self._instruments.values())

    def find_by_id(self, instrument_id: str) -> InstrumentRegistration:
        with self._lock:
            try:
                return self._instruments[instrument_id]
            except KeyError as exc:
                raise KeyError(f"Unknown instrument_id: {instrument_id}") from exc

    def update_status(
        self,
        instrument_id: str,
        *,
        detection_status: DetectionStatus | None = None,
        processing_status: ProcessingStatus | None = None,
    ) -> InstrumentRegistration:
        if detection_status is None and processing_status is None:
            raise ValueError("At least one status must be provided")
        if detection_status is not None and not isinstance(
            detection_status, DetectionStatus
        ):
            raise TypeError("detection_status must be DetectionStatus")
        if processing_status is not None and not isinstance(
            processing_status, ProcessingStatus
        ):
            raise TypeError("processing_status must be ProcessingStatus")
        with self._lock:
            current = self.find_by_id(instrument_id)
            updated = replace(
                current,
                detection_status=(
                    current.detection_status
                    if detection_status is None
                    else detection_status
                ),
                processing_status=(
                    current.processing_status
                    if processing_status is None
                    else processing_status
                ),
            )
            self._instruments[instrument_id] = updated
            return updated

    def set_enabled(
        self, instrument_id: str, enabled: bool
    ) -> InstrumentRegistration:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        with self._lock:
            updated = replace(self.find_by_id(instrument_id), enabled=enabled)
            self._instruments[instrument_id] = updated
            return updated

    def enable(self, instrument_id: str) -> InstrumentRegistration:
        return self.set_enabled(instrument_id, True)

    def disable(self, instrument_id: str) -> InstrumentRegistration:
        return self.set_enabled(instrument_id, False)

    def update_priority(
        self, instrument_id: str, priority: int
    ) -> InstrumentRegistration:
        _validate_priority(priority)
        with self._lock:
            updated = replace(self.find_by_id(instrument_id), priority=priority)
            self._instruments[instrument_id] = updated
            return updated

    def grouped_by_physical_system(
        self,
    ) -> dict[PhysicalGroup, tuple[InstrumentRegistration, ...]]:
        with self._lock:
            groups: dict[PhysicalGroup, list[InstrumentRegistration]] = {
                group: [] for group in PhysicalGroup
            }
            for instrument in self._instruments.values():
                groups[instrument.physical_group].append(instrument)
            return {group: tuple(items) for group, items in groups.items()}

    def sorted_by_priority(self) -> tuple[InstrumentRegistration, ...]:
        with self._lock:
            insertion_order = {
                instrument_id: index
                for index, instrument_id in enumerate(self._instruments)
            }
            return tuple(
                sorted(
                    self._instruments.values(),
                    key=lambda item: (
                        item.effective_priority,
                        insertion_order[item.instrument_id],
                    ),
                )
            )
