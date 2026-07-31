"""Lazy immutable bridge to the validated OPC-N3 processor."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

from core.headless_plotting import use_headless_backend
from core.legacy_paths import legacy_integration_path
from types import ModuleType

DEFAULT_LEGACY_PATH = legacy_integration_path("Hatchbox", "opc_n3_quicklook.py")


class LegacyOpcBridge:
    _lock = threading.RLock()

    def __init__(
        self,
        source_path: Path = DEFAULT_LEGACY_PATH,
        module: ModuleType | None = None,
    ) -> None:
        self.source_path = Path(source_path)
        self._module = module

    @property
    def module(self) -> ModuleType:
        with self._lock:
            if self._module is None:
                if not self.source_path.is_file():
                    raise FileNotFoundError(
                        f"Legacy OPC source is unavailable: {self.source_path}"
                    )
                spec = importlib.util.spec_from_file_location(
                    "ccflux_legacy_opc_n3", self.source_path
                )
                if spec is None or spec.loader is None:
                    raise ImportError(
                        f"Could not load legacy OPC source: {self.source_path}"
                    )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                # Legacy modules import pyplot at module scope; pin the
                # non-interactive backend before that happens.
                use_headless_backend()
                spec.loader.exec_module(module)
                self._module = module
            return self._module

    def sensor_spec(self, instrument_id: str):
        if instrument_id == "opc_hbx4":
            return self.module.HBX4
        if instrument_id == "opc_hbx5":
            return self.module.HBX5
        raise ValueError(f"Unknown OPC instrument ID: {instrument_id}")

    def required_columns(self, instrument_id: str) -> list[str]:
        return self.module.required_columns(self.sensor_spec(instrument_id))

    def load_sensor(
        self,
        path: Path,
        instrument_id: str,
        gap_seconds: float,
        bin_units: str,
    ):
        return self.module.load_sensor(
            path,
            self.sensor_spec(instrument_id),
            gap_seconds,
            bin_units,
        )
