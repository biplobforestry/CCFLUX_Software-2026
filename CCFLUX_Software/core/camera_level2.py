"""Explicit capability declarations for optional camera Level 2 processing."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Level2Routine:
    routine_id: str
    label: str
    available: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


LEVEL2_CAPABILITIES: dict[str, tuple[Level2Routine, ...]] = {
    "micasense": (
        Level2Routine("radiometric_correction", "Radiometric correction", False,
                      "Validated source exists, but its calibrated metadata/ExifTool input path is not yet integrated."),
        Level2Routine("panel_calibration", "Panel calibration", False,
                      "Validated source requires user-supplied panel ROIs and certified reflectance values."),
        Level2Routine("band_alignment", "Band alignment", False,
                      "No validated band-alignment routine was found in the repository."),
        Level2Routine("reflectance_conversion", "Reflectance conversion", False,
                      "Existing calculation is coupled to panel inputs that are not yet available in the modular application."),
        Level2Routine("vegetation_indices", "Vegetation indices", False,
                      "No validated vegetation-index routine was found in the repository."),
        Level2Routine("georeferencing", "Georeferencing", False,
                      "No validated MicaSense georeferencing routine was found in the repository."),
    ),
    "flir": (
        Level2Routine("radiometric_temperature_conversion", "Radiometric temperature conversion", True,
                      "Uses the unchanged batch calculation in FLIR_Processing_pipeline.py."),
        Level2Routine("frame_temperature_statistics", "Frame-level temperature statistics", True,
                      "Uses the unchanged per-frame statistics in FLIR_Processing_pipeline.py."),
        Level2Routine("temperature_imagery", "Temperature imagery", False,
                      "No validated temperature-imagery export routine was found in the repository."),
        Level2Routine("noseboom_georeferencing", "Georeferencing by matching Noseboom time", True,
                      "Matches each valid FLIR frame statistic to the nearest processed Noseboom UTC navigation sample within 2.5 seconds."),
    ),
}


def level2_capability_snapshot() -> dict[str, list[dict[str, object]]]:
    return {
        instrument_id: [routine.to_dict() for routine in routines]
        for instrument_id, routines in LEVEL2_CAPABILITIES.items()
    }


def validate_level2_selection(instrument_id: str, routine_ids: object) -> tuple[str, ...]:
    if not isinstance(routine_ids, list) or not routine_ids:
        raise ValueError("Select at least one available Level 2 routine")
    if not all(isinstance(value, str) for value in routine_ids):
        raise ValueError("selected_routines must be a list of routine IDs")
    if len(routine_ids) != len(set(routine_ids)):
        raise ValueError("Level 2 routine selection contains duplicates")
    known = {routine.routine_id: routine for routine in LEVEL2_CAPABILITIES.get(instrument_id, ())}
    unknown = [value for value in routine_ids if value not in known]
    if unknown:
        raise ValueError("Unknown Level 2 routine(s): " + ", ".join(unknown))
    unavailable = [known[value].label for value in routine_ids if not known[value].available]
    if unavailable:
        raise ValueError("Unavailable Level 2 routine(s): " + ", ".join(unavailable))
    return tuple(routine_ids)
