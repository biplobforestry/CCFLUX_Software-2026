"""Lazy bridge to the validated FLIR radiometric temperature science.

``flir_radiometry.py`` implements Teledyne FLIR's reference ``counts2temp``
calculation, and ``flir_health_temperature.py`` streams a multi-gigabyte export
without loading it. Both are bundled unchanged; this module only loads them and
exposes the pieces Level 2 needs, so the CLI in the reference is never invoked.

Official equation:
https://flir.custhelp.com/app/answers/detail/a_id/3321/
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

from core.headless_plotting import use_headless_backend
from core.legacy_paths import legacy_integration_path

RADIOMETRY_SOURCE = legacy_integration_path("FLIR", "flir_radiometry.py")
HEALTH_SOURCE = legacy_integration_path("FLIR", "flir_health_temperature.py")

# The one mode the campaign can support. Environment-corrected temperature needs
# five measured environment values - emissivity, object distance, atmospheric
# and reflected apparent temperature, relative humidity - that the campaign does
# not record, so it was never quantitative and is no longer offered.
APPARENT = "apparent"


class LegacyFlirLevel2Bridge:
    """Load the validated radiometry without executing its command line."""

    _lock = threading.RLock()

    def __init__(
        self,
        radiometry_path: Path = RADIOMETRY_SOURCE,
        health_path: Path = HEALTH_SOURCE,
    ) -> None:
        self.radiometry_path = Path(radiometry_path)
        self.health_path = Path(health_path)
        self._radiometry: ModuleType | None = None
        self._health: ModuleType | None = None

    def _load(self, name: str, path: Path) -> ModuleType:
        if not path.is_file():
            raise FileNotFoundError(
                f"Validated FLIR science is unavailable: {path}"
            )
        # The health module imports flir_radiometry by name.
        directory = str(path.parent)
        if directory not in sys.path:
            sys.path.insert(0, directory)
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load FLIR science: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        # It renders diagnostic plots; pin the backend before it imports pyplot.
        use_headless_backend()
        spec.loader.exec_module(module)
        return module

    @property
    def radiometry(self) -> ModuleType:
        with self._lock:
            if self._radiometry is None:
                self._radiometry = self._load(
                    "ccflux_flir_radiometry", self.radiometry_path
                )
            return self._radiometry

    @property
    def health(self) -> ModuleType:
        with self._lock:
            if self._health is None:
                # Radiometry first: the health module imports it at module scope.
                self.radiometry
                self._health = self._load(
                    "ccflux_flir_health_temperature", self.health_path
                )
            return self._health
