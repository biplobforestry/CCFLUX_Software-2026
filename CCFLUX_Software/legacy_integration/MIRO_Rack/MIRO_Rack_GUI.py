"""Trace Gas Measurement - Zeppelin CC flux measurement 2026 local dashboard."""
from __future__ import annotations
import argparse
import json
import math
import os
import shutil
import subprocess
import threading
import time
import traceback
import webbrowser
import warnings
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, Response, jsonify, request
from plotly.offline import get_plotlyjs
from werkzeug.serving import BaseWSGIServer, ThreadedWSGIServer
import numpy as np
import pandas as pd
import export as figure_export
import miro
import picarro

APP_TITLE = "Trace Gas Measurement, Zeppelin CC flux measurement 2026"
DEFAULT_MIRO = r"C:\My_PC\Zeppelin\Temp\Data_17_20\MIRO"
DEFAULT_PICARRO = r"C:\My_PC\Zeppelin\Temp\Data_17_20\PICARRO"
app = Flask(__name__)
LOCK = threading.RLock()
PROJECT_VERSION = 1
STORE = {"miro": None, "picarro": None, "meta": None, "results": None, "project": None, "last_export": None}
JOB = {"running": False, "kind": "", "fraction": 0.0, "message": "Ready", "started": 0.0, "error": None}
MAX_LOG_ENTRIES = 5000
LOGS: list[dict] = []
LOG_COUNTER = 0


def add_log(level: str, context: str, message: str, details: str = "") -> dict:
    """Append one bounded, JSON/HDF-safe diagnostic record."""
    global LOG_COUNTER
    with LOCK:
        LOG_COUNTER += 1
        entry = {
            "id": LOG_COUNTER,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": str(level or "INFO").upper(),
            "context": str(context or "application"),
            "message": str(message or ""),
            "details": str(details or ""),
        }
        LOGS.append(entry)
        if len(LOGS) > MAX_LOG_ENTRIES:
            del LOGS[:-MAX_LOG_ENTRIES]
        return dict(entry)


def log_exception(context: str, exc: Exception) -> None:
    add_log("ERROR", context, f"{type(exc).__name__}: {exc}", traceback.format_exc())


def api_failure(context: str, exc: Exception, status: int = 400):
    log_exception(context, exc)
    return jsonify({"error": str(exc)}), status


def restore_logs(saved_logs) -> None:
    """Merge project diagnostics with messages from the current load session."""
    global LOG_COUNTER
    with LOCK:
        merged, seen = [], set()
        for raw in list(saved_logs or []) + list(LOGS):
            if not isinstance(raw, dict):
                continue
            item = {
                "timestamp_utc": str(raw.get("timestamp_utc", "")),
                "level": str(raw.get("level", "INFO")).upper(),
                "context": str(raw.get("context", "application")),
                "message": str(raw.get("message", "")),
                "details": str(raw.get("details", "")),
            }
            key = tuple(item.values())
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        merged = merged[-MAX_LOG_ENTRIES:]
        for index, item in enumerate(merged, start=1):
            item["id"] = index
        LOGS[:] = merged
        LOG_COUNTER = len(merged)


def set_job(fraction=None, message=None, running=None, error=None, kind=None):
    with LOCK:
        if fraction is not None: JOB["fraction"] = float(max(0,min(1,fraction)))
        if message is not None: JOB["message"] = str(message)
        if running is not None: JOB["running"] = bool(running)
        if error is not None or error is None and running is True: JOB["error"] = error
        if kind is not None: JOB["kind"] = kind


def start_job(kind, target, *args):
    with LOCK:
        if JOB["running"]: raise RuntimeError("Another operation is already running.")
        JOB.update({"running":True,"kind":kind,"fraction":0.0,"message":f"Starting {kind}...","started":time.time(),"error":None})
    add_log("INFO", kind, f"Started {kind} operation")
    def runner():
        try:
            target(*args)
            set_job(1.0, f"{kind.title()} complete", running=False)
            add_log("INFO", kind, f"Completed {kind} operation")
        except Exception as exc:
            log_exception(kind, exc)
            set_job(message=f"{kind.title()} failed", running=False, error=f"{type(exc).__name__}: {exc}")
    threading.Thread(target=runner, daemon=True).start()


def load_worker(miro_path, picarro_path):
    def miro_progress(fraction,message): set_job(.02+.43*fraction,message)
    def picarro_progress(fraction,message): set_job(.48+.48*fraction,message)
    mdata,mmeta=miro.load_folder(miro_path,miro_progress)
    set_job(.47,"MIRO complete; starting Picarro")
    pdata,pmeta=picarro.load_folder(picarro_path,picarro_progress)
    with LOCK:
        STORE["miro"],STORE["picarro"] = mdata,pdata
        STORE["meta"]={"miro":mmeta,"picarro":pmeta,"paths":{"miro":miro_path,"picarro":picarro_path}}
        STORE["results"]=None
        STORE["project"]=None
    add_log("INFO", "loading", f"Loaded MIRO: {mmeta.get('rows', 0):,} rows from {mmeta.get('files_used', 0)} files")
    add_log("INFO", "loading", f"Loaded Picarro: {pmeta.get('rows', 0):,} rows from {pmeta.get('files_used', 0)} files")
    for instrument, metadata in (("MIRO", mmeta), ("Picarro", pmeta)):
        for skipped in metadata.get("skipped_files", []):
            add_log("WARNING", "loading", f"{instrument} skipped file: {skipped.get('file', '')}", skipped.get("reason", ""))
        for duplicate in metadata.get("duplicate_files", []):
            add_log("WARNING", "loading", f"{instrument} duplicate file ignored: {duplicate}")
    set_job(.98,"Preparing filters and gas selectors")


def comparison_payload(mdata,pdata,gas,mstart,mend,pstart,pend):
    unit={"CO2":"ppm","CH4":"ppm","H2O":"%"}[gas]
    m_requested_start=miro._time_bound(mstart) if mstart else mdata.timestamp.min()
    m_requested_end=miro._time_bound(mend) if mend else mdata.timestamp.max()
    p_requested_start=picarro._time_bound(pstart) if pstart else pdata.timestamp.min()
    p_requested_end=picarro._time_bound(pend) if pend else pdata.timestamp.max()
    start_gap=abs((m_requested_start-p_requested_start).total_seconds())
    end_gap=abs((m_requested_end-p_requested_end).total_seconds())
    tolerance_seconds=120.0
    if start_gap>tolerance_seconds or end_gap>tolerance_seconds:
        warning=("Correlation not shown: selected MIRO and Picarro timeframes differ by more than "
                 f"the +/-2-minute tolerance (start difference={start_gap/60:.1f} min, end difference={end_gap/60:.1f} min). "
                 "Individual-instrument analyses are still valid for their selected periods.")
        return {"gas":gas,"unit":unit,"warning":warning,"count":0,"timeframes_compatible":False}

    miro_gas={"CO2":"CO2 wet","CH4":"CH4 wet","H2O":"H2O wet"}[gas]
    ms=miro.comparison_series(mdata,miro_gas,mstart,mend)
    ps=picarro.comparison_series(pdata,gas,pstart,pend)
    mlo,mhi=ms.index.min(),ms.index.max(); plo,phi=ps.index.min(),ps.index.max()
    overlap_start=max(mlo,plo); overlap_end=min(mhi,phi)
    if overlap_start>=overlap_end:
        return {"gas":gas,"unit":unit,"warning":f"Correlation not shown: no overlapping recorded time. MIRO {mlo} to {mhi}; Picarro {plo} to {phi}","count":0,"timeframes_compatible":False}
    warning=None
    if start_gap>0 or end_gap>0:
        warning=(f"Selected timeframes differ within the +/-2-minute tolerance "
                 f"(start difference={start_gap/60:.1f} min, end difference={end_gap/60:.1f} min). "
                 f"Correlation uses common overlap {overlap_start} to {overlap_end}.")
    m1=ms.loc[overlap_start:overlap_end].resample("1min",label="left",closed="left").mean().rename("miro")
    p1=ps.loc[overlap_start:overlap_end].resample("1min",label="left",closed="left").mean().rename("picarro")
    joined=pd.concat([m1,p1],axis=1).dropna()
    if joined.empty:
        return {"gas":gas,"unit":unit,"warning":"Correlation not shown: no paired one-minute means were found in the common timeframe.","count":0,"timeframes_compatible":True}
    rolling_window_minutes=30
    rolling_corr=joined.picarro.rolling(f"{rolling_window_minutes}min",min_periods=15).corr(joined.miro)
    correlation_values=rolling_corr.replace([np.inf,-np.inf],np.nan).dropna().clip(-1.0,1.0).to_numpy(float)
    if correlation_values.size:
        correlation_min=float(correlation_values.min()); correlation_max=float(correlation_values.max())
        bins=min(24,max(8,int(round(math.sqrt(correlation_values.size)))))
        histogram_low,histogram_high=correlation_min,correlation_max
        if histogram_high<=histogram_low:
            delta=max(1e-4,abs(histogram_low)*1e-3)
            histogram_low=max(-1.0,histogram_low-delta); histogram_high=min(1.0,histogram_high+delta)
        frequencies,edges=np.histogram(correlation_values,bins=bins,range=(histogram_low,histogram_high))
        centers=(edges[:-1]+edges[1:])/2.0
        kernel_x=np.arange(-3,4,dtype=float)
        kernel=np.exp(-.5*(kernel_x/1.2)**2); kernel/=kernel.sum()
        smooth_frequencies=np.convolve(frequencies.astype(float),kernel,mode="same")
        correlation_distribution={"window_minutes":rolling_window_minutes,"values":int(correlation_values.size),"min":correlation_min,"max":correlation_max,"center":centers.tolist(),"frequency":frequencies.tolist(),"smooth_frequency":smooth_frequencies.tolist(),"bin_width":float(edges[1]-edges[0])}
    else:
        correlation_distribution={"window_minutes":rolling_window_minutes,"values":0,"min":None,"max":None,"center":[],"frequency":[],"smooth_frequency":[],"bin_width":None}
    if len(joined)>8000: joined=joined.iloc[np.linspace(0,len(joined)-1,8000).astype(int)]
    miro_values=joined.miro.to_numpy(float)
    picarro_values=joined.picarro.to_numpy(float)
    count=len(joined)
    correlation=float(np.corrcoef(picarro_values,miro_values)[0,1]) if count>1 else math.nan
    slope=intercept=slope_se=intercept_se=r_squared=math.nan
    if count>2 and float(np.var(picarro_values))>0:
        slope,intercept=np.polyfit(picarro_values,miro_values,1)
        predicted=slope*picarro_values+intercept
        residuals=miro_values-predicted
        ss_res=float(np.sum(residuals**2))
        ss_tot=float(np.sum((miro_values-np.mean(miro_values))**2))
        r_squared=1.0-ss_res/ss_tot if ss_tot>0 else math.nan
        sxx=float(np.sum((picarro_values-np.mean(picarro_values))**2))
        variance=ss_res/(count-2)
        slope_se=math.sqrt(variance/sxx) if sxx>0 else math.nan
        intercept_se=math.sqrt(variance*(1.0/count+float(np.mean(picarro_values))**2/sxx)) if sxx>0 else math.nan
    denominator=float(np.sum(picarro_values))
    nmb=100.0*float(np.sum(miro_values-picarro_values))/denominator if denominator!=0 else math.nan
    bias=float(np.mean(miro_values-picarro_values))
    def padded_range(values):
        low=float(np.min(values)); high=float(np.max(values))
        span=high-low
        padding=.06*span if span>0 else max(abs(low)*.02,1e-6)
        return [low-padding,high+padding]
    x_range=padded_range(picarro_values)
    y_range=padded_range(miro_values)
    fit_x=[float(picarro_values.min()),float(picarro_values.max())]
    fit_y=[float(slope*fit_x[0]+intercept),float(slope*fit_x[1]+intercept)] if math.isfinite(slope) else []
    return {"gas":gas,"unit":unit,"warning":warning,"count":count,"time":[pd.Timestamp(v).isoformat() for v in joined.index],"miro":miro_values.tolist(),"picarro":picarro_values.tolist(),"x_range":x_range,"y_range":y_range,"fit_x":fit_x,"fit_y":fit_y,"correlation":correlation,"correlation_distribution":correlation_distribution,"r_squared":r_squared,"slope":float(slope),"intercept":float(intercept),"slope_se":float(slope_se),"intercept_se":float(intercept_se),"nmb":nmb,"bias":bias,"timeframes_compatible":True,"averaging_seconds":60}


def analyze_worker(params):
    """Analyse whichever analyzer this flight carried.

    A flight can fly one of the two. Requiring both meant that a Picarro-only
    flight produced no results at all, so its overview - the time series and
    the distribution - stayed empty while the map, which reads the analyzers
    separately, was drawn.
    """
    with LOCK:
        mdata,pdata=STORE["miro"],STORE["picarro"]
    if mdata is None and pdata is None: raise RuntimeError("Load MIRO or Picarro data first.")
    mresult=None; presult=None
    if mdata is not None:
        set_job(.08,"MIRO: applying valve stabilization and trace-gas analysis")
        mresult=miro.analyze(mdata,params["miro_gas"],float(params.get("smooth_seconds",300)),params.get("miro_start"),params.get("miro_end"),30.0)
        for warning in mresult.get("warnings", []):
            add_log("WARNING", "MIRO analysis", warning)
    else:
        add_log("INFO", "MIRO analysis", "This flight carries no MIRO data; analysing Picarro alone.")
    if pdata is not None:
        set_job(.55,"Picarro: preparing time series and distribution")
        presult=picarro.analyze(pdata,params["picarro_gas"],params.get("picarro_start"),params.get("picarro_end"))
    else:
        add_log("INFO", "Picarro analysis", "This flight carries no Picarro data; analysing MIRO alone.")
    with LOCK: STORE["results"]={"miro":mresult,"picarro":presult,"comparison":{},"parameters":params}
    set_job(.97,"Rendering the available trace-gas plots")


def comparison_worker(params):
    with LOCK:
        mdata,pdata=STORE["miro"],STORE["picarro"]
        existing=STORE.get("results")
    # A comparison genuinely needs both; one analyzer has nothing to compare to.
    if mdata is None or pdata is None: raise RuntimeError("A MIRO-Picarro comparison needs both analyzers; this flight carries one.")
    if not existing: raise RuntimeError("Run the MIRO and Picarro analysis before comparison.")
    comparisons={}
    for index,gas in enumerate(("CO2","CH4","H2O"),start=1):
        set_job(.08+.27*(index-1),f"Comparison: pairing one-minute {gas} measurements")
        comparisons[gas]=comparison_payload(mdata,pdata,gas,params.get("miro_start"),params.get("miro_end"),params.get("picarro_start"),params.get("picarro_end"))
    with LOCK:
        STORE["results"]["comparison"]=comparisons
        STORE["results"]["parameters"]={**STORE["results"].get("parameters",{}),**params}
    set_job(.97,"Rendering comparison plots")


def export_worker(scope, output_directory, formats, dpi, params):
    with LOCK:
        mdata, pdata = STORE["miro"], STORE["picarro"]
    if scope == "miro" and mdata is None:
        raise RuntimeError("Load MIRO data before exporting MIRO compounds.")
    if scope == "picarro" and pdata is None:
        raise RuntimeError("Load Picarro data before exporting.")
    if scope == "comparison" and (mdata is None or pdata is None):
        raise RuntimeError("Load MIRO and Picarro data before exporting comparisons.")
    with LOCK:
        STORE["last_export"] = None

    def export_progress(fraction, message):
        set_job(.04 + .92 * fraction, message)

    paths = figure_export.export_figures(
        scope=scope,
        output_directory=output_directory,
        formats=formats,
        dpi=dpi,
        mdata=mdata,
        pdata=pdata,
        params=params or {},
        progress=export_progress,
    )
    with LOCK:
        STORE["last_export"] = paths
    set_job(.98, f"Export complete: {len(paths)} file(s)")

def _normalise_project_path(value):
    path = Path(str(value or "")).expanduser().resolve()
    if path.suffix.lower() not in {".hdf", ".h5", ".hdf5"}:
        path = path.with_suffix(".hdf")
    return path


def _sorted_unique(frame, instrument):
    if "timestamp" not in frame:
        raise ValueError(f"{instrument} project data has no timestamp column.")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp", kind="stable")
    frame = frame.drop_duplicates("timestamp", keep="first").reset_index(drop=True)
    if frame.empty or not frame.timestamp.is_monotonic_increasing:
        raise ValueError(f"{instrument} project timestamps could not be sorted.")
    return frame


def save_project_worker(filename, ui_state):
    path = _normalise_project_path(filename)
    if not path.parent.is_dir():
        raise FileNotFoundError(f"Project folder does not exist: {path.parent}")
    with LOCK:
        mdata, pdata, meta, results = STORE["miro"], STORE["picarro"], STORE["meta"], STORE["results"]
    # A flight can carry one of the two analyzers. Requiring both meant such a
    # flight saved no project at all, so its analysis was gone on reopening.
    present = {name: frame for name, frame in (("miro", mdata), ("picarro", pdata))
               if frame is not None}
    if not present or meta is None:
        raise RuntimeError("Load MIRO or Picarro data before saving a project.")
    estimated = int(sum(frame.memory_usage(deep=True).sum() for frame in present.values()))
    try:
        free_space = shutil.disk_usage(path.parent).free
    except OSError as exc:
        free_space = None
        add_log(
            "WARNING",
            "project save",
            "Could not verify free disk space; the HDF write will still be attempted.",
            f"{type(exc).__name__}: {exc}",
        )
    if free_space is not None and free_space < max(100_000_000, int(estimated * 1.15)):
        raise OSError("Not enough free disk space to save the project safely.")
    with LOCK:
        logs_snapshot = [dict(entry) for entry in LOGS]
    results_to_save = results if bool((ui_state or {}).get("results_current")) else None
    project_info = {
        "version": PROJECT_VERSION,
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "paths": meta.get("paths", {}),
        "meta": meta,
        "state": ui_state or {},
    }
    temp = path.with_name(path.name + ".tmp")
    if temp.exists():
        temp.unlink()
    try:
        set_job(.08, "Project: preparing MIRO data")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with pd.HDFStore(temp, mode="w", complevel=5, complib="blosc:zstd") as store:
                if mdata is not None:
                    store.put("miro", mdata, format="fixed")
                set_job(.42, "Project: saving Picarro data")
                if pdata is not None:
                    store.put("picarro", pdata, format="fixed")
                set_job(.82, "Project: saving settings and analysis")
                store.put("project", pd.DataFrame({"json": [json.dumps(project_info, allow_nan=True)]}), format="fixed")
                store.put("logs", pd.DataFrame({"json": [json.dumps(logs_snapshot, ensure_ascii=False)]}), format="fixed")
                if results_to_save is not None:
                    store.put("results", pd.DataFrame({"json": [json.dumps(results_to_save, allow_nan=True)]}), format="fixed")
        set_job(.96, "Project: finalizing HDF file")
        os.replace(temp, path)
    except Exception as exc:
        raise OSError(f"Could not write the HDF project in {path.parent}. Choose a writable folder with enough free space ({type(exc).__name__}).") from exc
    finally:
        if temp.exists():
            temp.unlink()
    with LOCK:
        STORE["project"] = {"path": str(path), "state": ui_state or {}, "results_available": results_to_save is not None, "saved_at_utc": project_info["saved_at_utc"]}


def load_project_worker(filename):
    path = _normalise_project_path(filename)
    if not path.is_file():
        raise FileNotFoundError(f"Project file does not exist: {path}")
    set_job(.08, "Project: validating HDF structure")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pd.HDFStore(path, mode="r") as store:
            keys = set(store.keys())
            # A project saved from a flight carrying one analyzer holds only
            # that one; either alone is a complete record of what was flown.
            if "/project" not in keys or not keys & {"/miro", "/picarro"}:
                raise ValueError("This HDF file is not a complete Trace Gas Measurement project.")
            info = json.loads(str(store["project"].iloc[0]["json"]))
            if int(info.get("version", 0)) != PROJECT_VERSION:
                raise ValueError(f"Unsupported project version: {info.get('version')}")
            set_job(.25, "Project: loading MIRO data")
            mdata = _sorted_unique(store["miro"], "MIRO") if "/miro" in keys else None
            set_job(.55, "Project: loading Picarro data")
            pdata = _sorted_unique(store["picarro"], "Picarro") if "/picarro" in keys else None
            set_job(.82, "Project: restoring analysis and controls")
            results = json.loads(str(store["results"].iloc[0]["json"])) if "/results" in keys else None
            saved_logs = json.loads(str(store["logs"].iloc[0]["json"])) if "/logs" in keys else []
    restore_logs(saved_logs)
    meta = info.get("meta") or {}
    meta.setdefault("paths", info.get("paths", {}))
    for name, data in (("miro", mdata), ("picarro", pdata)):
        if data is None:
            continue
        instrument = meta.setdefault(name, {})
        instrument.update({"rows": len(data), "start": data.timestamp.min().isoformat(), "end": data.timestamp.max().isoformat(), "sorted": True})
    with LOCK:
        STORE.update({"miro": mdata, "picarro": pdata, "meta": meta, "results": results,
                      "project": {"path": str(path), "state": info.get("state", {}), "results_available": results is not None, "saved_at_utc": info.get("saved_at_utc")}})
    set_job(.97, "Project: ready")


def project_dialog(mode, initial=""):
    escaped = str(initial or "").replace("'", "''")
    if mode == "save":
        script = ("Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.SaveFileDialog; "
                  "$d.Filter='Trace Gas project (*.hdf)|*.hdf'; $d.DefaultExt='hdf'; $d.AddExtension=$true; "
                  f"$d.FileName='{escaped}'; $d.Title='Save Trace Gas Measurement project'; "
                  "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Write($d.FileName)}")
    elif mode == "open":
        script = ("Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.OpenFileDialog; "
                  "$d.Filter='Trace Gas project (*.hdf;*.h5;*.hdf5)|*.hdf;*.h5;*.hdf5'; $d.CheckFileExists=$true; "
                  f"$d.FileName='{escaped}'; $d.Title='Load Trace Gas Measurement project'; "
                  "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Write($d.FileName)}")
    else:
        raise ValueError("Project dialog mode must be open or save.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script], capture_output=True, text=True, creationflags=flags, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Project file dialog failed")
    return result.stdout.strip()


def folder_dialog(initial):
    escaped=str(initial or "").replace("'","''")
    script=(
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        "$owner=New-Object System.Windows.Forms.Form; $owner.Text='Trace Gas Measurement - Select folder'; "
        "$owner.StartPosition='CenterScreen'; $owner.Size=New-Object System.Drawing.Size(1,1); "
        "$owner.ShowInTaskbar=$false; $owner.TopMost=$true; $owner.Opacity=0; $owner.Show(); "
        "$owner.Activate(); $owner.BringToFront(); "
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$d.SelectedPath='{escaped}'; $d.Description='Select instrument data folder'; "
        "$d.ShowNewFolderButton=$false; "
        "$result=$d.ShowDialog($owner); $owner.Close(); $owner.Dispose(); "
        "if($result -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Write($d.SelectedPath)}"
    )
    flags=getattr(subprocess,"CREATE_NO_WINDOW",0)
    result=subprocess.run(["powershell.exe","-NoProfile","-STA","-Command",script],capture_output=True,text=True,creationflags=flags,timeout=600)
    if result.returncode!=0: raise RuntimeError(result.stderr.strip() or "Folder dialog failed")
    return result.stdout.strip()

add_log("INFO", "application", "Trace Gas Measurement server initialized")

@app.get("/")
def home(): return Response(HTML, mimetype="text/html")

@app.get("/plotly.min.js")
def plotly_js(): return Response(get_plotlyjs(),mimetype="application/javascript")

@app.post("/api/browse")
def browse():
    body=request.get_json(silent=True) or {}
    try: return jsonify({"path":folder_dialog(body.get("initial",""))})
    except Exception as exc: return api_failure("browse dialog request", exc)

@app.post("/api/folders")
def api_folders():
    body = request.get_json(silent=True) or {}
    raw = str(body.get("path", "")).strip()
    roots = []
    for letter in range(ord("A"), ord("Z") + 1):
        root = chr(letter) + ":\\"
        try:
            if Path(root).is_dir():
                roots.append(root)
        except OSError:
            continue
    home = Path.home()
    quick_candidates = [
        ("Home", home),
        ("Desktop", home / "Desktop"),
        ("Documents", home / "Documents"),
        ("Downloads", home / "Downloads"),
        ("OneDrive", home / "OneDrive"),
    ]
    quick = []
    seen = set()
    for name, location in quick_candidates:
        try:
            resolved = str(location.resolve())
            if location.is_dir() and resolved.lower() not in seen:
                quick.append({"name": name, "path": resolved})
                seen.add(resolved.lower())
        except OSError:
            continue
    common = {"drives": [{"name": root, "path": root} for root in roots], "quick": quick}
    if not raw:
        return jsonify({**common, "path": "", "parent": None, "folders": common["drives"]})
    try:
        folder = Path(raw).expanduser().resolve()
        if not folder.is_dir():
            raise FileNotFoundError(f"Folder does not exist: {folder}")
        children = []
        project_files = []
        for child in folder.iterdir():
            try:
                if child.is_dir():
                    children.append({"name": child.name, "path": str(child)})
                elif child.is_file() and child.suffix.lower() in {".hdf", ".h5", ".hdf5"}:
                    project_files.append({"name": child.name, "path": str(child), "size": child.stat().st_size})
            except OSError:
                continue
        children.sort(key=lambda item: item["name"].lower())
        project_files.sort(key=lambda item: item["name"].lower())
        parent = str(folder.parent) if folder.parent != folder else ""
        return jsonify({**common, "path": str(folder), "parent": parent, "folders": children[:2000], "project_files": project_files[:1000]})
    except Exception as exc:
        log_exception("folder listing request", exc)
        return jsonify({"error": str(exc), **common}), 400

@app.post("/api/project-dialog")
def api_project_dialog():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"path": project_dialog(body.get("mode", "open"), body.get("initial", ""))})
    except Exception as exc:
        return api_failure("project dialog request", exc)


@app.post("/api/save-project")
def api_save_project():
    body = request.get_json(force=True)
    try:
        start_job("project save", save_project_worker, body.get("path", ""), body.get("state", {}))
        return jsonify({"started": True})
    except Exception as exc:
        return api_failure("project save request", exc, 409)


@app.post("/api/load-project")
def api_load_project():
    body = request.get_json(force=True)
    try:
        start_job("project load", load_project_worker, body.get("path", ""))
        return jsonify({"started": True})
    except Exception as exc:
        return api_failure("project load request", exc, 409)


@app.get("/api/project-state")
def api_project_state():
    with LOCK:
        value = STORE.get("project")
    return jsonify(value or {})


@app.post("/api/exit")
def api_exit():
    def terminate():
        time.sleep(.8)
        os._exit(0)
    if not app.testing:
        threading.Thread(target=terminate, daemon=True).start()
    return jsonify({"exiting": True})


@app.post("/api/load")
def api_load():
    body=request.get_json(force=True)
    try: start_job("loading",load_worker,body.get("miro_path",DEFAULT_MIRO),body.get("picarro_path",DEFAULT_PICARRO)); return jsonify({"started":True})
    except Exception as exc: return api_failure("operation request", exc, 409)

@app.post("/api/analyze")
def api_analyze():
    body=request.get_json(force=True)
    try: start_job("analysis",analyze_worker,body); return jsonify({"started":True})
    except Exception as exc: return api_failure("operation request", exc, 409)
@app.post("/api/compare")
def api_compare():
    body=request.get_json(force=True)
    try: start_job("comparison",comparison_worker,body); return jsonify({"started":True})
    except Exception as exc: return api_failure("operation request", exc, 409)

@app.post("/api/export")
def api_export():
    body=request.get_json(force=True)
    try:
        start_job(
            "export",
            export_worker,
            body.get("scope", ""),
            body.get("output_directory", ""),
            body.get("formats", []),
            body.get("dpi", 1000),
            body.get("parameters", {}),
        )
        return jsonify({"started":True})
    except Exception as exc:
        return api_failure("export request", exc, 409)
@app.get("/api/logs")
def api_logs():
    try:
        limit = max(1, min(MAX_LOG_ENTRIES, int(request.args.get("limit", MAX_LOG_ENTRIES))))
    except (TypeError, ValueError):
        limit = MAX_LOG_ENTRIES
    with LOCK:
        entries = [dict(entry) for entry in LOGS[-limit:]]
        total = len(LOGS)
    errors = sum(entry.get("level") == "ERROR" for entry in entries)
    warnings_count = sum(entry.get("level") == "WARNING" for entry in entries)
    return jsonify({"entries": entries, "total": total, "errors": errors, "warnings": warnings_count})


@app.post("/api/client-log")
def api_client_log():
    body = request.get_json(silent=True) or {}
    context = str(body.get("context", "browser"))[:120]
    message = str(body.get("message", "Unknown browser error"))[:2000]
    details = str(body.get("details", ""))[:20000]
    add_log("ERROR", f"browser: {context}", message, details)
    return jsonify({"logged": True})


@app.get("/api/progress")
def progress():
    with LOCK: state=dict(JOB)
    elapsed=max(0,time.time()-state.get("started",time.time())); fraction=state["fraction"]
    state["percent"]=round(100*fraction,1); state["elapsed_seconds"]=round(elapsed,1)
    state["eta_seconds"]=round(elapsed*(1-fraction)/fraction,1) if state["running"] and fraction>.01 else None
    return jsonify(state)

@app.get("/api/meta")
def meta():
    with LOCK: value=STORE["meta"]
    return jsonify(value or {})

@app.get("/api/results")
def results():
    with LOCK: value=STORE["results"]
    if value is None: return jsonify({"error":"No analysis results available."}),404
    return jsonify(value)

@app.get("/api/export-result")
def export_result():
    with LOCK:
        value = STORE.get("last_export")
    if not value:
        return jsonify({"error":"No exported files are available."}),404
    return jsonify({"paths":value})
HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trace Gas Measurement</title><script src="/plotly.min.js"></script>
<style>
:root{--ink:#082f33;--teal:#0e7773;--mint:#26a69a;--pale:#eef7f6;--line:#b7d0d0;--danger:#b63838;--ok:#16835b;--card:#fff;--shadow:0 7px 22px rgba(4,45,49,.08)}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#e9f4f3,#f7faf9 360px);color:var(--ink);font-family:Inter,Segoe UI,Arial,sans-serif;font-size:clamp(13px,1vw,16px);padding-bottom:58px}
header{position:relative;min-height:104px;background:linear-gradient(110deg,#07383c,#0d5051);color:white;padding:20px clamp(18px,3vw,48px);border-bottom:6px solid var(--mint);display:flex;align-items:flex-end}
.header-kicker{position:absolute;top:17px;right:clamp(18px,3vw,48px);color:#fff;font-size:clamp(22px,1.9vw,31px);font-weight:800;letter-spacing:.02em}.header-title{margin:0;font-size:clamp(30px,3.2vw,48px);font-weight:750}
main{padding:22px;max-width:1900px;margin:auto}.app-footer{position:fixed;left:0;right:0;bottom:0;z-index:15;background:linear-gradient(110deg,#07383c,#0d5051);border-top:6px solid var(--mint);color:#fff;padding:10px clamp(18px,3vw,48px);display:flex;justify-content:center;align-items:center;gap:20px;flex-wrap:wrap;font-size:17px;font-weight:400;letter-spacing:.01em;text-align:center;box-shadow:0 -4px 14px rgba(3,35,37,.18)}.app-footer .version{color:#fff}.card{background:var(--card);border:1px solid var(--line);border-top:5px solid var(--mint);border-radius:16px;box-shadow:var(--shadow);padding:18px;margin-bottom:18px}.card h2{margin:0 0 14px;font-size:clamp(20px,1.5vw,27px)}
.setup-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:15px}.setup-heading h2{margin:0}.setup-tools{display:flex;justify-content:flex-end;align-items:center;gap:9px;flex-wrap:wrap}.output-summary{max-width:360px;color:#557577;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.setup{display:grid;grid-template-columns:repeat(2,minmax(340px,1fr));gap:16px 20px;align-items:end}.folder label,.field label{display:block;font-weight:700;margin-bottom:7px}.pathrow{display:flex;gap:9px;min-width:0}.pathrow input{flex:1;min-width:0}.pathrow input[readonly]{background:#f3f7f7;color:#315c5e;cursor:default;text-overflow:ellipsis}.flight-field{grid-column:1/2;max-width:360px}.flight-field input{width:100%}.setup .actions{grid-column:2/3;justify-content:flex-end;align-self:end}
input,select,button{font:inherit;border:1px solid #78a9aa;border-radius:9px;padding:10px 12px;background:white;color:var(--ink)}button{font-weight:700;cursor:pointer;background:#f0f8f7;white-space:nowrap}button:hover{background:#dff2ef}button:disabled{opacity:1;cursor:not-allowed;background:#dce2e2!important;color:#899394!important;border-color:#c6cece!important;box-shadow:none}.primary{background:var(--teal);color:white;border-color:var(--teal)}.danger{background:#9f2f35;color:white;border-color:#9f2f35}.danger:hover{background:#84252b}.status-red{background:var(--danger);color:#fff;border-color:var(--danger)}.status-green{background:var(--ok);color:#fff;border-color:var(--ok)}
.actions{display:flex;gap:9px;flex-wrap:wrap}.meta{margin-top:15px;padding:13px;background:var(--pale);border:1px solid #d4e5e4;border-radius:10px;color:#315c5e;min-height:52px}.meta-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.meta-item{background:#fff;border-radius:8px;padding:9px 11px;min-width:0}.meta-item b{display:block;color:var(--ink);margin-bottom:3px}.meta-item span{display:block;overflow-wrap:anywhere}.filter-status{grid-column:1/-1;padding:7px 10px;border-radius:7px;background:#e2f1ef;font-weight:700}.section-head{display:flex;align-items:end;justify-content:space-between;gap:12px;flex-wrap:wrap}.controls{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.field input,.field select{min-width:145px}.grid-2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.grid-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.panel{border:1px solid #c7dbdb;border-radius:12px;min-width:0;padding:10px;background:#fff}.panel h3{margin:4px 7px;font-size:16px}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.panel-head select{padding:7px 9px;max-width:270px}.plot{height:390px;width:100%;min-width:0}.plot.tall{height:450px}.correlation-plot{height:510px}.stats{display:grid;grid-template-columns:1fr;gap:9px}.stat{background:var(--pale);padding:12px;border-left:4px solid var(--teal);border-radius:6px}.stat b{display:block;font-size:18px;margin-top:3px}.quick-stats{grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:14px}.miro-warning-list{display:grid;gap:7px;margin-top:14px}.miro-note{color:#315c5e;background:#e9f5f3;border-left:4px solid var(--teal);border-radius:6px;padding:9px 11px;font-size:12px}.warning{color:#8c3a15;background:#fff2df;border:1px solid #e8b572;border-radius:7px;padding:7px;margin:5px;font-size:12px}.comparison-controls{display:flex;align-items:center;justify-content:flex-end;gap:14px;flex-wrap:wrap}.comparison-placeholder{height:100%;display:grid;place-items:center;text-align:center;color:#607d7f;background:linear-gradient(135deg,#f8fbfb,#eef7f6);border-radius:8px;padding:25px}.project-file{border-color:#8db8b5}.project-file.selected{background:#cce9e5;border-color:var(--teal);box-shadow:0 0 0 2px rgba(14,119,115,.18)}
dialog{border:0;border-radius:15px;box-shadow:0 18px 60px rgba(0,0,0,.28);max-width:900px;width:min(94vw,900px);padding:0}.folder-dialog{max-width:1120px;width:min(96vw,1120px)}.folder-dialog .dialog-body{padding:24px}.folder-help{margin:5px 0 15px}.folder-shell{display:grid;grid-template-columns:210px minmax(0,1fr);height:min(64vh,590px);border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff}.folder-sidebar{background:#edf5f4;border-right:1px solid var(--line);padding:13px 9px;overflow:auto}.folder-side-title{font-size:12px;font-weight:800;color:#557577;text-transform:uppercase;letter-spacing:.08em;padding:7px 10px 5px}.folder-side-button{display:flex;align-items:center;gap:9px;width:100%;border:0;background:transparent;text-align:left;padding:9px 10px;font-weight:650}.folder-side-button:hover,.folder-side-button.active{background:#d5ebe8}.folder-main{display:flex;flex-direction:column;min-width:0}.folder-nav{display:grid;grid-template-columns:auto auto auto minmax(180px,1fr) auto;gap:7px;padding:11px;border-bottom:1px solid var(--line);background:#f8fbfb}.folder-nav button{padding:8px 11px}.folder-address{width:100%;font-family:Consolas,monospace;min-width:0}.folder-tools{display:grid;grid-template-columns:minmax(0,1fr) minmax(180px,280px);gap:9px;padding:9px 12px;border-bottom:1px solid #d7e5e4}.folder-breadcrumbs{display:flex;gap:3px;align-items:center;overflow:auto;white-space:nowrap}.crumb{border:0;background:transparent;padding:6px 8px;color:#1e5b5e}.crumb:hover{background:#e0f1ef}.folder-search{width:100%}.folder-list{flex:1;overflow:auto;padding:12px;background:#fbfdfd;display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));grid-auto-rows:min-content;align-content:start;gap:8px}.folder-entry{display:grid;grid-template-columns:30px minmax(0,1fr);align-items:center;gap:8px;min-height:52px;width:100%;text-align:left;background:white;border:1px solid #d5e4e3;padding:8px 10px;white-space:normal}.folder-entry:hover,.folder-entry:focus{background:#dff3f1;border-color:#64aaa6}.folder-icon{font-size:22px}.folder-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.folder-empty{grid-column:1/-1;padding:35px;color:#5a7779;text-align:center}.folder-status{font-size:12px;color:#587779;padding:7px 13px;border-top:1px solid #d7e5e4;background:#f4f9f8}.folder-dialog .dialog-actions{margin-top:13px}@media(max-width:760px){.folder-shell{grid-template-columns:1fr;height:min(68vh,620px)}.folder-sidebar{display:flex;gap:5px;border-right:0;border-bottom:1px solid var(--line);overflow-x:auto;padding:7px}.folder-side-title{display:none}.folder-side-button{width:auto;white-space:nowrap}.folder-nav{grid-template-columns:auto auto auto minmax(120px,1fr) auto}.folder-tools{grid-template-columns:1fr}.folder-list{grid-template-columns:1fr}}dialog::backdrop{background:rgba(3,35,37,.55)}.dialog-body{padding:28px}.dialog-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.filter-panel{padding:16px 18px}.filter-panel small{display:block;margin:9px 0 16px;line-height:1.4;color:#49696b}.filter-panel .field{margin-top:15px}.filter-panel .field+ .field{margin-top:18px}.filter-panel input{display:block;width:100%;min-width:0;font-variant-numeric:tabular-nums;letter-spacing:.02em}.dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
dialog{max-height:calc(100dvh - 20px);overflow:hidden}.dialog-body{max-height:calc(100dvh - 20px);overflow-y:auto;overscroll-behavior:contain}.folder-dialog .dialog-body{height:min(92dvh,820px);display:flex;flex-direction:column;overflow:hidden}.folder-dialog h2,.folder-help{flex:0 0 auto}.folder-shell{height:auto;flex:1;min-height:220px}.folder-sidebar,.folder-main{min-height:0}.folder-list{min-height:0;overflow-y:scroll;overscroll-behavior:contain;touch-action:pan-y;scrollbar-gutter:stable}.folder-dialog .dialog-actions{flex:0 0 auto}.folder-list::-webkit-scrollbar{width:12px}.folder-list::-webkit-scrollbar-track{background:#edf5f4}.folder-list::-webkit-scrollbar-thumb{background:#77aaa7;border:3px solid #edf5f4;border-radius:10px}@media(max-height:700px){.folder-dialog .dialog-body{height:calc(100dvh - 16px);padding:14px}.folder-help{margin:2px 0 8px}.folder-shell{min-height:170px}.folder-dialog .dialog-actions{margin-top:8px}}@media(max-width:760px){.folder-shell{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}.folder-sidebar{display:flex;align-items:center;gap:4px;max-height:60px;overflow-x:auto;overflow-y:hidden;padding:6px;white-space:nowrap}.folder-sidebar #folderQuick,.folder-sidebar #folderDrives{display:flex;align-items:center;gap:4px}.folder-side-button{min-height:38px;width:auto;flex:0 0 auto;padding:7px 9px}.folder-main{min-height:0}.folder-nav{grid-template-columns:auto auto auto minmax(70px,1fr) auto;gap:4px;padding:7px}.folder-nav button{padding:7px 8px}.folder-address{font-size:12px}.folder-tools{padding:7px}.folder-list{padding:8px}}
@media(max-width:420px){.folder-dialog .dialog-body{padding:12px}.folder-help{font-size:12px;line-height:1.35}.folder-dialog h2{font-size:21px}.folder-breadcrumbs{font-size:12px}.folder-dialog .dialog-actions button{padding:9px 10px}}#progressOverlay{position:fixed;inset:0;background:rgba(3,35,37,.62);z-index:20;display:none;align-items:center;justify-content:center}.progress-box{background:white;border-radius:15px;padding:25px;width:min(560px,90vw);box-shadow:0 20px 70px rgba(0,0,0,.3)}.progress-track{height:17px;border-radius:20px;background:#d8e5e5;overflow:hidden;margin:15px 0}.progress-bar{height:100%;background:linear-gradient(90deg,var(--teal),#47c7a9);width:0;transition:width .3s}.progress-line{display:flex;justify-content:space-between;gap:10px}.muted{color:#5a7779}.section-title-actions{display:flex;align-items:center;gap:12px}.small-export{padding:7px 13px;font-size:13px}.export-dialog{max-width:470px}.export-dialog .dialog-body{padding:24px}.format-list{max-height:170px;overflow-y:scroll;border:1px solid var(--line);border-radius:10px;padding:7px;background:#f8fbfb;scrollbar-gutter:stable}.format-option{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:7px;font-weight:700}.format-option:hover{background:#e3f2f0}.format-option input{width:18px;height:18px;min-width:18px}.export-settings{display:grid;grid-template-columns:1fr;gap:12px;margin-top:14px}.export-settings input{width:100%}.log-dialog{width:min(94vw,980px);max-width:980px}.log-dialog .dialog-body{display:flex;flex-direction:column;height:min(84dvh,720px);overflow:hidden}.log-heading{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}.log-heading h2{margin-bottom:5px}.log-summary{font-size:13px;color:#557577}.log-output{flex:1;min-height:260px;margin:10px 0 0;padding:14px;border:1px solid #9dbcbc;border-radius:10px;background:#071f22;color:#d9f4f0;font:12px/1.48 Consolas,"Courier New",monospace;white-space:pre-wrap;overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable}.log-output::-webkit-scrollbar{width:12px}.log-output::-webkit-scrollbar-track{background:#102d30}.log-output::-webkit-scrollbar-thumb{background:#4e8f8b;border:3px solid #102d30;border-radius:10px}.log-dialog .dialog-actions{flex:0 0 auto}
@media(max-width:1100px){.setup{grid-template-columns:1fr}.flight-field,.setup .actions{grid-column:1}.meta-grid{grid-template-columns:1fr}.grid-3{grid-template-columns:1fr}.grid-2{grid-template-columns:1fr}.plot{height:360px}}@media(max-width:600px){.app-footer{justify-content:center;text-align:center}.setup-heading{flex-direction:column}.setup-tools{justify-content:flex-start}.output-summary{width:100%;max-width:none}header{min-height:122px;padding-top:58px}.header-kicker{top:15px;left:18px;right:auto;font-size:22px}.header-title{font-size:30px}main{padding:10px}.card{padding:12px}.dialog-body{padding:18px}.dialog-grid{grid-template-columns:1fr}.pathrow{flex-direction:column}.setup .actions{justify-content:stretch}.setup .actions button{flex:1}}
</style></head><body>
<header><div class="header-kicker">Trace Gas Measurement</div><h1 class="header-title">Zeppelin CCFLUX Campaign 2026</h1></header>
<main>
<section class="card"><div class="setup-heading"><h2>Project setup</h2><div class="setup-tools"><span id="outputSummary" class="output-summary" title="No output directory selected">Output: not selected</span><button id="outputBtn" onclick="browseFolder('output')">Output directory</button><button id="saveProjectBtn" class="primary" disabled onclick="saveProject()">Save Project</button><button id="loadProjectBtn" onclick="browseProject()">Load Project</button><button id="logBtn" onclick="openLogDialog()">Log</button></div></div><input id="outputPath" type="hidden"><div class="setup">
<div class="folder"><label>MIRO data folder (.txt only)</label><div class="pathrow"><input id="miroPath" readonly placeholder="No MIRO folder selected"><button id="selectMiroBtn" onclick="browseFolder('miro')">Select MIRO</button></div></div>
<div class="folder"><label>Picarro data folder (.dat only)</label><div class="pathrow"><input id="picarroPath" readonly placeholder="No Picarro folder selected"><button id="selectPicarroBtn" onclick="browseFolder('picarro')">Select Picarro</button></div></div>
<div class="field flight-field"><label for="flightNo">Flight No. (Optional)</label><input id="flightNo" type="text" maxlength="80" placeholder="e.g., Flight 01" autocomplete="off"></div>
<div class="actions"><button id="loadBtn" class="primary" disabled onclick="loadData()">Load</button><button id="filterBtn" class="status-red" disabled onclick="filterDialog.showModal()">DateTime Filter</button><button id="analyzeBtn" class="primary" disabled onclick="runAnalysis()">Analyze</button><button id="exitBtn" class="danger" onclick="exitDialog.showModal()">Exit</button></div>
</div><div id="metaText" hidden></div></section>

<section class="card" id="miroSection"><div class="section-head"><h2>MIRO analysis</h2><div class="controls"><div class="field"><label>Trace gas</label><select id="miroGas" onchange="markAnalysisDirty()"></select></div><div class="field"><label>Detrending cutoff (s)</label><input id="smoothSeconds" type="number" min="1" value="300" oninput="markAnalysisDirty()"></div><button id="miroUpdate" disabled onclick="runAnalysis()">Update MIRO</button><button id="miroExportBtn" class="small-export" disabled onclick="openExportDialog('miro')">Export</button></div></div>
<div class="grid-2"><div class="panel"><h3>Ambient concentration</h3><div id="miroRaw" class="plot"></div></div><div class="panel"><h3 id="miroResidualTitle">High-pass residual after 300 s detrending</h3><div id="miroResidual" class="plot"></div></div><div class="panel"><div class="panel-head"><h3>Allan deviation</h3><select id="miroAllanMode" onchange="renderMiroAllanMode()"><option value="ambient">Ambient effective time</option><option value="segmented_residual">Segmented ambient residual</option><option value="stable_zero_air">Stable measured zero air</option></select></div><div id="miroAllan" class="plot tall"></div></div><div class="panel"><h3>Power spectral density of high-pass residual</h3><div id="miroPsd" class="plot tall"></div></div></div></section>

<section class="card"><div class="section-head"><h2>Picarro analysis</h2><div class="controls"><div class="field"><label>Trace gas</label><select id="picarroGas" onchange="markAnalysisDirty()"></select></div><button id="picarroUpdate" disabled onclick="runAnalysis()">Update Picarro</button><button id="picarroExportBtn" class="small-export" disabled onclick="openExportDialog('picarro')">Export</button></div></div>
<div class="grid-2"><div class="panel"><h3>Time series</h3><div id="picarroTime" class="plot"></div></div><div class="panel"><h3>Distribution</h3><div id="picarroHist" class="plot"></div></div></div></section>

<section class="card"><div class="section-head"><div class="section-title-actions"><h2>MIRO vs Picarro comparison</h2><button id="comparisonExportBtn" class="small-export" disabled onclick="openExportDialog('comparison')">Export</button></div><div class="comparison-controls"><button id="comparisonProceedBtn" class="primary" disabled onclick="runComparison()">Proceed</button></div></div><div class="grid-3"><div class="panel"><h3>CO2</h3><div id="warnCO2"></div><div id="cmpCO2" class="plot correlation-plot"><div class="comparison-placeholder">Run MIRO/Picarro analysis, then click <b>Proceed</b>.</div></div></div><div class="panel"><h3>CH4</h3><div id="warnCH4"></div><div id="cmpCH4" class="plot correlation-plot"><div class="comparison-placeholder">Run MIRO/Picarro analysis, then click <b>Proceed</b>.</div></div></div><div class="panel"><h3>H2O</h3><div id="warnH2O"></div><div id="cmpH2O" class="plot correlation-plot"><div class="comparison-placeholder">Run MIRO/Picarro analysis, then click <b>Proceed</b>.</div></div></div></div></section>
</main>
<footer class="app-footer"><span>&copy; Biplob Dey, 2026</span><span class="version">Version 1.0_26_07</span></footer>
<dialog id="folderDialog" class="folder-dialog"><div class="dialog-body"><h2 id="folderDialogTitle">Select data folder</h2><p class="muted folder-help">Choose any folder on this PC. Double-click a folder to open it, or enter a full path in the address bar.</p><div class="folder-shell"><aside class="folder-sidebar"><div class="folder-side-title">Quick access</div><div id="folderQuick"></div><div class="folder-side-title">This PC</div><button class="folder-side-button" onclick="navigateFolder('')">&#128421; This PC</button><div id="folderDrives"></div></aside><div class="folder-main"><div class="folder-nav"><button id="folderBackBtn" title="Back" onclick="folderHistoryBack()">&#8592;</button><button id="folderForwardBtn" title="Forward" onclick="folderHistoryForward()">&#8594;</button><button id="folderUpBtn" title="Up one level" onclick="navigateFolder(folderParent)">&#8593;</button><input id="folderAddress" class="folder-address" aria-label="Folder address" placeholder="Enter a folder path" onkeydown="if(event.key==='Enter')navigateFolder(this.value)"><button onclick="navigateFolder(folderAddress.value)">Go</button></div><div class="folder-tools"><div id="folderBreadcrumbs" class="folder-breadcrumbs"></div><input id="folderSearch" class="folder-search" type="search" aria-label="Search folders" placeholder="Search folders in this location" oninput="filterFolderEntries()"></div><div id="folderList" class="folder-list"><div class="folder-empty">Loading folders...</div></div><div id="folderStatus" class="folder-status">Ready</div></div></div><div class="dialog-actions"><button onclick="cancelFolderDialog()">Cancel</button><button id="useFolderBtn" class="primary" onclick="useCurrentFolder()">Use this folder</button></div></div></dialog><dialog id="filterDialog"><div class="dialog-body"><h2>DateTime Filter</h2><p class="muted">Enter recorded time as MM-DD-YYYY HH:mm using the 24-hour clock. No timezone conversion is applied. Correlation requires matching MIRO and Picarro windows within +/-2 minutes.</p><div class="dialog-grid">
<div class="panel filter-panel"><h3>MIRO recorded time</h3><small id="miroAvailable"></small><div class="field"><label>Start</label><input id="miroStart" type="text" inputmode="numeric" placeholder="MM-DD-YYYY HH:mm" maxlength="16" autocomplete="off" spellcheck="false"></div><div class="field"><label>End</label><input id="miroEnd" type="text" inputmode="numeric" placeholder="MM-DD-YYYY HH:mm" maxlength="16" autocomplete="off" spellcheck="false"></div></div>
<div class="panel filter-panel"><h3>Picarro recorded time</h3><small id="picarroAvailable"></small><div class="field"><label>Start</label><input id="picarroStart" type="text" inputmode="numeric" placeholder="MM-DD-YYYY HH:mm" maxlength="16" autocomplete="off" spellcheck="false"></div><div class="field"><label>End</label><input id="picarroEnd" type="text" inputmode="numeric" placeholder="MM-DD-YYYY HH:mm" maxlength="16" autocomplete="off" spellcheck="false"></div></div></div><div class="dialog-actions"><button onclick="resetFilters()">Reset defaults</button><button onclick="filterDialog.close()">Cancel</button><button class="primary" onclick="applyFilters()">Apply</button></div></div></dialog>
<dialog id="exportDialog" class="export-dialog"><div class="dialog-body"><h2 id="exportTitle">Export figure</h2><p id="exportDescription" class="muted">Choose one or more output formats.</p><div id="exportFormatList" class="format-list" role="group" aria-label="Export formats"><label class="format-option"><input type="checkbox" name="exportFormat" value="pdf"> PDF &mdash; publication document</label><label class="format-option"><input type="checkbox" name="exportFormat" value="png" checked> PNG &mdash; high-resolution image</label><label class="format-option"><input type="checkbox" name="exportFormat" value="svg"> SVG &mdash; scalable vector graphic</label></div><div class="export-settings"><div class="field"><label for="exportDpi">Resolution (DPI)</label><input id="exportDpi" type="number" min="72" max="2000" step="1" value="1200"></div></div><div class="dialog-actions"><button onclick="cancelExportDialog()">Cancel</button><button class="primary" onclick="confirmExportOptions()">Export</button></div></div></dialog><dialog id="logDialog" class="log-dialog"><div class="dialog-body"><div class="log-heading"><h2>Diagnostic log</h2><span id="logSummary" class="log-summary">Loading log...</span></div><p class="muted">Operations, warnings, browser errors, Python exceptions, and complete tracebacks are shown here. Logs are saved inside HDF projects.</p><pre id="logOutput" class="log-output" tabindex="0">Loading...</pre><div class="dialog-actions"><button onclick="refreshLogs()">Refresh</button><button onclick="copyDiagnosticLog()">Copy log</button><button class="primary" onclick="closeLogDialog()">Close</button></div></div></dialog><dialog id="exitDialog"><div class="dialog-body"><h2>Exit Trace Gas Measurement?</h2><p>You have the option to save the complete loaded data, filters, selected gases and analysis in an HDF project before closing.</p><div class="warning">Unsaved work will be lost if you exit without saving.</div><div class="dialog-actions"><button onclick="exitDialog.close()">Cancel</button><button class="danger" onclick="exitWithoutSaving()">Exit without saving</button><button class="primary" id="saveExitBtn" onclick="saveAndExit()">Save project and exit</button></div></div></dialog>
<div id="progressOverlay"><div class="progress-box"><h2 id="progressTitle">Working...</h2><div id="progressMessage">Starting</div><div class="progress-track"><div id="progressBar" class="progress-bar"></div></div><div class="progress-line"><b id="progressPercent">0%</b><span id="progressEta" class="muted">Estimating time...</span></div></div></div>
<script>
const app={loaded:false,meta:null,filters:{},poll:null,busy:false,resultsCurrent:false,comparisonCurrent:false,mismatchAccepted:false,filterMessage:'',filtersApplied:false,pendingSaveExit:false,pendingSaveOnly:false,pendingRecoverySave:null,pendingExport:null,lastExport:[],operationId:0,miroResult:null};
const POLL_MS=500;
const config={responsive:true,scrollZoom:true,displaylogo:false,displayModeBar:true,modeBarButtonsToRemove:['lasso2d','select2d']};
const colors={miro:'#0e7773',picarro:'#db7415',blue:'#1756d1',green:'#159447',purple:'#8931ef',grid:'rgba(30,85,88,.15)'};
function baseLayout(xTitle,yTitle){return {margin:{l:68,r:24,t:28,b:58},font:{family:'Inter,Segoe UI,Arial',size:Math.max(10,Math.min(14,innerWidth/115)),color:'#082f33'},paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'#fff',hovermode:'closest',xaxis:{title:{text:xTitle,standoff:10},gridcolor:colors.grid,automargin:true},yaxis:{title:{text:yTitle,standoff:10},gridcolor:colors.grid,automargin:true},legend:{orientation:'h',y:1.14,x:0}}}
function filterInput(value){const m=String(value||'').match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);return m?`${m[2]}-${m[3]}-${m[1]} ${m[4]}:${m[5]}`:''}
function normaliseFilter(value){const text=String(value||'').trim();if(/^\d{2}-\d{2}-\d{4} \d{2}:\d{2}$/.test(text))return text;const legacy=text.match(/^(\d{2})(\d{2})(\d{4}) (\d{2}:\d{2})$/);return legacy?`${legacy[1]}-${legacy[2]}-${legacy[3]} ${legacy[4]}`:filterInput(text)}
function parseFilter(value){const m=String(value||'').match(/^(\d{2})-(\d{2})-(\d{4}) ([01]\d|2[0-3]):([0-5]\d)$/);if(!m)return null;const month=Number(m[1]),day=Number(m[2]),year=Number(m[3]),hour=Number(m[4]),minute=Number(m[5]);const date=new Date(year,month-1,day,hour,minute,0,0);if(date.getFullYear()!==year||date.getMonth()!==month-1||date.getDate()!==day)return null;return date.getTime()}
function filterWindowState(values=app.filters){const ms=parseFilter(values.miro_start),me=parseFilter(values.miro_end),ps=parseFilter(values.picarro_start),pe=parseFilter(values.picarro_end);if([ms,me,ps,pe].some(v=>v===null))return {error:'Use MM-DD-YYYY HH:mm in 24-hour format for all four date-time fields.'};if(ms>=me)return {error:'MIRO start must be before MIRO end.'};if(ps>=pe)return {error:'Picarro start must be before Picarro end.'};const startGap=Math.abs(ms-ps)/60000,endGap=Math.abs(me-pe)/60000;return {startGap,endGap,comparable:startGap<=2&&endGap<=2}}
function confirmMismatch(state){if(state.comparable)return true;if(app.mismatchAccepted)return true;const ok=confirm(`Warning: MIRO and Picarro timeframes differ by more than +/-2 minutes (start difference=${state.startGap.toFixed(1)} min, end difference=${state.endGap.toFixed(1)} min). Individual analyses can proceed, but correlation plots will be hidden because the periods are not comparable. Continue?`);if(ok)app.mismatchAccepted=true;return ok}
function n(v,d=5){return Number(v).toFixed(d)}
function foldersReady(){return Boolean(miroPath.value.trim()&&picarroPath.value.trim())}
function updateSetupState(){selectMiroBtn.disabled=app.busy;selectPicarroBtn.disabled=app.busy;outputBtn.disabled=app.busy;saveProjectBtn.disabled=app.busy||!app.loaded;loadBtn.disabled=app.busy||!foldersReady();loadProjectBtn.disabled=app.busy;exitBtn.disabled=app.busy;filterBtn.disabled=app.busy||!app.loaded;analyzeBtn.disabled=app.busy||!app.loaded||!app.filtersApplied;miroUpdate.disabled=app.busy||!app.loaded||!app.filtersApplied;picarroUpdate.disabled=app.busy||!app.loaded||!app.filtersApplied;miroExportBtn.disabled=app.busy||!app.loaded;picarroExportBtn.disabled=app.busy||!app.loaded;comparisonExportBtn.disabled=app.busy||!app.loaded||!app.comparisonCurrent;comparisonProceedBtn.disabled=app.busy||!app.loaded||!app.filtersApplied||!app.resultsCurrent;miroGas.disabled=!app.loaded||app.busy;smoothSeconds.disabled=!app.loaded||app.busy;picarroGas.disabled=!app.loaded||app.busy;flightNo.disabled=app.busy}
const defaultFolders={miro:'C:\\My_PC\\Zeppelin\\Temp\\Data_17_20\\MIRO',picarro:'C:\\My_PC\\Zeppelin\\Temp\\Data_17_20\\PICARRO',output:'C:\\Users\\B.dey\\Documents'};let folderTarget='',folderParent='',folderCurrentPath='',folderItems=[],folderProjectFiles=[],folderSelectedFile='',folderHistory=[],folderHistoryIndex=-1;
async function browseFolder(which){folderTarget=which;folderSelectedFile='';folderDialogTitle.textContent=which==='output'?'Select output directory':`Select ${which==='miro'?'MIRO':'Picarro'} data folder`;useFolderBtn.textContent=which==='output'?'Use as output directory':'Use this folder';folderDialog.showModal();folderHistory=[];folderHistoryIndex=-1;const input=which==='output'?outputPath:document.getElementById(which+'Path');await navigateFolder(input.value||defaultFolders[which])}
async function browseProject(){folderTarget='project';folderSelectedFile='';folderDialogTitle.textContent='Load HDF project';useFolderBtn.textContent='Load selected project';folderDialog.showModal();folderHistory=[];folderHistoryIndex=-1;await navigateFolder(outputPath.value||defaultFolders.output)}
function cancelFolderDialog(){app.pendingSaveExit=false;app.pendingSaveOnly=false;app.pendingExport=null;folderDialog.close()}
async function navigateFolder(path,record=true){folderList.innerHTML='<div class="folder-empty">Loading folders...</div>';folderStatus.textContent='Loading...';useFolderBtn.disabled=true;folderSelectedFile='';try{const r=await fetchWithTimeout('/api/folders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:String(path||'').trim()})},15000);const j=await r.json();if(!r.ok)throw new Error(j.error||'Could not list folders.');folderCurrentPath=j.path||'';folderParent=j.parent??'';folderItems=j.folders||[];folderProjectFiles=folderTarget==='project'?(j.project_files||[]):[];folderAddress.value=folderCurrentPath;folderSearch.value='';folderUpBtn.disabled=j.parent===null;useFolderBtn.disabled=folderTarget==='project'||!folderCurrentPath;if(record){folderHistory=folderHistory.slice(0,folderHistoryIndex+1);folderHistory.push(folderCurrentPath);folderHistoryIndex=folderHistory.length-1}renderFolderNavigation(j);renderFolderEntries(folderItems,folderProjectFiles);updateFolderHistoryButtons()}catch(error){reportClientError('folder navigation',error);folderList.innerHTML=`<div class="warning">${escapeHtml(error.message)}</div>`;folderStatus.textContent='Could not open this location';useFolderBtn.disabled=true}}
function renderFolderNavigation(data){folderQuick.innerHTML=(data.quick||[]).map(item=>folderSideButton(item,'&#9733;')).join('');folderDrives.innerHTML=(data.drives||[]).map(item=>folderSideButton(item,'&#128190;')).join('');document.querySelectorAll('.folder-side-button[data-path]').forEach(button=>button.addEventListener('click',()=>navigateFolder(button.dataset.path)));renderBreadcrumbs()}
function folderSideButton(item,icon){const active=folderCurrentPath&&item.path.toLowerCase()===folderCurrentPath.toLowerCase()?' active':'';return `<button class="folder-side-button${active}" data-path="${escapeHtml(item.path)}">${icon} <span>${escapeHtml(item.name)}</span></button>`}
function renderBreadcrumbs(){if(!folderCurrentPath){folderBreadcrumbs.innerHTML='<button class="crumb" onclick="navigateFolder(\'\')">This PC</button>';return}const normalized=folderCurrentPath.replaceAll('/','\\'),parts=normalized.split('\\').filter(Boolean);let built='';const crumbs=['<button class="crumb" onclick="navigateFolder(\'\')">This PC</button>'];parts.forEach((part,index)=>{built=index===0&&part.endsWith(':')?part+'\\':built+part+'\\';const target=index===parts.length-1?folderCurrentPath:built;crumbs.push('<span>&rsaquo;</span>',`<button class="crumb" data-path="${escapeHtml(target)}">${escapeHtml(part)}</button>`)});folderBreadcrumbs.innerHTML=crumbs.join('');folderBreadcrumbs.querySelectorAll('.crumb[data-path]').forEach(button=>button.addEventListener('click',()=>navigateFolder(button.dataset.path)))}
function renderFolderEntries(folders,files=[]){const folderHtml=folders.map(item=>`<button class="folder-entry" data-kind="folder" data-path="${escapeHtml(item.path)}" title="Double-click to open ${escapeHtml(item.name)}"><span class="folder-icon">&#128193;</span><span class="folder-name">${escapeHtml(item.name)}</span></button>`).join('');const fileHtml=files.map(item=>`<button class="folder-entry project-file" data-kind="project" data-path="${escapeHtml(item.path)}" title="${escapeHtml(item.name)}"><span class="folder-icon">&#128196;</span><span class="folder-name">${escapeHtml(item.name)}</span></button>`).join('');folderList.innerHTML=folderHtml+fileHtml||'<div class="folder-empty">This folder has no matching items.</div>';folderList.querySelectorAll('.folder-entry[data-kind="folder"]').forEach(button=>{button.addEventListener('dblclick',()=>navigateFolder(button.dataset.path));button.addEventListener('click',()=>{folderStatus.textContent=`Selected folder: ${button.dataset.path} - double-click to open`})});folderList.querySelectorAll('.project-file').forEach(button=>{button.addEventListener('click',()=>selectProjectFile(button));button.addEventListener('dblclick',()=>{selectProjectFile(button);loadSelectedProject()})});folderStatus.textContent=folderTarget==='project'?`${folders.length} folders | ${files.length} HDF projects`:`${folders.length.toLocaleString()} folder${folders.length===1?'':'s'} | Current location: ${folderCurrentPath||'This PC'}`}
function selectProjectFile(button){folderList.querySelectorAll('.project-file').forEach(item=>item.classList.remove('selected'));button.classList.add('selected');folderSelectedFile=button.dataset.path;useFolderBtn.disabled=false;folderStatus.textContent=`Selected project: ${folderSelectedFile}`}
function filterFolderEntries(){const query=folderSearch.value.trim().toLowerCase(),folders=query?folderItems.filter(item=>item.name.toLowerCase().includes(query)):folderItems,files=query?folderProjectFiles.filter(item=>item.name.toLowerCase().includes(query)):folderProjectFiles;renderFolderEntries(folders,files);if(query)folderStatus.textContent=`${folders.length+files.length} matching item(s)`}
async function folderHistoryBack(){if(folderHistoryIndex<=0)return;folderHistoryIndex--;await navigateFolder(folderHistory[folderHistoryIndex],false)}
async function folderHistoryForward(){if(folderHistoryIndex>=folderHistory.length-1)return;folderHistoryIndex++;await navigateFolder(folderHistory[folderHistoryIndex],false)}
function updateFolderHistoryButtons(){folderBackBtn.disabled=folderHistoryIndex<=0;folderForwardBtn.disabled=folderHistoryIndex>=folderHistory.length-1}
function escapeHtml(value){return String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}
function useCurrentFolder(){if(folderTarget==='project'){loadSelectedProject();return}if(!folderCurrentPath)return;if(folderTarget==='output'){outputPath.value=folderCurrentPath;outputSummary.textContent=`Output: ${folderCurrentPath}`;outputSummary.title=folderCurrentPath;folderDialog.close();if(app.pendingExport)startPendingExport();else if(app.pendingSaveExit)startSaveAndExit();else if(app.pendingSaveOnly)startSaveProject();return}const input=document.getElementById(folderTarget+'Path');input.value=folderCurrentPath;folderDialog.close();app.loaded=false;app.resultsCurrent=false;app.comparisonCurrent=false;app.filterMessage='';app.filtersApplied=false;filterBtn.className='status-red';resetComparisonPlots('Run MIRO/Picarro analysis, then click Proceed.');metaText.textContent=foldersReady()?'Both folders selected. Click Load to scan and concatenate the files.':'Select the other instrument folder to enable Load.';updateSetupState()}
async function loadSelectedProject(){if(!folderSelectedFile)return;const path=folderSelectedFile;folderDialog.close();await startOperation('/api/load-project',{path},'Loading HDF project',afterProjectLoad)}
function setBusy(value){app.busy=value;updateSetupState()}function showProgress(title){setBusy(true);progressTitle.textContent=title;progressMessage.textContent='Starting...';progressBar.style.width='0%';progressPercent.textContent='0%';progressEta.textContent='Estimating time...';progressOverlay.style.display='flex'}
function hideProgress(){progressOverlay.style.display='none';setBusy(false)}
function fetchWithTimeout(url,options={},timeoutMs=15000){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);return fetch(url,{...options,signal:controller.signal,cache:'no-store'}).finally(()=>clearTimeout(timer))}
function delay(ms){return new Promise(resolve=>setTimeout(resolve,ms))}
let logRefreshTimer=null;
function diagnosticText(entries){return (entries||[]).map(entry=>{const header=`[${entry.timestamp_utc||''}] ${entry.level||'INFO'} | ${entry.context||'application'}`,details=entry.details?`\n${entry.details}`:'';return `${header}\n${entry.message||''}${details}`}).join('\n\n')||'No diagnostic messages have been recorded.'}
async function refreshLogs(){try{const response=await fetchWithTimeout('/api/logs?limit=5000',{},15000);if(!response.ok)throw new Error('Server returned '+response.status);const data=await response.json();logOutput.textContent=diagnosticText(data.entries);logSummary.textContent=`${data.total||0} entries | ${data.errors||0} errors | ${data.warnings||0} warnings`;logOutput.scrollTop=logOutput.scrollHeight}catch(error){logOutput.textContent='Could not read the diagnostic log.\n'+(error.message||error);logSummary.textContent='Log unavailable'}}
async function openLogDialog(){if(!logDialog.open)logDialog.showModal();await refreshLogs();clearInterval(logRefreshTimer);logRefreshTimer=setInterval(()=>{if(logDialog.open)refreshLogs()},2000)}
function closeLogDialog(){clearInterval(logRefreshTimer);logRefreshTimer=null;logDialog.close()}
async function copyDiagnosticLog(){try{await navigator.clipboard.writeText(logOutput.textContent);logSummary.textContent+=' | copied'}catch(error){alert('Could not copy the log: '+error.message)}}
function reportClientError(context,error,details=''){const message=error?.message||String(error||'Unknown browser error'),stack=details||error?.stack||'';fetch('/api/client-log',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({context,message,details:stack})}).catch(()=>{})}
window.addEventListener('error',event=>reportClientError('window error',event.error||event.message,event.error?.stack||`${event.filename||''}:${event.lineno||''}:${event.colno||''}`));
window.addEventListener('unhandledrejection',event=>reportClientError('unhandled promise rejection',event.reason,event.reason?.stack||''));
async function getJsonWithRetry(url,attempts=8){let lastError;for(let attempt=1;attempt<=attempts;attempt++){try{const response=await fetchWithTimeout(url,{},15000);if(!response.ok)throw new Error('Server returned '+response.status);return await response.json()}catch(error){lastError=error;if(attempt<attempts){progressMessage.textContent=`Connection interrupted; reconnecting (${attempt}/${attempts-1})...`;progressEta.textContent='The analysis is still running';await delay(Math.min(4000,400*2**(attempt-1)))}}}throw lastError}
async function startOperation(url,payload,title,onDone){if(app.busy)return;showProgress(title);const operationStarted=Date.now(),operationId=Date.now()+Math.random();app.operationId=operationId;try{const r=await fetchWithTimeout(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)},20000);const j=await r.json();if(!r.ok)throw new Error(j.error||'Could not start');const monitor=async()=>{if(app.operationId!==operationId)return;try{const p=await getJsonWithRetry('/api/progress');if(app.operationId!==operationId)return;progressBar.style.width=p.percent+'%';progressPercent.textContent=Number(p.percent).toFixed(1)+'%';progressMessage.textContent=p.message||'Working...';progressEta.textContent=p.eta_seconds==null?`Elapsed ${Math.ceil((Date.now()-operationStarted)/1000)} s`:'Estimated '+Math.ceil(p.eta_seconds)+' s remaining';if(p.running){app.poll=setTimeout(monitor,POLL_MS);return}if(p.error){hideProgress();alert(p.error);return}const completion=await onDone();if(completion?.keepProgress)return;progressMessage.textContent=p.message||'Complete';progressBar.style.width='100%';progressPercent.textContent='100.0%';setTimeout(hideProgress,350)}catch(error){if(app.operationId!==operationId)return;reportClientError('operation completion or plot rendering',error);hideProgress();alert('The operation finished, but the results could not be displayed: '+(error?.message||error)+'. Open Log for technical details, then click Analyze again.')}};clearTimeout(app.poll);app.poll=setTimeout(monitor,100)}catch(error){reportClientError('operation request',error);if(app.operationId===operationId)hideProgress();const detail=error.name==='AbortError'?'The server did not accept the operation within 20 seconds.':error.message;alert(detail)}}async function loadData(){if(!foldersReady()){alert('Select both MIRO and Picarro folders first.');return}app.filterMessage='';await startOperation('/api/load',{miro_path:miroPath.value,picarro_path:picarroPath.value},'Loading and scanning instrument files',afterLoad)}
function enableLoadedControls(){app.loaded=true;filterBtn.className='status-green';setBusy(false)}function metaSummary(prefix=''){const m=app.meta;if(!m)return;const status=[String(prefix).replace(/\s*\|\s*$/,''),app.filterMessage].filter(Boolean).join(' | ');metaText.innerHTML=`<div class="meta-grid"><div class="meta-item"><b>MIRO</b><span>${m.miro.files_used??'project'} files &middot; ${m.miro.rows.toLocaleString()} rows</span><span>${filterInput(m.miro.start)} &rarr; ${filterInput(m.miro.end)}</span></div><div class="meta-item"><b>Picarro</b><span>${m.picarro.files_used??'project'} files &middot; ${m.picarro.rows.toLocaleString()} rows</span><span>${filterInput(m.picarro.start)} &rarr; ${filterInput(m.picarro.end)}</span></div>${status?`<div class="filter-status">${status}</div>`:''}</div>`}async function afterLoad(){app.meta=await getJsonWithRetry('/api/meta');app.resultsCurrent=false;app.comparisonCurrent=false;app.filterMessage='';app.filtersApplied=false;clearInstrumentPlots();fillSelect(miroGas,app.meta.miro.gases,'NO2 wet');fillSelect(picarroGas,app.meta.picarro.gases,'CO2 raw');resetFilters();resetComparisonPlots('Apply the DateTime Filter, run Analyze, then click Proceed.');enableLoadedControls();metaSummary()}
function markAnalysisDirty(){app.resultsCurrent=false;app.comparisonCurrent=false;resetComparisonPlots('Analysis settings changed. Run Analyze, then click Proceed.');updateSetupState()}
function currentState(){return {flight_no:flightNo.value.trim(),miro_path:miroPath.value,picarro_path:picarroPath.value,output_path:outputPath.value,miro_gas:miroGas.value,picarro_gas:picarroGas.value,smooth_seconds:Number(smoothSeconds.value),filters:{...app.filters},results_current:app.resultsCurrent,comparison_current:app.comparisonCurrent}}
async function afterProjectLoad(){app.meta=await getJsonWithRetry('/api/meta');app.filterMessage='';clearInstrumentPlots();resetComparisonPlots('Project loaded. Run Analyze, then click Proceed.');const project=await getJsonWithRetry('/api/project-state');const state=project.state||{};flightNo.value=state.flight_no||'';miroPath.value=state.miro_path||app.meta.paths?.miro||'';picarroPath.value=state.picarro_path||app.meta.paths?.picarro||'';outputPath.value=state.output_path||'';outputSummary.textContent=outputPath.value?`Output: ${outputPath.value}`:'Output: not selected';outputSummary.title=outputPath.value||'No output directory selected';fillSelect(miroGas,app.meta.miro.gases,state.miro_gas||'NO2 wet');fillSelect(picarroGas,app.meta.picarro.gases,state.picarro_gas||'CO2 raw');smoothSeconds.value=state.smooth_seconds||300;resetFilters();if(state.filters){for(const [key,value] of Object.entries(state.filters)){const el=document.getElementById(key.replace(/_([a-z])/g,(_,c)=>c.toUpperCase()));if(el&&value)el.value=normaliseFilter(value)}app.filters={miro_start:miroStart.value,miro_end:miroEnd.value,picarro_start:picarroStart.value,picarro_end:picarroEnd.value};app.mismatchAccepted=false;app.filtersApplied=true}enableLoadedControls();metaSummary('Project loaded | ');app.resultsCurrent=Boolean(project.results_available);app.comparisonCurrent=false;if(project.results_available){const result=await getJsonWithRetry('/api/results');if(result?.miro?.series?.time?.length)await renderMiro(result.miro);if(result?.picarro?.series?.time?.length)await renderPicarro(result.picarro);if(result.comparison&&['CO2','CH4','H2O'].every(gas=>result.comparison[gas])){for(const gas of ['CO2','CH4','H2O'])await renderComparison(gas,result.comparison[gas]);app.comparisonCurrent=true}else resetComparisonPlots('Project analysis loaded. Click Proceed to calculate comparisons.')}updateSetupState()}
function projectFilename(){const now=new Date(),part=value=>String(value).padStart(2,'0'),flight=flightNo.value.trim().replace(/[<>:"/\\|?*\x00-\x1F]/g,'_').replace(/\s+/g,'_').replace(/[. ]+$/g,'').slice(0,60),stamp=`${now.getFullYear()}${part(now.getMonth()+1)}${part(now.getDate())}_${part(now.getHours())}${part(now.getMinutes())}${part(now.getSeconds())}`;return `Trace_Gas_Project${flight?'_'+flight:''}_${stamp}.hdf`}
function projectPath(){const directory=outputPath.value.replace(/[\\/]+$/,'');return directory+'\\'+projectFilename()}
async function backendDataReady(){try{const response=await fetchWithTimeout('/api/meta',{},8000);if(!response.ok)return false;const meta=await response.json();return Boolean(meta?.miro?.rows&&meta?.picarro?.rows)}catch(error){return false}}
function openExportDialog(scope){if(!app.loaded){alert('Load the instrument data before exporting.');return}if(scope==='comparison'&&!app.comparisonCurrent){alert('Run Analyze and then Proceed before exporting the comparison figure.');return}app.pendingExport={scope};const flight=flightNo.value.trim(),flightNote=flight?' Title: '+flight+'.':scope==='miro'?' Compound name will be used as title.':' No figure title will be added.';if(scope==='miro'){exportTitle.textContent='Export all MIRO compounds';exportDescription.textContent='Ten compounds; four plots per compound in a 2 x 2 layout (7 x 6 inches). PDF creates one 10-page document; PNG and SVG create 10 individual figures. Every format includes the compound title, page position and timestamp footer.'+flightNote}else if(scope==='comparison'){exportTitle.textContent='Export MIRO vs Picarro comparison';exportDescription.textContent='One row with CO2, CH4 and H2O comparison panels (7 x 3.5 inches), with a timestamp footer.'+flightNote}else{exportTitle.textContent='Export Picarro time series';exportDescription.textContent='Three rows with Picarro CO2 raw, CH4 raw and H2O time series (7 x 6 inches), with a timestamp footer.'+flightNote}exportDialog.showModal()}
function cancelExportDialog(){app.pendingExport=null;exportDialog.close()}
async function confirmExportOptions(){if(!app.pendingExport)return;const formats=[...document.querySelectorAll('input[name="exportFormat"]:checked')].map(item=>item.value),dpi=Number(exportDpi.value);if(!formats.length){alert('Choose at least one format: PDF, PNG, or SVG.');return}if(!Number.isInteger(dpi)||dpi<72||dpi>2000){alert('DPI must be a whole number between 72 and 2000.');return}app.pendingExport={...app.pendingExport,formats,dpi};exportDialog.close();if(!outputPath.value){await browseFolder('output');return}await startPendingExport()}
async function startPendingExport(){const options=app.pendingExport;if(!options||!outputPath.value)return;app.pendingExport=null;await startOperation('/api/export',{scope:options.scope,output_directory:outputPath.value,formats:options.formats,dpi:options.dpi,parameters:analysisPayload()},options.scope==='miro'?'Exporting all MIRO compounds':options.scope==='comparison'?'Exporting comparison figure':'Exporting Picarro figure',afterExport)}
async function afterExport(){const result=await getJsonWithRetry('/api/export-result');app.lastExport=result.paths||[];progressTitle.textContent='Export complete';progressMessage.textContent=app.lastExport.map(path=>path.split(/[\\/]/).pop()).join(', ');progressBar.style.width='100%';progressPercent.textContent='100.0%';progressEta.textContent=`Saved ${app.lastExport.length} file(s) in the output directory`;setTimeout(hideProgress,2600);return {keepProgress:true}}
async function saveProject(){if(!app.loaded){alert('Load MIRO and Picarro data before saving a project.');return}app.pendingSaveOnly=true;app.pendingSaveExit=false;if(!outputPath.value){await browseFolder('output');return}await startSaveProject()}
async function saveAndExit(){exitDialog.close();if(!app.loaded){alert('No loaded data are available to save. Use Exit without saving or load data first.');return}app.pendingSaveExit=true;app.pendingSaveOnly=false;if(!outputPath.value){await browseFolder('output');return}await startSaveAndExit()}
async function startSaveProject(){if(!app.pendingSaveOnly||!outputPath.value)return;app.pendingSaveOnly=false;await saveProjectTo(projectPath(),false,currentState())}
async function startSaveAndExit(){if(!app.pendingSaveExit||!outputPath.value)return;app.pendingSaveExit=false;await saveProjectTo(projectPath(),true,currentState())}
async function saveProjectTo(path,exitAfter,state,skipReadyCheck=false){
 if(!skipReadyCheck&&!await backendDataReady()){
  if(!foldersReady()){alert('The local data session is no longer available. Select both data folders and click Load before saving.');return}
  const restore=confirm('The local data session was restarted, so the server must reload the selected MIRO and Picarro folders before saving. Continue?');
  if(!restore)return;
  app.pendingRecoverySave={path,exitAfter,state};
  await startOperation('/api/load',{miro_path:miroPath.value,picarro_path:picarroPath.value},'Restoring instrument data before saving',async()=>{await afterLoad();const pending=app.pendingRecoverySave;app.pendingRecoverySave=null;progressMessage.textContent='Instrument data restored. Starting project save...';setTimeout(()=>saveProjectTo(pending.path,pending.exitAfter,pending.state,true),550)});
  return
 }
 await startOperation('/api/save-project',{path,state},exitAfter?'Saving HDF project before exit':'Saving HDF project',async()=>{
  progressBar.style.width='100%';progressPercent.textContent='100.0%';progressTitle.textContent='Project saved successfully';progressMessage.textContent=`Saved: ${path}`;
  if(exitAfter){progressEta.textContent='Closing in 1 second...';setTimeout(exitApplication,1000)}else{progressEta.textContent='Complete';app.filterMessage=`Project saved: ${path.split(/[\\/]/).pop()}`;metaSummary();setTimeout(hideProgress,1400)}
  return {keepProgress:true}
 })
}
async function exitWithoutSaving(){exitDialog.close();await exitApplication()}
async function exitApplication(){try{await fetch('/api/exit',{method:'POST'});document.body.innerHTML='<main><section class="card"><h2>Trace Gas Measurement closed</h2><p>The application has exited. You can close this browser tab.</p></section></main>'}catch(error){document.body.innerHTML='<main><section class="card"><h2>Trace Gas Measurement closed</h2><p>You can close this browser tab.</p></section></main>'}}
function fillSelect(select,values,preferred){select.innerHTML='';values.forEach(v=>{const o=document.createElement('option');o.value=v;o.textContent=v;select.appendChild(o)});if(values.includes(preferred))select.value=preferred}
function resetFilters(){if(!app.meta)return;miroStart.value=filterInput(app.meta.miro.start);miroEnd.value=filterInput(app.meta.miro.end);picarroStart.value=filterInput(app.meta.picarro.start);picarroEnd.value=filterInput(app.meta.picarro.end);miroAvailable.textContent=`Available: ${filterInput(app.meta.miro.start)} to ${filterInput(app.meta.miro.end)}`;picarroAvailable.textContent=`Available: ${filterInput(app.meta.picarro.start)} to ${filterInput(app.meta.picarro.end)}`;app.filters={miro_start:miroStart.value,miro_end:miroEnd.value,picarro_start:picarroStart.value,picarro_end:picarroEnd.value};app.mismatchAccepted=false}
function applyFilters(){const next={miro_start:miroStart.value.trim(),miro_end:miroEnd.value.trim(),picarro_start:picarroStart.value.trim(),picarro_end:picarroEnd.value.trim()};const state=filterWindowState(next);if(state.error){alert(state.error);return}app.mismatchAccepted=false;if(!confirmMismatch(state))return;app.filters=next;app.filtersApplied=true;markAnalysisDirty();filterDialog.close();app.filterMessage=state.comparable?'Comparable filters applied (+/-2 min)':'Different timeframes accepted; correlations will be hidden';metaSummary();updateSetupState()}
function analysisPayload(){return {flight_no:flightNo.value.trim(),miro_gas:miroGas.value,picarro_gas:picarroGas.value,smooth_seconds:Number(smoothSeconds.value),...app.filters}}
async function runAnalysis(){if(!app.loaded||app.busy||!app.filtersApplied)return;const filterState=filterWindowState();if(filterState.error){alert(filterState.error);return}if(!confirmMismatch(filterState))return;markAnalysisDirty();const smoothing=Number(smoothSeconds.value);if(!Number.isFinite(smoothing)||smoothing<=0){alert('Smooth / cutoff must be a positive number of seconds.');return}await startOperation('/api/analyze',analysisPayload(),'Analyzing MIRO and Picarro measurements',afterAnalyze)}
async function afterAnalyze(){const result=await getJsonWithRetry('/api/results');const hasMiro=Boolean(result?.miro?.series?.time?.length);const hasPicarro=Boolean(result?.picarro?.series?.time?.length);if(!hasMiro&&!hasPicarro)throw new Error('Neither MIRO nor Picarro returned plottable values for the selected timeframe.');app.resultsCurrent=false;app.comparisonCurrent=false;if(hasMiro)await renderMiro(result.miro);if(hasPicarro)await renderPicarro(result.picarro);app.resultsCurrent=true;resetComparisonPlots('MIRO and Picarro analyses are ready. Click Proceed to calculate correlations.');updateSetupState()}
async function runComparison(){if(!app.loaded||!app.resultsCurrent||app.busy)return;const filterState=filterWindowState();if(filterState.error){alert(filterState.error);return}if(!confirmMismatch(filterState))return;app.comparisonCurrent=false;await startOperation('/api/compare',analysisPayload(),'Analyzing MIRO vs Picarro comparison',afterComparison)}
async function afterComparison(){const result=await getJsonWithRetry('/api/results');for(const gas of ['CO2','CH4','H2O'])await renderComparison(gas,result.comparison[gas]);app.comparisonCurrent=true;updateSetupState()}
function clearInstrumentPlots(){app.miroResult=null;for(const id of ['miroRaw','miroResidual','miroAllan','miroPsd','picarroTime','picarroHist']){const element=document.getElementById(id);if(!element)continue;if(window.Plotly)Plotly.purge(id);element.innerHTML=''}}
function resetComparisonPlots(message){for(const gas of ['CO2','CH4','H2O']){const id='cmp'+gas,element=document.getElementById(id),warn=document.getElementById('warn'+gas);if(!element)continue;if(window.Plotly)Plotly.purge(id);warn.innerHTML='';element.innerHTML=`<div class="comparison-placeholder">${message}</div>`}}
async function renderMiro(m){
 app.miroResult=m;
 const s=m.series,u=m.unit,cutoff=Number(m.smooth_seconds);
 miroResidualTitle.textContent=`High-pass residual after ${Number.isInteger(cutoff)?cutoff:n(cutoff,2)} s detrending`;
 const ambientLayout=baseLayout('Recorded time',`${m.gas} (${u})`);
 ambientLayout.hovermode='x';
 await Plotly.react('miroRaw',[{x:s.time,y:s.ambient,type:'scatter',mode:'lines',name:'Stable ambient',connectgaps:false,line:{color:colors.blue,width:1},hovertemplate:'%{x}<br>%{y:.6g} '+u+'<extra></extra>'}],ambientLayout,config);
 const residualLayout=baseLayout('Recorded time',`High-pass residual (${u})`);
 residualLayout.hovermode='x';
 residualLayout.shapes=[{type:'line',xref:'paper',x0:0,x1:1,y0:0,y1:0,line:{color:'#555',width:1,dash:'dash'}}];
 await Plotly.react('miroResidual',[{x:s.time,y:s.residual,type:'scatter',mode:'lines',name:'Mathematically detrended residual',connectgaps:false,line:{color:colors.blue,width:1},hovertemplate:'%{x}<br>%{y:.6g} '+u+'<extra></extra>'}],residualLayout,config);
 await renderMiroAllanMode();
 const p=m.psd,psdLayout=baseLayout('Frequency (Hz)',`Power spectral density (${u}²/Hz)`);
 psdLayout.xaxis.type='log';psdLayout.xaxis.dtick=1;psdLayout.yaxis.type='log';psdLayout.yaxis.dtick=1;psdLayout.margin={l:82,r:25,t:88,b:68};psdLayout.showlegend=false;
 const psdTicks=logTickIndices(p.frequency||[],5);
 psdLayout.xaxis2={type:'log',overlaying:'x',matches:'x',side:'top',tickvals:psdTicks.map(i=>p.frequency[i]),ticktext:psdTicks.map(i=>periodLabel(1/p.frequency[i])),title:{text:'Period',standoff:7},showgrid:false,zeroline:false,showline:true,ticks:'outside',showticklabels:true,tickangle:0};
 await Plotly.react('miroPsd',[{x:p.frequency,y:p.power,type:'scatter',mode:'lines',name:`Welch PSD (nperseg=${p.nperseg||'n/a'})`,line:{color:colors.purple,width:1.5},hovertemplate:'%{x:.5g} Hz<br>%{y:.5g} '+u+'²/Hz<extra></extra>'},{x:p.frequency,y:p.power,type:'scatter',mode:'markers',xaxis:'x2',showlegend:false,hoverinfo:'skip',marker:{opacity:0,size:1}}],psdLayout,config);

}
async function renderMiroAllanMode(){
 const m=app.miroResult;if(!m)return;
 const options={
  ambient:{data:m.allan,key:'ambient',name:'Background-corrected ambient',axis:'Averaging time, τ (s)'},
  segmented_residual:{data:m.diagnostic_allan?.segmented_residual,key:'residual',name:'Segmented high-pass ambient residual',axis:'Averaging time, τ (s)'},
  stable_zero_air:{data:m.diagnostic_allan?.stable_zero_air,key:'zero_air',name:'Stable measured zero air',axis:'Averaging time, τ (s)'}
 };
 for(const option of miroAllanMode.options){const entry=options[option.value],values=entry?.data?.[entry.key]||[];option.disabled=!values.length}
 let mode=miroAllanMode.value;if(miroAllanMode.selectedOptions[0]?.disabled){mode='ambient';miroAllanMode.value=mode}
 const entry=options[mode],a=entry.data||{tau:[],samples:[],white_noise:[]},values=a[entry.key]||[],u=m.unit;
 const layout=baseLayout(entry.axis,`Allan deviation (${u})`);layout.xaxis.type='log';layout.xaxis.dtick=1;layout.yaxis.type='log';layout.yaxis.dtick=1;layout.margin={l:76,r:25,t:88,b:82};layout.legend={orientation:'h',x:.02,y:.98,xanchor:'left',yanchor:'top',bgcolor:'rgba(255,255,255,.78)'};
 const ticks=(a.tau||[]).length?Array.from(new Set(npIndices(a.tau.length,5))).map(i=>i):[];
 layout.xaxis2={type:'log',overlaying:'x',matches:'x',side:'top',tickvals:ticks.map(i=>a.tau[i]),ticktext:ticks.map(i=>a.samples[i]),title:{text:'Samples per averaging block, m',standoff:7},showgrid:false,zeroline:false,showline:true,ticks:'outside',showticklabels:true};
 const custom=(a.differences||[]).length?a.differences.map(value=>[value]):undefined;
 await Plotly.react('miroAllan',[{x:a.tau,y:values,type:'scatter',mode:'lines+markers',name:entry.name,line:{color:colors.blue,width:1.5},marker:{size:5,symbol:'circle-open'},customdata:custom,hovertemplate:custom?'τ=%{x:.5g} s<br>σA=%{y:.5g} '+u+'<br>within-segment differences=%{customdata[0]}<extra></extra>':'τ=%{x:.5g} s<br>σA=%{y:.5g} '+u+'<extra></extra>'},{x:a.tau,y:a.white_noise,type:'scatter',mode:'lines',name:'Reference slope ∝ τ<sup>−1/2</sup> (not a fit)',line:{color:'#333',dash:'dash',width:1.4}},{x:a.tau,y:values,type:'scatter',mode:'markers',xaxis:'x2',showlegend:false,hoverinfo:'skip',marker:{opacity:0,size:1}}],layout,config);
}
function npIndices(length,count){if(length<=1)return [0];return Array.from({length:Math.min(count,length)},(_,i)=>Math.round(i*(length-1)/(Math.min(count,length)-1)))} function logTickIndices(values,count){if(!values.length)return [];if(values.length===1)return [0];const lo=Math.log(Number(values[0])),hi=Math.log(Number(values[values.length-1])),indices=[];for(let i=0;i<Math.min(count,values.length);i++){const target=Math.exp(lo+i*(hi-lo)/(Math.min(count,values.length)-1));let best=0,distance=Infinity;for(let j=0;j<values.length;j++){const candidate=Math.abs(Math.log(Number(values[j]))-Math.log(target));if(candidate<distance){distance=candidate;best=j}}indices.push(best)}return Array.from(new Set(indices))}
function periodLabel(seconds){if(!Number.isFinite(seconds))return '';if(seconds>=3600)return `${(seconds/3600).toPrecision(3)} h`;if(seconds>=60)return `${(seconds/60).toPrecision(3)} min`;return `${seconds.toPrecision(3)} s`}async function renderPicarro(p){const l=baseLayout('Time',p.gas+' ('+p.unit+')');await Plotly.react('picarroTime',[{x:p.series.time,y:p.series.value,type:'scatter',mode:'lines',name:p.gas,line:{color:colors.picarro,width:1}}],l,config);await Plotly.react('picarroHist',[{x:p.histogram.center,y:p.histogram.count,type:'bar',name:p.gas,marker:{color:colors.picarro}}],baseLayout(p.gas+' ('+p.unit+')','Count'),config)}
async function renderComparison(gas,c){
 const warn=document.getElementById('warn'+gas),id='cmp'+gas;
 warn.innerHTML=c.warning?`<div class="warning">${c.warning}</div>`:'';
 if(!c.count){Plotly.purge(id);document.getElementById(id).innerHTML='<p class="warning">Correlation unavailable for the selected timeframes.</p>';return}
 document.getElementById(id).innerHTML='';
 const unit=c.unit,x=c.picarro.map(Number),y=c.miro.map(Number);
 const paddedRange=values=>{const lo=Math.min(...values),hi=Math.max(...values),span=hi-lo,pad=span>0?.06*span:Math.max(Math.abs(lo)*.02,1e-6);return [lo-pad,hi+pad]};
 const xRange=Array.isArray(c.x_range)?c.x_range.map(Number):paddedRange(x);
 const yRange=Array.isArray(c.y_range)?c.y_range.map(Number):paddedRange(y);
 const picarroGreen='#159447',miroPurple='#8931ef',fitRed='#e53935';
 const traces=[{x,y,type:'scatter',mode:'markers',name:'Paired Picarro-MIRO values',marker:{size:6,color:miroPurple,opacity:.82,line:{color:picarroGreen,width:1.35}},customdata:c.time,hovertemplate:'Recorded time %{customdata}<br><span style="color:#159447">Picarro %{x:.5g} '+unit+'</span><br><span style="color:#8931ef">MIRO %{y:.5g} '+unit+'</span><extra></extra>'}];
 if(c.fit_x?.length===2&&c.fit_y?.length===2)traces.push({x:c.fit_x,y:c.fit_y,type:'scatter',mode:'lines',name:'Fitted regression',line:{color:fitRed,width:3},hoverinfo:'skip'});
 const dist=c.correlation_distribution||{},corrMin=Number(dist.min),corrMax=Number(dist.max),hasCorrelationDistribution=Array.isArray(dist.center)&&dist.center.length>0&&Number.isFinite(corrMin)&&Number.isFinite(corrMax);
 let corrRange=[-1,1];
 if(hasCorrelationDistribution){if(corrMin<corrMax)corrRange=[corrMin,corrMax];else{const delta=Math.max(1e-4,Math.abs(corrMin)*1e-3);corrRange=[Math.max(-1,corrMin-delta),Math.min(1,corrMax+delta)]}traces.push({x:dist.center,y:dist.frequency,width:Number(dist.bin_width)*.9,type:'bar',xaxis:'x2',yaxis:'y2',name:'Rolling correlation frequency',marker:{color:'rgba(38,166,154,.48)',line:{width:0}},hovertemplate:'Correlation r %{x:.4f}<br>Count %{y}<extra></extra>'});traces.push({x:dist.center,y:dist.smooth_frequency,type:'scatter',mode:'lines',xaxis:'x2',yaxis:'y2',name:'Smoothed frequency',line:{color:'#087f78',width:2.5,shape:'spline',smoothing:.65},hovertemplate:'Correlation r %{x:.4f}<br>Smoothed count %{y:.1f}<extra></extra>'})}
 const statistic=(value,digits=3)=>Number.isFinite(Number(value))?Number(value).toFixed(digits):'n/a';
 const slope=statistic(c.slope),slopeSe=statistic(c.slope_se),offset=statistic(c.intercept),offsetSe=statistic(c.intercept_se),rSquared=statistic(c.r_squared);
 const statistics=`slope = ${slope}${slopeSe==='n/a'?'':` ± ${slopeSe}`}<br>offset = ${offset}${offsetSe==='n/a'?'':` ± ${offsetSe}`} ${unit}<br>R<sup>2</sup> = ${rSquared}`;
 const annotations=[{text:statistics,xref:'paper',yref:'paper',x:.02,y:.98,xanchor:'left',yanchor:'top',align:'left',showarrow:false,bgcolor:'rgba(255,255,255,.78)',borderpad:3,font:{size:12,color:'#082f33'}}];
 const layout={margin:{l:76,r:20,t:22,b:80},font:{family:'Inter,Segoe UI,Arial',size:chartFontSize(),color:'#082f33'},paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'#fff',hovermode:'closest',showlegend:false,barmode:'overlay',bargap:.08,xaxis:{title:{text:`Picarro ${gas} (${unit})`,standoff:12,font:{size:13,color:picarroGreen}},tickfont:{color:picarroGreen},range:xRange,gridcolor:colors.grid,zeroline:false,automargin:true},yaxis:{title:{text:`MIRO ${gas} (${unit})`,standoff:12,font:{size:13,color:miroPurple}},tickfont:{color:miroPurple},range:yRange,gridcolor:colors.grid,zeroline:false,automargin:true},xaxis2:{domain:[.66,.98],anchor:'y2',title:{text:'Correlation',standoff:4,font:{size:9}},tickfont:{size:8},ticks:'outside',ticklen:3,nticks:3,tickangle:0,range:corrRange,showgrid:false,zeroline:false,showline:true,linecolor:'#082f33',linewidth:1},yaxis2:{domain:[.17,.40],anchor:'x2',title:{text:'Count',standoff:3,font:{size:9}},tickfont:{size:8},ticks:'outside',ticklen:3,nticks:3,showgrid:false,zeroline:false,showline:true,linecolor:'#082f33',linewidth:1},shapes:hasCorrelationDistribution?[{type:'rect',xref:'paper',yref:'paper',x0:.63,x1:1,y0:.11,y1:.44,fillcolor:'rgba(255,255,255,.90)',line:{width:0},layer:'below'}]:[],annotations};
 await Plotly.react(id,traces,layout,config)
}
async function restoreSession(){updateSetupState();try{const meta=await getJsonWithRetry('/api/meta',5);if(meta?.miro?.rows&&meta?.picarro?.rows){app.meta=meta;miroPath.value=meta.paths?.miro||'';picarroPath.value=meta.paths?.picarro||'';fillSelect(miroGas,meta.miro.gases,'NO2 wet');fillSelect(picarroGas,meta.picarro.gases,'CO2 raw');resetFilters();enableLoadedControls();metaSummary('Loaded session | ')}}catch(error){console.warn('Could not restore loaded session',error);reportClientError('session restore',error)}}restoreSession();let resizeFrame;function chartFontSize(){return Math.max(10,Math.min(14,innerWidth/115))}const resizeObserver=new ResizeObserver(()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>document.querySelectorAll('.js-plotly-plot').forEach(el=>{Plotly.Plots.resize(el);Plotly.relayout(el,{'font.size':chartFontSize()})}))});document.querySelectorAll('.panel').forEach(p=>resizeObserver.observe(p));
</script></body></html>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8765);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    BaseWSGIServer.allow_reuse_address = False
    ThreadedWSGIServer.allow_reuse_address = False
    if not args.no_browser: threading.Timer(1.0,lambda:webbrowser.open(f'http://{args.host}:{args.port}')).start()
    app.run(host=args.host,port=args.port,debug=False,threaded=True,use_reloader=False)


if __name__=='__main__': main()
