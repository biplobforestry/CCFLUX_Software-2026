"""Lazy, immutable bridge to the validated legacy Noseboom module."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

from core.headless_plotting import use_headless_backend
from core.legacy_paths import legacy_integration_path
from types import ModuleType
from typing import Callable, Iterable

DEFAULT_LEGACY_PATH = legacy_integration_path("Noseboom", "noseboom_browser_GUI.py")


class LegacyNoseboomBridge:
    """Load legacy functions without starting its browser GUI."""

    _load_lock = threading.RLock()

    def __init__(
        self, source_path: Path = DEFAULT_LEGACY_PATH, module: ModuleType | None = None
    ) -> None:
        self.source_path = Path(source_path)
        self._module = module

    @property
    def module(self) -> ModuleType:
        with self._load_lock:
            if self._module is None:
                if not self.source_path.is_file():
                    raise FileNotFoundError(
                        f"Legacy Noseboom source is unavailable: {self.source_path}"
                    )
                spec = importlib.util.spec_from_file_location(
                    "ccflux_legacy_noseboom", self.source_path
                )
                if spec is None or spec.loader is None:
                    raise ImportError(
                        f"Could not load legacy Noseboom source: {self.source_path}"
                    )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                # Legacy modules import pyplot at module scope; pin the
                # non-interactive backend before that happens.
                use_headless_backend()
                spec.loader.exec_module(module)
                self._module = module
            return self._module

    def load_csv_files(
        self,
        files: Iterable[Path],
        progress: Callable[[float, str], None] | None = None,
    ):
        module = self.module
        original = module.set_status

        def report(percent, message, busy=True):
            if progress:
                progress(float(percent), str(message))

        with self._load_lock:
            module.set_status = report
            try:
                return module.load_csv_files(list(files))
            finally:
                module.set_status = original

    def load_csv_window(
        self,
        files: Iterable[Path],
        start_ns: int,
        end_ns: int,
        progress: Callable[[float, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        """Use legacy column mapping while retaining only the requested rows."""
        module = self.module
        chunks = []
        file_list = list(files)
        total_bytes = sum(path.stat().st_size for path in file_list if path.is_file())
        total_rows = 0
        counted_bytes = 0
        for path in file_list:
            file_lines = 0
            final_byte = b""
            with path.open("rb") as stream:
                while True:
                    if cancelled and cancelled():
                        raise RuntimeError("Noseboom processing was cancelled")
                    block = stream.read(8 * 1024 * 1024)
                    if not block:
                        break
                    counted_bytes += len(block)
                    file_lines += block.count(b"\n")
                    final_byte = block[-1:]
                    if progress:
                        progress(
                            2.0 + 3.0 * counted_bytes / max(1, total_bytes),
                            f"Indexing source rows in {path.name}",
                        )
            if final_byte and final_byte != b"\n":
                file_lines += 1
            total_rows += max(0, file_lines - 1)
        rows_examined = 0
        for path in file_list:
            usecols = module.csv_usecols(path)
            for raw in module.pd.read_csv(
                path,
                usecols=usecols,
                encoding=module.detect_encoding(path),
                low_memory=False,
                chunksize=module.CHUNKSIZE,
            ):
                # usecols names the columns as the file spells them, so a
                # prefixed export gives back "NoseBoom_Airflow_UTCcorr_...".
                # Dropping the prefix here is what the browser loader does, and
                # it has to happen before anything looks a column up: the old
                # test against usecols never matched a prefixed file, fell back
                # to "time_ns", and failed with KeyError on a valid export.
                raw = raw.rename(columns=module.normalize_column_name)
                rows_examined += len(raw)
                if cancelled and cancelled():
                    raise RuntimeError("Noseboom processing was cancelled")
                time_column = (
                    module.FIELDS["time_ns"]
                    if module.FIELDS["time_ns"] in raw.columns
                    else "time_ns"
                )
                if time_column not in raw.columns:
                    raise ValueError(
                        "The Noseboom file carries no "
                        f"{module.FIELDS['time_ns']} column, so its rows cannot "
                        f"be placed in time: {path.name}"
                    )
                values = module.pd.to_numeric(raw[time_column], errors="coerce")
                finite = values.dropna()
                if not finite.empty and float(finite.min()) > end_ns:
                    break
                selected = raw.loc[(values >= start_ns) & (values <= end_ns)]
                if not selected.empty:
                    chunks.append(module.simplify(selected, path.name))
                if progress:
                    progress(
                        min(40.0, 5.0 + 35.0 * rows_examined / max(1, total_rows)),
                        f"Reading {rows_examined:,} of {total_rows:,} source rows "
                        f"from {path.name}",
                    )
        if not chunks:
            raise ValueError("Selected interval does not intersect Noseboom data")
        if progress:
            progress(40.0, "Selected Noseboom interval loaded.")
        data = module.pd.concat(chunks, ignore_index=True)
        data["time"] = module.pd.to_datetime(data["time"], errors="coerce")
        if data["time"].isna().all():
            data["time"] = module.pd.to_datetime(
                module.pd.to_numeric(data["time_ns"], errors="coerce"),
                unit="ns",
                errors="coerce",
            )
        for column in data.columns:
            if column not in ("time", "_source_csv"):
                data[column] = module.pd.to_numeric(data[column], errors="coerce")
        data["time_ns"] = data["time_ns"].fillna(-1).astype(module.np.int64)
        data["plot_lat"] = data["lat"].where(
            data["lat"].between(-90, 90), data["gnss_lat"]
        )
        data["plot_lon"] = data["lon"].where(
            data["lon"].between(-180, 180), data["gnss_lon"]
        )
        data["altitude_m"] = data["alt_msl_m"].where(
            module.np.isfinite(data["alt_msl_m"]), data["height_m"]
        )
        data["vertical_speed_mps"] = -data["down_mps"]
        data["roll_deg"] = module.np.rad2deg(data["roll_rad"])
        valid = (
            data["time"].notna()
            & data["plot_lat"].between(-90, 90)
            & data["plot_lon"].between(-180, 180)
        )
        return (
            data.loc[valid]
            .sort_values(["time_ns", "time"])
            .drop_duplicates("time_ns")
            .reset_index(drop=True)
        )

    def quicklook(
        self,
        data,
        trim_minutes: float,
        straight_settings=None,
        *,
        include_terrain: bool = False,
    ):
        module = self.module
        one_hz = module.one_hz(data)
        if include_terrain and hasattr(module, "sample_terrarium"):
            cache = module.Path(module.tempfile.gettempdir()) / "noseboom_terrain_tile_cache"
            one_hz["terrain_m"] = module.sample_terrarium(one_hz, cache)
        straight = module.detect_straight(one_hz, straight_settings)
        straight_attrs = dict(straight.attrs)
        if "terrain_m" in one_hz.columns and "terrain_m" not in straight.columns:
            straight = straight.join(one_hz[["terrain_m"]], how="left")
            straight.attrs.update(straight_attrs)
        frequency = module.trim_frequency(data, trim_minutes)
        spectra = module.compute_wind_spectra(data, trim_minutes)
        export_source = module.make_export_source(data)
        return one_hz, straight, frequency, spectra, export_source

    def detailed(self, data, output: Path, flight_name: str, trim_minutes: float):
        return self.module.analyze(data, output, flight_name, trim_minutes)

    def export(
        self,
        output: Path,
        flight_name: str,
        export_source,
        frequency_hz: float,
        format_name: str,
    ):
        return self.module.export_noseboom_data(
            output, flight_name, export_source, frequency_hz, format_name
        )
