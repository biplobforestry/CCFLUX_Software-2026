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

# Two modes, exactly as the reference defines them.
APPARENT = "apparent"
CORRECTED = "corrected"

# Results computed from guessed environment values must never be presented as
# quantitative, so the provenance travels with every row.
PROVENANCE_MEASURED = "measured"
PROVENANCE_ASSUMED = "assumed_for_testing"


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

    def correction_inputs(self, options: dict) -> object | None:
        """Build CorrectionInputs, or None for apparent (uncorrected) mode.

        Apparent mode uses factory calibration with emissivity 1 and no
        atmospheric, reflected or optics correction. The reference is explicit
        that it is a sensor sanity check, not a publication-grade surface
        temperature, and the caller is expected to say so in its output.
        """
        if str(options.get("mode", APPARENT)) != CORRECTED:
            return None
        required = (
            "emissivity",
            "object_distance_m",
            "atmospheric_temperature_c",
            "reflected_apparent_temperature_c",
            "relative_humidity_percent",
        )
        missing = [name for name in required if options.get(name) is None]
        if missing:
            raise ValueError(
                "Environment-corrected temperature needs measured values for: "
                + ", ".join(missing)
            )
        inputs = self.radiometry.CorrectionInputs(
            emissivity=float(options["emissivity"]),
            object_distance_m=float(options["object_distance_m"]),
            atmospheric_temperature_c=float(options["atmospheric_temperature_c"]),
            reflected_apparent_temperature_c=float(
                options["reflected_apparent_temperature_c"]
            ),
            relative_humidity_percent=float(options["relative_humidity_percent"]),
            external_optics_transmission=float(
                options.get("external_optics_transmission", 1.0)
            ),
            external_optics_temperature_c=(
                None
                if options.get("external_optics_temperature_c") is None
                else float(options["external_optics_temperature_c"])
            ),
        )
        inputs.validate()
        return inputs
