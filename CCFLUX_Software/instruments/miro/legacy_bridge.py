"""Lazy, immutable bridge to the validated legacy MIRO modules."""

from __future__ import annotations

import importlib.util
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from core.legacy_paths import legacy_integration_path
from types import ModuleType
from typing import Callable, Iterable

DEFAULT_LEGACY_DIRECTORY = legacy_integration_path("MIRO_Rack")


class LegacyMiroBridge:
    """Expose legacy MIRO science without importing its Flask GUI."""

    _lock = threading.RLock()

    def __init__(
        self,
        source_directory: Path = DEFAULT_LEGACY_DIRECTORY,
        *,
        miro_module: ModuleType | None = None,
        export_module: ModuleType | None = None,
    ) -> None:
        self.source_directory = Path(source_directory)
        self._miro_module = miro_module
        self._export_module = export_module

    @property
    def miro(self) -> ModuleType:
        with self._lock:
            if self._miro_module is None:
                self._miro_module = self._load(
                    "ccflux_legacy_miro", self.source_directory / "miro.py"
                )
            return self._miro_module

    @property
    def exporter(self) -> ModuleType:
        with self._lock:
            if self._export_module is None:
                # export.py uses the historical top-level import names.
                picarro = self._load(
                    "ccflux_legacy_picarro_dependency",
                    self.source_directory / "picarro.py",
                )
                with self._legacy_names({"miro": self.miro, "picarro": picarro}):
                    self._export_module = self._load(
                        "ccflux_legacy_miro_export",
                        self.source_directory / "export.py",
                    )
            return self._export_module

    def discover_files(self, root: Path):
        return self.miro.discover_files(root)

    def load_folder(
        self,
        root: Path,
        progress: Callable[[float, str], None] | None = None,
    ):
        return self.miro.load_folder(root, progress)

    def analyze(self, data, **options):
        return self.miro.analyze(
            data,
            options["gas"],
            float(options.get("smooth_seconds", 300.0)),
            options.get("start"),
            options.get("end"),
            float(options.get("remove_seconds", 30.0)),
        )

    def save_quicklook(self, analysis: dict, output_path: Path, params: dict) -> Path:
        figure = self.exporter.miro_figure(analysis, params)
        try:
            figure.savefig(
                output_path,
                format=output_path.suffix.lstrip("."),
                dpi=int(params.get("dpi", 150)),
                facecolor="white",
            )
        finally:
            self.exporter.plt.close(figure)
        return output_path

    def export_figures(
        self,
        output_directory: Path,
        formats: Iterable[str],
        data,
        params: dict,
        progress: Callable[[float, str], None] | None = None,
    ) -> list[str]:
        return self.exporter.export_figures(
            "miro",
            output_directory,
            formats,
            int(params.get("dpi", 300)),
            data,
            None,
            params,
            progress,
        )

    @staticmethod
    def _load(name: str, path: Path) -> ModuleType:
        if not path.is_file():
            raise FileNotFoundError(f"Legacy MIRO source is unavailable: {path}")
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load legacy MIRO source: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    @contextmanager
    def _legacy_names(self, modules: dict[str, ModuleType]):
        previous = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
