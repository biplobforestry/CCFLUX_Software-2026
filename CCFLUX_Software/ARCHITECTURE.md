# CC-FLUX 2026 — Architecture Overview

Post-flight scientific payload review for the CC-FLUX Zeppelin campaign. A local
Python HTTP server serves a browser dashboard, discovers instrument data in
read-only raw folders, schedules bounded processing jobs, and writes all products
into an operator-selected Output Folder.

Scale: ~30 400 lines of Python (of which ~7 700 in `legacy_integration/`), ~6 800
lines of frontend HTML/JS/CSS, 11 registered instruments, 13 processing jobs.

---

## 1. Runtime topology

```
┌─ Browser (dashboard.html + dashboard.js, Leaflet, Plotly) ──────────────┐
│  polls /api/scan (250 ms) · /api/logs (700 ms) · /api/queue (800 ms)    │
│  instrument pages: /noseboom /gopro /flir /opc /partector /ins_gimbal   │
│                    /sif /miro_rack /miro_rack/map                       │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ HTTP, 127.0.0.1, no auth
┌──────────────────────────────▼──────────────────────────────────────────┐
│ app/server.py — ThreadingHTTPServer, hand-rolled route table            │
│   DashboardHTTPServer owns: backend + MiroRackBridge + asset root       │
└───────────┬───────────────────────────────────┬─────────────────────────┘
            │                                   │
┌───────────▼─────────────────────┐  ┌──────────▼──────────────────────────┐
│ app/scan_backend.py             │  │ app/miro_rack_bridge.py             │
│ DashboardScanBackend (4 823 ln) │  │ Wraps the legacy MIRO Rack Flask app │
│  · the single stateful hub      │  │  · HTML rewriting + request proxy   │
│  · one RLock guards everything  │  │  · 1 Hz Mapview georeferencing      │
└───────────┬─────────────────────┘  └─────────────────────────────────────┘
            │
   ┌────────┴──────────────────────────────────────────────┐
   │                        core/                          │
   │  scanner · detection_configuration · time_extraction  │
   │  dashboard_time · processing_manager · priority_mgr   │
   │  resource_manager · logging_manager · flight_project  │
   │  gopro/flir_georeference · camera_level2              │
   └────────┬──────────────────────────────────────────────┘
            │
   ┌────────▼────────────┐        ┌──────────────────────────────┐
   │   instruments/*     │───────▶│   legacy_integration/*       │
   │  adapter.py         │ import │  validated science, unchanged │
   │  legacy_bridge.py   │ by path│  loaded via importlib spec   │
   └─────────────────────┘        └──────────────────────────────┘
```

### Layer contract

| Layer | Owns | Must not |
|---|---|---|
| `app/server.py` | HTTP routing, static assets, JSON encoding | hold science state |
| `app/scan_backend.py` | all mutable session state, job task closures | contain science algorithms |
| `core/` | discovery, time, scheduling, resources, persistence | import an instrument adapter |
| `instruments/*/adapter.py` | the `InstrumentBase` contract, I/O bounds, output paths | modify raw data |
| `instruments/*/legacy_bridge.py` | `importlib` load of the legacy module | edit legacy source |
| `legacy_integration/` | validated campaign science | be refactored |

---

## 2. Operator workflow → code path

```
Select Flight Folder      → backend.select_folders()      → native picker (osascript / PowerShell / tkinter)
Select Camera Folder      → backend.select_camera_folder() → independence check vs. flight root
Select Output Folder      → backend.select_output_folder() → write probe + independence check
Initial Check             → backend.start_scan()          → 1 daemon thread → FlightFolderScanner
  ├─ inventory pass       → _count_files()                → full tree walk, counts only
  ├─ discovery pass       → scan()                        → name/header/EXIF evidence per file
  └─ validation pass      → _apply_report()               → TimestampExtractor per instrument
Time Filter               → DashboardTimeState            → Noseboom is the anchor; 2 min/1 min edge trim
Processing Priority       → ProcessingPriorityQueue       → enable/disable/reorder → priority 1–3
Start Processing          → start_processing()            → binds task closures, ProcessingScheduler.dispatch()
Remote Sensing            → start_remote_sensing()        → camera_metadata worker group only
FLIR Level 2              → start_detailed_processing()   → camera_detailed group, requires ≥4 workers
Save .ccflux              → FlightProjectStore.save_project()
```

---

## 3. Discovery pipeline (`core/scanner.py`)

Configuration-driven, streaming, cancellable. Two YAML/JSON documents with a
matched `schema_version` drive everything:

- `configs/instrument_detection.yaml` — `instrument_id → pattern_set`, plus a
  `requires_confirmation` + `todo` mechanism for incomplete rules.
- `configs/file_patterns.yaml` — per-instrument evidence: folder names, filename
  prefixes/suffixes, extensions, required/optional CSV columns, JSON metadata
  keys, EXIF tags, header text, exclusion globs.

Scoring in `_evaluate_rule`: extension 0.10 · specific folder 0.25 (generic 0.05)
· filename prefix 0.25 · suffix 0.05 · full required column set 0.35 · optional
columns ≤0.10 · header text 0.25 · metadata key 0.20 · EXIF tag 0.25, capped at
1.0. A match is rejected unless there is **strong identity** (prefix, complete
required schema, header, metadata, EXIF, or a non-generic suffix) or a
non-generic folder plus a matching extension. Generic containers (`2026*`,
`HATCH-BOX`, `influxdb`, any glob) deliberately do not confer identity.

Bounds: 64 KiB header read, 5 sampled rows, 20 sampled files per candidate, 50
retained diagnostics per class, symlinks skipped, progress throttled to 10 Hz
after the first 5 files. Camera instruments (`micasense`, `flir`, `gopro`) retain
only samples; their adapters expand lazily.

Flight and Camera roots are scanned as two independent channels through a
`ThreadPoolExecutor(max_workers=1)` and merged by `_merge_scan_reports`, which
re-flags cross-root duplicates as ambiguous.

---

## 4. Time model (`core/time_extraction.py`, `core/dashboard_time.py`)

Raw timestamps are never rewritten. Each instrument has a declared parsing
strategy:

| Instrument | Source of truth | Timezone rule |
|---|---|---|
| Noseboom | `Airflow_UTCcorr_Nanoseconds_ns` | Unix epoch ns → UTC (authoritative) |
| MIRO | `t-stamp`, `;`-delimited `.txt` only | `%d.%m.%Y %H:%M:%S,%f`, assume UTC. TDMS is detected but never parsed |
| Picarro | whitespace `DATE` + `TIME` | assume UTC by delivery convention |
| OPC HBX-4/5, Partector | `_time` | as written |
| INS Gimbal | `_time` | single Influx export rotation auto-corrected |
| SIF | `datetime [UTC]` or raw `cycle_id;date;time` | assume UTC |
| GoPro | EXIF `DateTimeOriginal` | **Europe/Berlin → UTC at detection time** |
| FLIR | regex over first/last 16 MiB of the JSON stream | assume UTC |
| MicaSense | none configured | warns explicitly; filesystem mtime is never used |

`DashboardTimeState` derives the global interval from the **Noseboom anchor**,
trims 2 min from the start and 1 min from the end of continuous sensors
(`UNTRIMMED_INSTRUMENTS = {sif, flir, gopro, micasense}` are exempt), and computes
a per-instrument `availability_percentage` and `outside_selected_range` flag
against the selected interval. Per-instrument overrides exist in the model but
are rejected at the API boundary — one global filter is the design.

---

## 5. Processing model (`core/processing_manager.py`, `core/priority_manager.py`)

Three isolated `ThreadPoolExecutor` pools so a slow camera job cannot starve fast
science:

| Group | Capacity rule | Jobs |
|---|---|---|
| `FAST_SCIENCE` | `total − metadata − detailed` | noseboom, miro, picarro, opc_hbx4, opc_hbx5, partector, ins_gimbal, sif |
| `CAMERA_METADATA` | 1 if `total ≥ 2`, else 0 | micasense_quick, flir_quick, gopro_quick |
| `CAMERA_DETAILED` | 1 if `total ≥ 4`, else 0 | micasense_detailed, flir_detailed |

Jobs are pure data (`ProcessingJob`) with a `task: Callable[[ProcessingContext], JobOutcome|None]`
closure injected by the backend at start time — `core/` never imports an adapter.
Cancellation is cooperative through `ProcessingContext.check_cancelled()`.
Reordering re-derives priority bands at 55 % / 80 % of the queue length.

Every task follows the same shape:

```python
with self._lock:                       # snapshot report/project/interval
    ...
adapter = XAdapter(output_root=self._run_output_root(project, id), ...)
adapter.report_progress(lambda u: context.report_progress(...))
loaded  = adapter.load(InputCandidate(...))
result  = adapter.process_quicklook(loaded, {...options...})
figures = adapter.create_plots(result, adapter.output_root)
outputs = adapter.export_results(result, adapter.output_root, ("csv", "json"))
browser = adapter.export_browser_data(result, adapter.output_root)
with self._lock:                       # publish output paths + browser payload
    ...
```

`_run_output_root` stamps every attempt with `%Y%m%dT%H%M%S_%fZ`, so retries never
collide. Adapters guard writes with `_assert_output_path` (containment) and
`FileExistsError` (never silently overwrite).

---

## 6. Persistence (`core/flight_project.py`)

`flight_project.ccflux` is human-readable JSON, schema version 1, written
atomically (`.tmp` → `replace`). It stores folder selections, per-instrument
detection state and source file lists, UTC and selected intervals, resource
allocation, queue order, completed/failed/cancelled job IDs, instrument options,
output locations, and SHA-optional raw-file fingerprints.

Output tree:

```
<Output Folder>/<flight_id>/
  project/flight_project.ccflux
  processed/<instrument>/runs/<UTC stamp>/…
  quicklooks/{noseboom,gopro,flir,sif,opc,partector,ins_gimbal}_browser.json
  quicklooks/miro_rack_session.hdf · miro_rack_map_1hz.json
  reports/noseboom_statistics/ · exports/noseboom/ · logs/processing.jsonl
```

On open, `detect_changed_raw_files` compares size + mtime (+ SHA-256 in checksum
mode). Unchanged → the saved scan is reused and rebuilt into a synthetic
`ScanReport`; changed → the operator is told to rescan.

---

## 7. Legacy integration strategy

Validated campaign science is bundled verbatim under `legacy_integration/` and
loaded — never imported normally — through `importlib.util.spec_from_file_location`:

| Bridge | Legacy module |
|---|---|
| `LegacyNoseboomBridge` | `Noseboom/noseboom_browser_GUI.py` (`one_hz`, `detect_straight`, `compute_wind_spectra`, `export_noseboom_data`) |
| `MiroRackBridge` | `MIRO_Rack/MIRO_Rack_GUI.py` — a whole Flask app, proxied via `app.test_client()` |
| `LegacyOpcBridge` | `Hatchbox/opc_n3_quicklook.py` |
| `LegacyPartectorBridge` | `Hatchbox/partector_quicklook.py` |
| `LegacyInsGimbalBridge` | `Hatchbox/gremsy_full_flight_quicklook.py` |
| `LegacySifBridge` | `Hatchbox/SIF/scripts/airflox_sif_automation.py` |
| FLIR Level 2 (inline) | `FLIR/FLIR_Processing_pipeline.py` |

`core/legacy_paths.py` resolves the root from `$CCFLUX_LEGACY_ROOT`, then the
bundled folder, then two hard-coded developer paths.

The MIRO Rack integration is the most invasive: `page_html()` string-rewrites the
legacy HTML (route prefixes, fonts, footer, injected header bar and ~200 lines of
injected JavaScript) and `forward_get`/`forward_post` tunnel requests into the
legacy Flask app's test client. It also reaches into `backend._lock` and
`backend._flight_project` directly.

---

## 8. Instrument capability matrix

| Instrument | Group | Level 1 | Level 2 | Browser page |
|---|---|---|---|---|
| Noseboom | NOSEBOOM | ✅ streaming window, 1 Hz, straight legs, spectra | via legacy `analyze` | `/noseboom` (Leaflet) |
| MIRO | MIRO RACK | ✅ + 1 Hz Mapview copy | — | `/miro_rack` |
| Picarro | MIRO RACK | ✅ only through the MIRO Rack bridge | — | `/miro_rack` |
| OPC HBX-4 / HBX-5 | HATCHBOX | ✅ chronological repair, quarantine, bin heatmap | — | `/opc` (combined) |
| Partector Pro | HATCHBOX | ✅ | — | `/partector` |
| INS Gimbal | HATCHBOX | ✅ | not supported | `/ins_gimbal` |
| SIF / FLOX | HATCHBOX | ✅ configurable (modes, position mode, corrections) | — | `/sif` |
| MicaSense | HATCHBOX | ✅ metadata only | all routines declared unavailable | — |
| FLIR | HATCHBOX | ✅ acquisition health | ✅ radiometric temperature + Noseboom georeferencing | `/flir` |
| GoPro | HATCHBOX | ✅ inventory + capture map | not supported | `/gopro` (Leaflet) |

`core/camera_level2.py` declares each Level 2 routine's availability with an
explicit reason string — honest capability reporting rather than silent absence.

---

## 9. Cross-cutting concerns

**Concurrency.** One `threading.RLock` (`DashboardScanBackend._lock`) guards all
session state. Additional locks: `_dialog_lock` (non-blocking, prevents stacked
native pickers), `_hatchbox_view_lock`, `ProcessingLogManager._lock`,
`ProcessingPriorityQueue._lock`, `ProcessingScheduler._lock`, and the MIRO bridge's
`_lock` / `_map_build_lock` / `_project_save_lock`.

**Resources.** `ResourceManager` detects cores and physical RAM without psutil
(GlobalMemoryStatusEx / sysconf / `sysctl hw.memsize`), reserves 1 GUI core and
1 GiB, caps at 75 % of RAM, and refuses tasks whose estimated peak exceeds the
budget (`admit_task`). Launchers pin `OMP/OPENBLAS/MKL/NUMEXPR_NUM_THREADS=1`.

**Logging.** `ProcessingLogManager` writes newline-delimited JSON to
`logs/processing.jsonl`, keeps a session list and a separately clearable GUI list,
and mirrors into the project output on each checkpoint.

**Read-only guarantees.** Raw roots are never written. Output Folder must be
independent of both raw roots (checked on selection, on scan start, and in
`FlightProject.validate`). Browser payloads strip absolute paths
(`public_capture`). Asset endpoints re-resolve and containment-check every path.

---

## 10. Known structural weaknesses

1. **`scan_backend.py` is ~6 000 lines** and mixes HTTP-facing API, session state,
   native dialogs, all 11 job closures, and file I/O. Splitting it into
   `app/tasks/` is the largest outstanding piece of work.
2. **Four parallel state stores** for the same facts: `InstrumentRegistry`,
   `InstrumentScanState`, `ProcessingJob`, `FlightProject.detected_instruments`.
   The registry's `enabled`/`priority` fields are effectively write-only.
3. **`instruments/base/interface.py` is aspirational** — adapters implement a
   looser de-facto contract (`load` / `process_quicklook` / `create_plots` /
   `export_results` / `export_browser_data`) than the ABC declares.
4. **MicaSense processing is not implemented.** Its imagery is scanned, indexed
   and time-ranged like the other cameras, but no science runs on it yet.

Resolved since this document was first written: the test suite now exists and
covers the tree (including a parse of every browser script, after a syntax error
once disabled the whole interface); `.gitignore` is in place and no virtualenv
is tracked; and the duplicate launcher family using `.venv` has been deleted,
leaving one launcher per platform.

---

*© 2026 Biplob Dey · Forschungszentrum Jülich GmbH*
