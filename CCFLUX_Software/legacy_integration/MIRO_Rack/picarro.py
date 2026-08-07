"""Picarro .dat loading and analysis for the Zeppelin trace-gas dashboard."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Callable
import numpy as np
import pandas as pd

GAS_COLUMNS = {
    "CO2 raw": ("CO2_sync", "ppm"),
    "CO2 dry": ("CO2_dry_sync", "ppm"),
    "CH4 raw": ("CH4_sync", "ppm"),
    "CH4 dry": ("CH4_dry_sync", "ppm"),
    "H2O": ("H2O_sync", "%"),
}
SOURCE_COLUMNS = tuple(value[0] for value in GAS_COLUMNS.values())

# A CRDS analyzer writes DataLog_User in one of two flavours, and which one
# arrives is a setting on the instrument rather than a property of the campaign.
# The synchronized flavour suffixes every species with _sync. The plain flavour
# does not, and instead carries each species' last measured value forward across
# the analyzer's sampling cycle. They are the same quantities, so either is
# accepted and renamed to the _sync spelling that the rest of this module, the
# browser page, and saved project files already use. Flight_CC0806 delivered the
# plain flavour and every one of its 24 files was skipped as "missing gas
# columns", which is how a complete Picarro record came to load as nothing.
#
# The canonical name is listed first, so a file carrying both keeps the
# synchronized column.
COLUMN_ALIASES = {
    "CO2_sync": ("CO2_sync", "CO2"),
    "CO2_dry_sync": ("CO2_dry_sync", "CO2_dry"),
    "CH4_sync": ("CH4_sync", "CH4"),
    "CH4_dry_sync": ("CH4_dry_sync", "CH4_dry"),
    "H2O_sync": ("H2O_sync", "H2O"),
}
TIME_COLUMNS = ("DATE", "TIME")
Progress = Callable[[float, str], None]


def resolve_gas_columns(header) -> dict[str, str]:
    """Map the gas columns this file actually has onto the canonical names."""
    present = set(header)
    resolved: dict[str, str] = {}
    for canonical, spellings in COLUMN_ALIASES.items():
        for spelling in spellings:
            if spelling in present:
                resolved[spelling] = canonical
                break
    return resolved


def discover_files(root: str | Path) -> list[Path]:
    folder = Path(root).expanduser().resolve()
    if not folder.is_dir(): raise FileNotFoundError(f"Picarro folder does not exist: {folder}")
    files = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".dat")
    if not files: raise FileNotFoundError(f"No Picarro .dat files found under {folder}")
    return files


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def duplicate_files_by_content(paths: list[Path]) -> dict[Path, Path]:
    """Map each byte-identical copy to the first file that held those bytes.

    Sizes are compared first and only a collision is hashed, so a delivery with
    no repeated size is never read for this at all. Flight_CC0806 shipped a
    partial "- Copy" of one log; it used to be parsed in full and then thrown
    away one timestamp at a time by the deduplication below.
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
        # Shortest name first, so "a.dat" is kept and "a - Copy.dat" is the
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


def load_folder(root: str | Path, progress: Progress | None=None) -> tuple[pd.DataFrame,dict]:
    files = discover_files(root); frames=[]; skipped_files=[]
    carried_forward: set[str] = set(); synchronized: set[str] = set()
    placeholder_zeros = 0
    unparsable_rows = 0
    duplicates = duplicate_files_by_content(files)
    duplicate_files: list[str] = []
    for index,path in enumerate(files,start=1):
        if progress: progress((index-1)/len(files), f"Picarro: reading {index}/{len(files)} - {path.name}")
        if path in duplicates:
            duplicate_files.append(str(path)); continue
        try:
            header = pd.read_csv(path, sep=r"\s+", nrows=0).columns
        except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
            skipped_files.append({"file": str(path), "reason": f"Header could not be read: {exc}"}); continue
        missing_time = [column for column in TIME_COLUMNS if column not in header]
        resolved = resolve_gas_columns(header)
        if missing_time or not resolved:
            # Name what is missing and what the file does carry. "Missing
            # timestamp or gas columns" was true but unactionable: it never said
            # that the gas columns were present under their other spelling.
            reason = (
                f"Missing timestamp column(s): {', '.join(missing_time)}"
                if missing_time else
                "No recognised gas column. Expected one of "
                + ", ".join(sorted({s for v in COLUMN_ALIASES.values() for s in v}))
                + f"; this file carries {', '.join(map(str, header))}"
            )
            skipped_files.append({"file": str(path), "reason": reason}); continue
        try:
            frame = pd.read_csv(path, sep=r"\s+", usecols=[*TIME_COLUMNS,*resolved])
        except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
            skipped_files.append({"file": str(path), "reason": str(exc)}); continue
        frame = frame.rename(columns=resolved)
        # Keep the instrument clock exactly as written in DATE and TIME.
        # These values remain timezone-naive: no localization, UTC conversion,
        # or clock shift is applied anywhere in the workflow.
        stamps = pd.to_datetime(frame["DATE"].astype(str)+" "+frame["TIME"].astype(str), errors="coerce")
        # A row whose clock cannot be read is dropped rather than guessed at,
        # and counted so the operator sees how much of the delivery that was.
        unparsable_rows += int(stamps.isna().sum())
        frame["timestamp"] = stamps
        frame = frame.drop(columns=list(TIME_COLUMNS)).dropna(subset=["timestamp"])
        for spelling, canonical in resolved.items():
            frame[canonical] = pd.to_numeric(frame[canonical], errors="coerce")
            if spelling == canonical:
                synchronized.add(canonical); continue
            carried_forward.add(canonical)
            # Before a species is measured for the first time, the plain log
            # writes an exact 0.0 rather than leaving the field empty. It is a
            # placeholder, not a measurement: ambient CO2 is never 0 ppm, and
            # keeping it reported this flight's minimum as 0.0 instead of
            # 337.9 ppm and stretched every histogram to the origin. The
            # synchronized columns never carry it, so this is scoped to the
            # spellings that do.
            placeholder = frame[canonical] == 0.0
            placeholder_zeros += int(placeholder.sum())
            frame.loc[placeholder, canonical] = float("nan")
        frame["source_file"] = str(path.relative_to(Path(root).resolve()))
        frames.append(frame)
    if not frames:
        # Carry the per-file reasons into the failure. Without them the operator
        # was told only that nothing was readable, with no way to find out why.
        detail = "; ".join(
            f"{Path(item['file']).name}: {item['reason']}"
            for item in skipped_files[:2]
        )
        more = f" (and {len(skipped_files) - 2} further file(s))" if len(skipped_files) > 2 else ""
        raise RuntimeError(
            "No readable timestamped Picarro records were found. "
            f"{len(skipped_files)} of {len(files)} file(s) were skipped - {detail}{more}"
        )
    data = pd.concat(frames,ignore_index=True,sort=False)
    # Measured before the sort, which is what makes it reportable: afterwards
    # there is nothing left to see. Both are single vectorised passes.
    out_of_order_rows = int((data["timestamp"].diff().dt.total_seconds() < 0).sum())
    data = data.sort_values("timestamp",kind="stable")
    before=len(data); data=data.drop_duplicates("timestamp",keep="first").reset_index(drop=True)
    duplicate_timestamps = before - len(data)
    if not data.timestamp.is_monotonic_increasing:
        raise RuntimeError("Picarro timestamps could not be sorted chronologically.")
    gases=[name for name,(column,_) in GAS_COLUMNS.items() if column in data and data[column].notna().any()]
    if progress: progress(1.0,"Picarro: concatenation and timestamp validation complete")
    variant = (
        "mixed" if carried_forward and synchronized
        else "carried-forward" if carried_forward
        else "synchronized"
    )
    warnings: list[str] = []
    if carried_forward:
        # Said out loud rather than substituted silently. The two flavours are
        # the same quantities, but only the synchronized one is interpolated
        # onto a common time base; the plain one repeats each species' last
        # measured value until it is measured again, so sub-cycle structure in
        # these columns is the analyzer's sampling pattern and not atmosphere.
        warnings.append(
            "Picarro delivered the unsynchronized DataLog_User: "
            + ", ".join(sorted(carried_forward))
            + " were read from the plain column names, where each species holds "
            "its last measured value until the analyzer next measures it. "
            "Concentrations are unchanged; sub-cycle variation is the sampling "
            "pattern, not the atmosphere."
        )
    if placeholder_zeros:
        warnings.append(
            f"{placeholder_zeros} placeholder zero(s) were excluded from the "
            "unsynchronized Picarro columns; they precede the first measurement "
            "of that species and are not observations."
        )
    if duplicate_files:
        names = ", ".join(Path(value).name for value in duplicate_files[:3])
        warnings.append(
            f"{len(duplicate_files)} Picarro file(s) are byte-identical copies "
            f"of another file and were read once: {names}"
            + (" ..." if len(duplicate_files) > 3 else "")
        )
    if unparsable_rows:
        warnings.append(
            f"{unparsable_rows:,} Picarro row(s) had no readable DATE/TIME and "
            "were excluded; a guessed instant would be worse than a declared gap."
        )
    if out_of_order_rows:
        warnings.append(
            f"{out_of_order_rows:,} out-of-order Picarro row transition(s) were "
            "put back into chronological order. The delivered files are unchanged."
        )
    if duplicate_timestamps:
        warnings.append(
            f"{duplicate_timestamps:,} duplicated Picarro timestamp(s) were "
            "removed, keeping the first record of each instant."
        )
    if skipped_files:
        warnings.append(
            f"{len(skipped_files)} of {len(files)} Picarro file(s) were skipped: "
            + "; ".join(
                f"{Path(item['file']).name}: {item['reason']}"
                for item in skipped_files[:2]
            )
        )
    return data,{"files_found":len(files),"files_used":len(frames),"duplicate_files":duplicate_files,"skipped_files":skipped_files,"duplicate_timestamps_removed":duplicate_timestamps,"unparsable_rows_removed":unparsable_rows,"out_of_order_rows_repaired":out_of_order_rows,"sorted":True,"rows":len(data),"start":data.timestamp.min().isoformat(),"end":data.timestamp.max().isoformat(),"gases":gases,"column_variant":variant,"carried_forward_columns":sorted(carried_forward),"placeholder_zeros_removed":placeholder_zeros,"warnings":warnings}


def _time_bound(value: str|None) -> pd.Timestamp|None:
    if not value: return None
    text = str(value).strip()
    try:
        # Interpret the filter in the recorded instrument-clock coordinate system.
        return pd.to_datetime(text, format="%m-%d-%Y %H:%M")
    except ValueError:
        # Retain compatibility with ISO timestamps stored by older projects.
        return pd.Timestamp(text)


def _slice(data: pd.DataFrame,start: str|None,end: str|None)->pd.DataFrame:
    frame=data; lo,hi=_time_bound(start),_time_bound(end)
    if lo is not None: frame=frame.loc[frame.timestamp>=lo]
    if hi is not None: frame=frame.loc[frame.timestamp<=hi]
    if frame.empty: raise ValueError("The Picarro date-time filter contains no data.")
    return frame.copy()


def _downsample(frame: pd.DataFrame,maximum: int=15000)->pd.DataFrame:
    if len(frame)<=maximum:return frame
    return frame.iloc[np.unique(np.linspace(0,len(frame)-1,maximum).astype(int))]


def analyze(data: pd.DataFrame,gas: str,start: str|None=None,end: str|None=None)->dict:
    if gas not in GAS_COLUMNS: raise KeyError(f"Unknown Picarro gas: {gas}")
    column,unit=GAS_COLUMNS[gas]; frame=_slice(data,start,end).dropna(subset=[column]).copy()
    if frame.empty: raise ValueError(f"No Picarro {gas} measurements in selected range.")
    display=_downsample(frame[["timestamp",column]])
    values=frame[column].to_numpy(float); counts,edges=np.histogram(values,bins=70)
    centers=(edges[:-1]+edges[1:])/2
    return {"gas":gas,"unit":unit,"series":{"time":[pd.Timestamp(value).isoformat() for value in display.timestamp],"value":display[column].astype(float).tolist()},"histogram":{"center":centers.astype(float).tolist(),"count":counts.astype(int).tolist()},"stats":{"rows":len(frame),"mean":float(np.mean(values)),"std":float(np.std(values,ddof=1)),"min":float(np.min(values)),"max":float(np.max(values))}}


def comparison_series(data: pd.DataFrame,gas: str,start: str|None=None,end: str|None=None)->pd.Series:
    mapping={"CO2":"CO2_sync","CH4":"CH4_sync","H2O":"H2O_sync"}
    column=mapping[gas]; frame=_slice(data,start,end).dropna(subset=[column])
    return pd.Series(frame[column].to_numpy(float),index=pd.DatetimeIndex(frame.timestamp),name="picarro").sort_index()


def main()->int:
    import argparse
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("input_dir",nargs="?",default=r"C:\My_PC\Zeppelin\Temp\Data_17_20\PICARRO")
    args=parser.parse_args(); _,meta=load_folder(args.input_dir,lambda f,m:print(f"{f:6.1%} {m}")); print(meta); return 0


if __name__=="__main__": raise SystemExit(main())
