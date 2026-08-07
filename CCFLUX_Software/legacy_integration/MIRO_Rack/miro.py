"""MIRO data loading and trace-gas analysis for the Zeppelin dashboard."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Callable
import numpy as np
import pandas as pd
from scipy.signal import welch

TIMESTAMP_COLUMN = "t-stamp"
VALVE_COLUMN = "VValve 0"
GAS_COLUMNS = ("CO wet", "N2O wet", "H2O wet", "NO wet", "NO2 wet", "CH4 wet", "SO2 wet", "NH3 wet", "O3 wet", "CO2 wet")
# The instrument's own state, read alongside the gases and carried through so a
# drift can be attributed rather than guessed at. On Flight_CC0806 the cell
# warmed 25.5 -> 33.2 C and CO2 followed it at 6.2 ppm/C, which is 14 times the
# atmospheric signal; without these columns there is nothing to regress against.
HOUSEKEEPING_COLUMNS = ("T Cell C", "Outside T", "Laser housing T", "p Cell")
HOUSEKEEPING_UNITS = {
    "T Cell C": "degC", "Outside T": "degC",
    "Laser housing T": "degC", "p Cell": "mbar",
}
Progress = Callable[[float, str], None]


TEXT_SUFFIX = ".txt"


def discover_files(root: str | Path) -> list[Path]:
    folder = Path(root).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"MIRO folder does not exist: {folder}")
    files = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == TEXT_SUFFIX)
    if not files:
        raise FileNotFoundError(f"No MIRO .txt files found under {folder}")
    return files


def ignored_files(root: str | Path) -> list[Path]:
    """Everything in the MIRO folder that is not a text delivery.

    MIRO writes TDMS beside the text export. Its timestamp schema was never
    confirmed for this campaign, so it is not read at all - but the operator is
    told how many files were passed over rather than left to wonder whether
    they went in.
    """
    folder = Path(root).expanduser().resolve()
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() != TEXT_SUFFIX
    )


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def duplicate_files_by_content(paths: list[Path]) -> dict[Path, Path]:
    """Map each byte-identical copy to the first file that held those bytes.

    Sizes are compared first and only a collision is hashed. Every file used to
    be hashed unconditionally, which is a second full read of the whole delivery
    before a single row is parsed; a delivery with no repeated size is now never
    read twice, and one with duplicates reads only the files that could be.
    """
    by_size: dict[int, list[Path]] = {}
    for path in paths:
        try:
            by_size.setdefault(path.stat().st_size, []).append(path)
        except OSError:
            continue
    duplicates: dict[Path, Path] = {}
    for group in by_size.values():
        if len(group) < 2:
            continue
        first_seen: dict[str, Path] = {}
        # Shortest name first, so "a.txt" is kept and "a - Copy.txt" is the
        # duplicate rather than the other way round - alphabetical order puts
        # the copy first and named the original as the redundant one. The bytes
        # are identical either way, so this decides only what is reported.
        for path in sorted(group, key=lambda item: (len(item.name), item.name)):
            try:
                fingerprint = _sha256(path)
            except OSError:
                continue
            original = first_seen.setdefault(fingerprint, path)
            if original != path:
                duplicates[path] = original
    return duplicates


def load_folder(root: str | Path, progress: Progress | None = None) -> tuple[pd.DataFrame, dict]:
    files = discover_files(root)
    passed_over = ignored_files(root)
    # Resolved up front, so a repeated file costs one hash rather than a parse.
    duplicates = duplicate_files_by_content(files)
    frames, duplicate_files, skipped_files = [], [], []
    unparsable_rows = 0
    for index, path in enumerate(files, start=1):
        if progress:
            progress((index - 1) / len(files), f"MIRO: reading {index}/{len(files)} - {path.name}")
        if path in duplicates:
            duplicate_files.append(str(path)); continue
        try:
            frame = pd.read_csv(path, sep=";", decimal=",")
            frame.columns = [str(column).strip() for column in frame.columns]
        except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
            skipped_files.append({"file": str(path), "reason": str(exc)}); continue
        if TIMESTAMP_COLUMN not in frame:
            skipped_files.append({"file": str(path), "reason": f"Missing {TIMESTAMP_COLUMN}"}); continue
        keep = [column for column in (TIMESTAMP_COLUMN, *GAS_COLUMNS, *HOUSEKEEPING_COLUMNS, VALVE_COLUMN) if column in frame]
        frame = frame[keep].copy()
        stamps = pd.to_datetime(frame.pop(TIMESTAMP_COLUMN), format="%d.%m.%Y %H:%M:%S,%f", errors="coerce")
        # A row whose clock cannot be read is dropped rather than guessed at, and
        # counted so the operator sees how much of the delivery that was.
        unparsable_rows += int(stamps.isna().sum())
        frame["timestamp"] = stamps
        for column in (*GAS_COLUMNS, *HOUSEKEEPING_COLUMNS, VALVE_COLUMN):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["source_file"] = path.name
        frames.append(frame.dropna(subset=["timestamp"]))
    if not frames:
        raise RuntimeError("No readable timestamped MIRO records were found.")
    data = pd.concat(frames, ignore_index=True, sort=False)
    # Measured before the sort, which is what makes it reportable: afterwards
    # there is nothing left to see. Both are single vectorised passes.
    out_of_order_rows = int((data["timestamp"].diff().dt.total_seconds() < 0).sum())
    data = data.sort_values("timestamp", kind="stable")
    before = len(data)
    data = data.drop_duplicates("timestamp", keep="first").reset_index(drop=True)
    duplicate_timestamps = before - len(data)
    if not data.timestamp.is_monotonic_increasing:
        raise RuntimeError("MIRO timestamps could not be sorted chronologically.")
    gases = [column for column in GAS_COLUMNS if column in data and data[column].notna().any()]
    if not gases:
        raise RuntimeError("MIRO files contain no supported trace-gas columns.")
    if progress:
        progress(1.0, "MIRO: concatenation and timestamp validation complete")
    warnings = _load_warnings(
        instrument="MIRO",
        duplicate_files=duplicate_files,
        skipped_files=skipped_files,
        unparsable_rows=unparsable_rows,
        out_of_order_rows=out_of_order_rows,
        duplicate_timestamps=duplicate_timestamps,
        clock_column=TIMESTAMP_COLUMN,
    )
    if passed_over:
        warnings.append(
            f"{len(passed_over)} non-text file(s) in the MIRO folder were "
            "ignored; only .txt deliveries are read: "
            + ", ".join(path.name for path in passed_over[:3])
            + (" ..." if len(passed_over) > 3 else "")
        )
    return data, {"files_found": len(files), "files_used": len(frames), "duplicate_files": duplicate_files, "skipped_files": skipped_files, "duplicate_timestamps_removed": duplicate_timestamps, "unparsable_rows_removed": unparsable_rows, "out_of_order_rows_repaired": out_of_order_rows, "ignored_files": [str(path) for path in passed_over], "sorted": True, "rows": len(data), "start": data.timestamp.min().isoformat(), "end": data.timestamp.max().isoformat(), "gases": gases, "warnings": warnings}


def _load_warnings(
    *,
    instrument: str,
    duplicate_files: list,
    skipped_files: list,
    unparsable_rows: int,
    out_of_order_rows: int,
    duplicate_timestamps: int,
    clock_column: str,
) -> list[str]:
    """Say what had to be repaired. Nothing here stops the load.

    A campaign folder collects second copies and logger restarts, and neither is
    a reason to refuse a delivery. Both are a reason to be told, because a run
    that silently drops 16,000 rows and one that had 16,000 duplicates look
    identical afterwards.
    """
    warnings: list[str] = []
    if duplicate_files:
        names = ", ".join(Path(value).name for value in duplicate_files[:3])
        warnings.append(
            f"{len(duplicate_files)} {instrument} file(s) are byte-identical "
            f"copies of another file and were read once: {names}"
            + (" ..." if len(duplicate_files) > 3 else "")
        )
    if unparsable_rows:
        warnings.append(
            f"{unparsable_rows:,} {instrument} row(s) had no readable "
            f"{clock_column} and were excluded; a guessed instant would be worse "
            "than a declared gap."
        )
    if out_of_order_rows:
        warnings.append(
            f"{out_of_order_rows:,} out-of-order {instrument} row transition(s) "
            "were put back into chronological order. The delivered files are "
            "unchanged."
        )
    if duplicate_timestamps:
        warnings.append(
            f"{duplicate_timestamps:,} duplicated {instrument} timestamp(s) were "
            "removed, keeping the first record of each instant."
        )
    if skipped_files:
        warnings.append(
            f"{len(skipped_files)} {instrument} file(s) could not be read: "
            + "; ".join(
                f"{Path(item['file']).name}: {item['reason']}"
                for item in skipped_files[:2]
            )
        )
    return warnings


def gas_unit_scale(column: str) -> tuple[str, float]:
    if column == "H2O wet": return "%", 100.0
    if column in {"CO2 wet", "CH4 wet"}: return "ppm", 1e6
    return "ppb", 1e9


def _stable_ambient_frame(
    data: pd.DataFrame,
    remove_seconds: float = 30.0,
    gap_threshold: float = 30.0,
) -> tuple[pd.DataFrame, list[str], dict]:
    """Mark stable ambient samples without performing another zero subtraction.

    Valve=0 values are MIRO's already background-corrected ambient output.
    A return to ambient is detected from Valve=1 to Valve=0 and,
    independently, from a timestamp gap longer than ``gap_threshold``.
    """
    frame = data.sort_values("timestamp", kind="stable").copy()
    if frame.empty:
        raise ValueError("No MIRO data are available.")
    warnings: list[str] = []
    delta = frame["timestamp"].diff().dt.total_seconds()
    gap_return = delta.gt(gap_threshold)
    valve_available = VALVE_COLUMN in frame and frame[VALVE_COLUMN].notna().any()

    if valve_available:
        raw_valve = pd.to_numeric(frame[VALVE_COLUMN], errors="coerce").round()
        invalid_valve = raw_valve.notna() & ~raw_valve.isin([0, 1])
        if invalid_valve.any():
            warnings.append(
                f"{int(invalid_valve.sum()):,} unsupported valve-state values were ignored."
            )
            raw_valve = raw_valve.mask(invalid_valve)
        frame["valve"] = raw_valve.ffill().bfill().astype("Int64")
        ambient_state = frame["valve"].eq(0).fillna(False)
        previous = frame["valve"].shift()
        valve_return = ambient_state & previous.eq(1).fillna(False)
        transition_return = valve_return | (ambient_state & gap_return)
        state_change = frame["valve"].ne(frame["valve"].shift()).fillna(False)
        block_start = state_change | gap_return
        frame["valve_episode"] = state_change.cumsum().astype(int) + 1
        episode_start = frame.groupby("valve_episode")["timestamp"].transform("first")
        frame["seconds_after_valve_switch"] = (
            frame["timestamp"] - episode_start
        ).dt.total_seconds()
        # The first observed state has no detected switch inside the dataset.
        frame.loc[frame["valve_episode"].eq(1), "seconds_after_valve_switch"] = np.inf
        detection_mode = "explicit valve state with time-gap fallback"
    else:
        frame["valve"] = pd.Series(0, index=frame.index, dtype="Int64")
        ambient_state = pd.Series(True, index=frame.index)
        valve_return = pd.Series(False, index=frame.index)
        transition_return = gap_return
        block_start = gap_return
        frame["valve_episode"] = 1
        frame["seconds_after_valve_switch"] = np.inf
        detection_mode = "time-gap detection (valve column unavailable)"
        warnings.append("Valve column is absent; ambient transitions were inferred from time gaps.")
        if not gap_return.any():
            warnings.append(
                f"Valve column is absent and no time gaps longer than {gap_threshold:g} s were detected."
            )

    frame["ambient_block"] = block_start.cumsum().astype(int) + 1
    timestamps = frame["timestamp"]
    latest_return = timestamps.where(transition_return).ffill()
    elapsed = (timestamps - latest_return).dt.total_seconds()
    affected = ambient_state & latest_return.notna() & elapsed.le(remove_seconds)
    frame["seconds_after_ambient_return"] = np.inf
    frame.loc[affected, "seconds_after_ambient_return"] = elapsed.loc[affected]

    stable_after_return = frame["seconds_after_ambient_return"].gt(remove_seconds)
    frame["valid_ambient"] = (ambient_state & stable_after_return).astype(bool)
    info = {
        "valve_available": bool(valve_available),
        "detection_mode": detection_mode,
        "valve_returns": int(valve_return.sum()),
        "gap_returns": int((ambient_state & gap_return).sum()),
        "transition_returns": int(transition_return.sum()),
        "gap_threshold_seconds": float(gap_threshold),
        "removed_after_return_seconds": float(remove_seconds),
    }
    return frame, warnings, info


def _time_bound(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return pd.to_datetime(text, format="%m-%d-%Y %H:%M")
    except ValueError:
        return pd.Timestamp(text)


def _slice(data: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    frame = data
    lo, hi = _time_bound(start), _time_bound(end)
    if lo is not None:
        frame = frame.loc[frame.timestamp >= lo]
    if hi is not None:
        end_text = str(end or "").strip()
        if len(end_text) == 16 and end_text[2] == "-" and end_text[5] == "-":
            frame = frame.loc[frame.timestamp < hi + pd.Timedelta(minutes=1)]
        else:
            frame = frame.loc[frame.timestamp <= hi]
    if frame.empty:
        raise ValueError("The MIRO date-time filter contains no data.")
    return frame.copy()


def _fft_lowpass(values: np.ndarray, sample_period: float, cutoff_s: float) -> np.ndarray:
    mean = float(np.mean(values))
    spectrum = np.fft.rfft(values - mean)
    frequencies = np.fft.rfftfreq(len(values), d=sample_period)
    spectrum[frequencies > 1.0 / cutoff_s] = 0.0
    return np.fft.irfft(spectrum, n=len(values)) + mean


def _block_average(values: np.ndarray, size: int) -> np.ndarray:
    count = len(values) // size
    if count == 0:
        return np.array([], dtype=float)
    return values[: count * size].reshape(count, size).mean(axis=1)


def _allan(values: np.ndarray, sample_period: float) -> pd.DataFrame:
    maximum = len(values) // 10
    if maximum < 1:
        return pd.DataFrame(columns=["samples", "tau", "deviation", "expected"])
    rows = []
    for size in np.unique(np.logspace(0, np.log10(maximum), 75).astype(int)):
        means = _block_average(values, int(size))
        if len(means) < 2:
            continue
        differences = np.diff(means)
        rows.append(
            {
                "samples": int(size),
                "tau": float(size * sample_period),
                "deviation": float(np.sqrt(0.5 * np.mean(differences**2))),
            }
        )
    table = pd.DataFrame(rows)
    if not table.empty:
        reference = float(table.iloc[0]["deviation"])
        first_tau = float(table.iloc[0]["tau"])
        table["expected"] = reference * np.sqrt(first_tau / table["tau"])
    return table


def _segmented_allan(
    segments: list[np.ndarray],
    sample_period: float,
    minimum_differences: int = 5,
) -> pd.DataFrame:
    """Non-overlapping Allan deviation without differences across segment gaps.

    Each segment is block-averaged independently. Consecutive block-mean
    differences are pooled only after they have been calculated within a
    continuous segment, so opposite sides of a removed valve interval are
    never treated as adjacent observations.
    """
    clean = [
        np.asarray(values, dtype=float)[np.isfinite(values)]
        for values in segments
        if len(values) >= 2
    ]
    clean = [values for values in clean if len(values) >= 2]
    total = sum(len(values) for values in clean)
    longest = max((len(values) for values in clean), default=0)
    maximum = min(total // 10, longest // 2)
    if maximum < 1:
        return pd.DataFrame(
            columns=["samples", "tau", "deviation", "expected", "differences"]
        )
    rows = []
    for size in np.unique(np.logspace(0, np.log10(maximum), 75).astype(int)):
        squared_differences = []
        for values in clean:
            means = _block_average(values, int(size))
            if len(means) >= 2:
                squared_differences.extend(np.diff(means) ** 2)
        if len(squared_differences) < minimum_differences:
            continue
        rows.append(
            {
                "samples": int(size),
                "tau": float(size * sample_period),
                "deviation": float(
                    np.sqrt(0.5 * np.mean(np.asarray(squared_differences)))
                ),
                "differences": int(len(squared_differences)),
            }
        )
    table = pd.DataFrame(rows)
    if not table.empty:
        reference = float(table.iloc[0]["deviation"])
        first_tau = float(table.iloc[0]["tau"])
        table["expected"] = reference * np.sqrt(first_tau / table["tau"])
    return table


def _allan_payload(table: pd.DataFrame, value_key: str) -> dict:
    if table.empty:
        return {
            "tau": [],
            "samples": [],
            value_key: [],
            "white_noise": [],
            "differences": [],
        }
    return {
        "tau": table["tau"].astype(float).tolist(),
        "samples": table["samples"].astype(int).tolist(),
        value_key: table["deviation"].astype(float).tolist(),
        "white_noise": table["expected"].astype(float).tolist(),
        "differences": (
            table["differences"].astype(int).tolist()
            if "differences" in table
            else []
        ),
    }


def _minimum_allan(table: pd.DataFrame) -> tuple[float, float]:
    if table.empty:
        return float("nan"), float("nan")
    index = table["deviation"].idxmin()
    return float(table.loc[index, "deviation"]), float(table.loc[index, "tau"])

def _finite_or_none(value: float) -> float | None:
    number = float(value)
    return number if np.isfinite(number) else None

def _wall_clock_payload(frame: pd.DataFrame, maximum: int = 12000) -> dict:
    """Downsample while inserting explicit None separators between blocks."""
    if len(frame) > maximum:
        indices = np.unique(np.linspace(0, len(frame) - 1, maximum).astype(int))
        display = frame.iloc[indices].copy()
    else:
        display = frame.copy()
    payload = {"time": [], "ambient": [], "trend": [], "residual": []}
    previous_block = None
    for row in display.itertuples():
        block = int(getattr(row, "analysis_block", row.ambient_block))
        if previous_block is not None and block != previous_block:
            for key in payload:
                payload[key].append(None)
        payload["time"].append(pd.Timestamp(row.timestamp).isoformat())
        payload["ambient"].append(float(row.corrected_ambient))
        payload["trend"].append(float(row.trend))
        payload["residual"].append(float(row.residual))
        previous_block = block
    return payload


def _welch_psd(
    residual: np.ndarray, sample_period: float
) -> tuple[np.ndarray, np.ndarray, int]:
    if len(residual) < 32:
        return np.array([]), np.array([]), 0
    target = min(4096, max(8, len(residual) // 4))
    nperseg = 2 ** int(np.floor(np.log2(target)))
    frequency, power = welch(
        residual,
        fs=1.0 / sample_period,
        window="hann",
        detrend="constant",
        nperseg=nperseg,
    )
    positive = (frequency > 0) & np.isfinite(power) & (power > 0)
    return frequency[positive], power[positive], int(nperseg)


def analyze(
    data: pd.DataFrame,
    gas: str,
    smooth_seconds: float = 300.0,
    start: str | None = None,
    end: str | None = None,
    remove_seconds: float = 30.0,
) -> dict:
    if gas not in data:
        raise KeyError(f"MIRO gas is unavailable: {gas}")
    if smooth_seconds <= 0:
        raise ValueError("Detrending cutoff must be positive.")
    unit, scale = gas_unit_scale(gas)
    prepared, warnings, transition_info = _stable_ambient_frame(data, remove_seconds)
    duplicate_mask = prepared["timestamp"].duplicated(keep="first")
    if duplicate_mask.any():
        warnings.append(
            f"{int(duplicate_mask.sum()):,} duplicate timestamps were removed before analysis."
        )
        prepared = prepared.loc[~duplicate_mask].copy()
    frame = _slice(prepared, start, end)
    native = pd.to_numeric(frame[gas], errors="coerce").to_numpy(float)
    candidate_ambient = frame["valve"].eq(0).fillna(False).to_numpy(bool)
    nonfinite = ~np.isfinite(native) & candidate_ambient
    if nonfinite.any():
        warnings.append(
            f"{int(nonfinite.sum()):,} NaN or infinite ambient {gas} values were excluded."
        )
    frame["corrected_ambient"] = native * scale
    ambient = frame.loc[
        frame["valid_ambient"] & np.isfinite(frame["corrected_ambient"])
    ].copy()
    if ambient.empty:
        raise ValueError("No valid stable ambient MIRO data remain.")
    if len(ambient) < 20:
        warnings.append("Too few retained ambient samples for a reliable quick-look analysis.")

    within_block = ambient["ambient_block"].eq(ambient["ambient_block"].shift())
    intervals = ambient["timestamp"].diff().dt.total_seconds()
    short = intervals[within_block & intervals.gt(0) & intervals.lt(5)]
    if short.empty:
        sample_period = 1.0
        warnings.append("No valid short sampling intervals were found; dt=1 s was assumed.")
    else:
        sample_period = float(short.median())
        tolerance = max(0.05, 0.1 * sample_period)
        irregular_fraction = float((short.sub(sample_period).abs() > tolerance).mean())
        if irregular_fraction > 0.10:
            warnings.append(
                f"Sampling is irregular within ambient blocks ({irregular_fraction:.1%} of short intervals differ from median dt={sample_period:.4g} s)."
            )

    # Missing samples and material timestamp jumps split continuous analysis
    # segments. Small clock jitter is tolerated; samples are never interpolated.
    continuity_limit = max(1.5 * sample_period, sample_period + 0.2)
    explicit_change = ambient["ambient_block"].ne(ambient["ambient_block"].shift())
    missing_break = (~explicit_change) & intervals.gt(continuity_limit)
    ambient["analysis_block"] = (explicit_change | missing_break).cumsum().astype(int)
    if missing_break.any():
        warnings.append(
            f"{int(missing_break.sum()):,} missing-sample time gaps longer than {continuity_limit:.4g} s split the segmented analyses; no interpolation was applied."
        )

    corrected = ambient["corrected_ambient"].to_numpy(float)
    retained_duration = len(corrected) * sample_period
    if smooth_seconds < 2 * sample_period:
        warnings.append(
            f"Selected FFT cutoff ({smooth_seconds:g} s) is shorter than twice dt ({2 * sample_period:.4g} s)."
        )
    if smooth_seconds > retained_duration:
        warnings.append(
            f"Selected FFT cutoff ({smooth_seconds:g} s) is longer than the retained ambient duration ({retained_duration:.1f} s)."
        )

    # Detrend every stable ambient block independently. This prevents the FFT
    # trend from using samples on opposite sides of a removed valve interval.
    ambient["trend"] = np.nan
    ambient["residual"] = np.nan
    residual_segments: list[np.ndarray] = []
    for _, block in ambient.groupby("analysis_block", sort=False):
        values = block["corrected_ambient"].to_numpy(float)
        if len(values) >= 3:
            block_trend = _fft_lowpass(values, sample_period, smooth_seconds)
        else:
            block_trend = np.full(len(values), float(np.mean(values)))
        block_residual = values - block_trend
        ambient.loc[block.index, "trend"] = block_trend
        ambient.loc[block.index, "residual"] = block_residual
        if len(block_residual) >= 2:
            residual_segments.append(block_residual)
    residual = ambient["residual"].to_numpy(float)
    block_count = int(ambient["analysis_block"].nunique())
    if block_count < 2:
        warnings.append("Fewer than two ambient blocks are available.")

    # Primary campaign view: background-corrected ambient samples on retained
    # (gap-collapsed) effective time. This intentionally has one boundary
    # difference per joined ambient block and is not an instrument-precision
    # estimate when zero-air gaps are present.
    allan = _allan(corrected, sample_period)
    if len(corrected) < 20 or allan.empty:
        warnings.append("Too few retained ambient samples remain for Allan deviation.")
    if allan.empty:
        allan_at_one = allan_at_one_tau = float("nan")
    else:
        nearest_one = (allan["tau"] - 1.0).abs().idxmin()
        allan_at_one = float(allan.loc[nearest_one, "deviation"])
        allan_at_one_tau = float(allan.loc[nearest_one, "tau"])
    allan_min, allan_tau = _minimum_allan(allan)
    allan_payload = _allan_payload(allan, "ambient")
    allan_payload.update(
        {
            "input_signal": "MIRO background-corrected stable ambient concentration",
            "method": "non-overlapping block means; gaps collapsed",
            "gap_handling": (
                "Ambient blocks are concatenated on effective retained time; "
                "one adjacent difference can span each removed gap."
            ),
        }
    )

    # Precision diagnostic 1: high-pass residual, evaluated independently in
    # every continuous stable-ambient block. No Allan difference crosses a gap.
    segmented_residual_allan = _segmented_allan(
        residual_segments, sample_period
    )
    residual_allan_min, residual_allan_tau = _minimum_allan(
        segmented_residual_allan
    )
    residual_allan_payload = _allan_payload(
        segmented_residual_allan, "residual"
    )
    residual_allan_payload.update(
        {
            "input_signal": (
                f"High-pass residual of background-corrected ambient; "
                f"FFT cutoff {smooth_seconds:g} s"
            ),
            "method": "non-overlapping block means pooled within segments",
            "gap_handling": "No block mean or adjacent difference crosses an ambient/zero-air gap.",
            "segments": len(residual_segments),
        }
    )

    # Precision diagnostic 2: measured zero-air values only. The first 30 s
    # after every detected switch into Valve=1 are excluded, just as they are
    # after the return to ambient. Automated MIRO files often contain no gas
    # values during this state; in that case this result is explicitly absent.
    zero_mask = (
        frame["valve"].eq(1).fillna(False)
        & frame["seconds_after_valve_switch"].gt(remove_seconds)
        & np.isfinite(native)
    )
    zero = frame.loc[zero_mask, ["timestamp", "valve_episode"]].copy()
    zero["scaled_signal"] = native[zero_mask.to_numpy(bool)] * scale
    zero_episode_change = zero["valve_episode"].ne(zero["valve_episode"].shift())
    zero_missing_break = zero["timestamp"].diff().dt.total_seconds().gt(continuity_limit)
    zero["analysis_block"] = (
        zero_episode_change | zero_missing_break
    ).cumsum().astype(int)
    zero_segments = [
        block["scaled_signal"].to_numpy(float)
        for _, block in zero.groupby("analysis_block", sort=False)
        if len(block) >= 2
    ]
    zero_allan = _segmented_allan(zero_segments, sample_period)
    zero_allan_min, zero_allan_tau = _minimum_allan(zero_allan)
    zero_allan_payload = _allan_payload(zero_allan, "zero_air")
    zero_allan_payload.update(
        {
            "input_signal": "Measured stable zero-air concentration (Valve=1)",
            "method": "non-overlapping block means pooled within zero-air episodes",
            "gap_handling": "First 30 s after each zero-air switch removed; no difference crosses episodes.",
            "segments": len(zero_segments),
            "rows": int(len(zero)),
        }
    )
    if zero_allan.empty:
        warnings.append(
            "No sufficiently long measured stable zero-air concentration episodes are available; zero-air precision Allan is unavailable."
        )

    frequency, power, nperseg = _welch_psd(residual, sample_period)
    if not len(frequency):
        warnings.append("Too few retained ambient samples remain for Welch PSD.")
    return {
        "gas": gas,
        "unit": unit,
        "correction_mode": "MIRO internal background correction; no additional zero-air subtraction",
        "detection_mode": transition_info["detection_mode"],
        "smooth_seconds": float(smooth_seconds),
        "sample_period": sample_period,
        "series": _wall_clock_payload(ambient),
        "allan": allan_payload,
        "diagnostic_allan": {
            "segmented_residual": residual_allan_payload,
            "stable_zero_air": zero_allan_payload,
        },
        "psd": {
            "frequency": frequency.astype(float).tolist(),
            "power": power.astype(float).tolist(),
            "nperseg": nperseg,
            "input_signal": "High-pass residual of background-corrected stable ambient concentration",
            "method": "Welch PSD on the retained residual sequence",
            "gap_handling": "Ambient blocks are gap-collapsed; the residual itself was detrended separately within each block.",
        },
        "warnings": list(dict.fromkeys(warnings)),
        "stats": {
            "ambient_std": float(np.std(corrected, ddof=1)) if len(corrected) > 1 else float("nan"),
            "residual_std": float(np.std(residual, ddof=1)) if len(residual) > 1 else float("nan"),
            "allan_at_one": _finite_or_none(allan_at_one),
            "allan_at_one_tau": _finite_or_none(allan_at_one_tau),
            "allan_min": _finite_or_none(allan_min),
            "allan_min_tau": _finite_or_none(allan_tau),
            "segmented_residual_allan_min": _finite_or_none(residual_allan_min),
            "segmented_residual_allan_min_tau": _finite_or_none(residual_allan_tau),
            "zero_air_allan_min": _finite_or_none(zero_allan_min),
            "zero_air_allan_min_tau": _finite_or_none(zero_allan_tau),
            "zero_air_rows": int(len(zero)),
            "zero_air_segments": len(zero_segments),
            "rows": len(ambient),
            "ambient_blocks": block_count,
            "retained_duration": retained_duration,
            "transition_returns": transition_info["transition_returns"],
        },
    }

def comparison_series(
    data: pd.DataFrame,
    gas: str,
    start: str | None = None,
    end: str | None = None,
    remove_seconds: float = 30.0,
) -> pd.Series:
    _, scale = gas_unit_scale(gas)
    prepared, _, _ = _stable_ambient_frame(data, remove_seconds)
    frame = _slice(prepared, start, end)
    selected = frame.loc[
        frame["valid_ambient"] & frame[gas].notna(), ["timestamp", gas]
    ].copy()
    selected.index = pd.DatetimeIndex(selected.timestamp)
    return (selected[gas] * scale).sort_index()