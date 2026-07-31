"""Picarro .dat loading and analysis for the Zeppelin trace-gas dashboard."""
from __future__ import annotations
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
Progress = Callable[[float, str], None]


def discover_files(root: str | Path) -> list[Path]:
    folder = Path(root).expanduser().resolve()
    if not folder.is_dir(): raise FileNotFoundError(f"Picarro folder does not exist: {folder}")
    files = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".dat")
    if not files: raise FileNotFoundError(f"No Picarro .dat files found under {folder}")
    return files


def load_folder(root: str | Path, progress: Progress | None=None) -> tuple[pd.DataFrame,dict]:
    files = discover_files(root); frames=[]; skipped_files=[]
    for index,path in enumerate(files,start=1):
        if progress: progress((index-1)/len(files), f"Picarro: reading {index}/{len(files)} - {path.name}")
        try:
            try:
                frame = pd.read_csv(path, sep=r"\s+", usecols=["DATE","TIME",*SOURCE_COLUMNS])
            except ValueError:
                header = pd.read_csv(path, sep=r"\s+", nrows=0).columns
                available = [column for column in SOURCE_COLUMNS if column in header]
                if "DATE" not in header or "TIME" not in header or not available:
                    skipped_files.append({"file": str(path), "reason": "Missing timestamp or gas columns"}); continue
                frame = pd.read_csv(path, sep=r"\s+", usecols=["DATE","TIME",*available])
        except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
            skipped_files.append({"file": str(path), "reason": str(exc)}); continue
        # Keep the instrument clock exactly as written in DATE and TIME.
        # These values remain timezone-naive: no localization, UTC conversion,
        # or clock shift is applied anywhere in the workflow.
        frame["timestamp"] = pd.to_datetime(frame["DATE"].astype(str)+" "+frame["TIME"].astype(str), errors="coerce")
        frame = frame.drop(columns=["DATE","TIME"]).dropna(subset=["timestamp"])
        for column in SOURCE_COLUMNS:
            if column in frame: frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["source_file"] = str(path.relative_to(Path(root).resolve()))
        frames.append(frame)
    if not frames: raise RuntimeError("No readable timestamped Picarro records were found.")
    data = pd.concat(frames,ignore_index=True,sort=False).sort_values("timestamp",kind="stable")
    before=len(data); data=data.drop_duplicates("timestamp",keep="first").reset_index(drop=True)
    if not data.timestamp.is_monotonic_increasing:
        raise RuntimeError("Picarro timestamps could not be sorted chronologically.")
    gases=[name for name,(column,_) in GAS_COLUMNS.items() if column in data and data[column].notna().any()]
    if progress: progress(1.0,"Picarro: concatenation and timestamp validation complete")
    return data,{"files_found":len(files),"files_used":len(frames),"skipped_files":skipped_files,"duplicate_timestamps_removed":before-len(data),"sorted":True,"rows":len(data),"start":data.timestamp.min().isoformat(),"end":data.timestamp.max().isoformat(),"gases":gases}


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
