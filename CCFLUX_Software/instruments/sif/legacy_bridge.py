"""Lazy immutable bridge to the AirFloX SIF automation module."""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

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
                spec.loader.exec_module(module)
                self._module = module
            finally:
                if sys.path[0] == str(self.source_path.parent):
                    sys.path.pop(0)
        return self._module

    def essentials(self, mode: str):
        return self.module.detect_essential_file(DEFAULT_ESSENTIALS, mode)
