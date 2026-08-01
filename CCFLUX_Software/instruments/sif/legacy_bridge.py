"""Lazy immutable bridge to the AirFloX SIF automation module."""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

from core.headless_plotting import use_headless_backend
from core.legacy_paths import legacy_integration_path

PACKAGE_ROOT = Path(__file__).resolve().parent
BUNDLED_SOURCE = PACKAGE_ROOT / "legacy" / "airflox_sif_automation.py"
BUNDLED_ESSENTIALS = PACKAGE_ROOT / "essentials"
LEGACY_SCRIPT_DIR = legacy_integration_path("Hatchbox", "SIF", "scripts")
LEGACY_SOURCE = LEGACY_SCRIPT_DIR / "airflox_sif_automation.py"
LEGACY_ESSENTIALS = LEGACY_SCRIPT_DIR.parent / "SIF_Essentials"
DEFAULT_SOURCE = BUNDLED_SOURCE if BUNDLED_SOURCE.is_file() else LEGACY_SOURCE
DEFAULT_ESSENTIALS = (
    BUNDLED_ESSENTIALS
    if BUNDLED_ESSENTIALS.is_dir()
    else LEGACY_ESSENTIALS
)


class LegacySifBridge:
    def __init__(self, source_path: Path = DEFAULT_SOURCE):
        self.source_path = Path(source_path)
        self._module = None

    @property
    def module(self):
        if self._module is None:
            sys.path.insert(0, str(self.source_path.parent))
            try:
                spec = importlib.util.spec_from_file_location("ccflux_legacy_sif", self.source_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                # Legacy modules import pyplot at module scope; pin the
                # non-interactive backend before that happens.
                use_headless_backend()
                spec.loader.exec_module(module)
                self._module = module
            finally:
                if sys.path[0] == str(self.source_path.parent):
                    sys.path.pop(0)
        return self._module

    def essentials(self, mode: str, overrides: dict | None = None):
        """Calibration and index files for one mode.

        The bundled CAL_FROG/Indices_ICOS files are the default, but an operator
        may supply their own: a recalibrated instrument, or a different index
        definition list. ``overrides`` carries ``calibration_full``,
        ``calibration_fluo`` and ``indices_file``; anything absent falls back to
        the bundled file, so a partial override is allowed.
        """
        calibration, indices = self.module.detect_essential_file(
            DEFAULT_ESSENTIALS, mode
        )
        if not overrides:
            return calibration, indices
        key = "calibration_full" if str(mode).upper() == "FULL" else "calibration_fluo"
        chosen = overrides.get(key)
        if chosen:
            candidate = Path(chosen)
            if not candidate.is_file():
                raise FileNotFoundError(
                    f"The selected {mode} calibration file no longer exists: {candidate}"
                )
            calibration = candidate
        chosen_indices = overrides.get("indices_file")
        if chosen_indices:
            candidate = Path(chosen_indices)
            if not candidate.is_file():
                raise FileNotFoundError(
                    f"The selected vegetation-index file no longer exists: {candidate}"
                )
            indices = candidate
        return calibration, indices
