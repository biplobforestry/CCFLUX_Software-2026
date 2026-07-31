"""Lazy immutable bridge to the Gremsy gimbal processor."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from core.headless_plotting import use_headless_backend
from core.legacy_paths import legacy_integration_path

DEFAULT_SOURCE = legacy_integration_path("Hatchbox", "gremsy_full_flight_quicklook.py")


class LegacyInsGimbalBridge:
    def __init__(self, source_path: Path = DEFAULT_SOURCE):
        self.source_path = Path(source_path)
        self._module = None

    @property
    def module(self):
        if self._module is None:
            if not self.source_path.is_file():
                raise FileNotFoundError(self.source_path)
            spec = importlib.util.spec_from_file_location(
                "ccflux_legacy_ins_gimbal", self.source_path
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load INS Gimbal source: {self.source_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            # Legacy modules import pyplot at module scope; pin the
            # non-interactive backend before that happens.
            use_headless_backend()
            spec.loader.exec_module(module)
            self._module = module
        return self._module
