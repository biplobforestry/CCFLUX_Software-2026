"""Lazy, read-only bridge to the existing FLIR Level 1 quick-look code."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

from core.headless_plotting import use_headless_backend
from core.legacy_paths import legacy_integration_path
from types import ModuleType

DEFAULT_LEGACY_PATH = legacy_integration_path("FLIR", "FLIR_Quick_look.py")


class LegacyFlirQuickLookBridge:
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
                        f"Legacy FLIR quick-look source is unavailable: {self.source_path}"
                    )
                spec = importlib.util.spec_from_file_location(
                    "ccflux_legacy_flir_quicklook", self.source_path
                )
                if spec is None or spec.loader is None:
                    raise ImportError(
                        f"Could not load FLIR quick-look source: {self.source_path}"
                    )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                # Legacy modules import pyplot at module scope; pin the
                # non-interactive backend before that happens.
                use_headless_backend()
                spec.loader.exec_module(module)
                self._module = module
            return self._module
