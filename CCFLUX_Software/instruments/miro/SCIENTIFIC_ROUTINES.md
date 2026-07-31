# MIRO Scientific Routine Preservation

The modular MIRO integration is an adapter. It dynamically loads the immutable
source files in `/Volumes/Biplob/Zeppelin_System/Integration_code/MIRO_Rack/`
and does not copy or rewrite their scientific functions.

## Unchanged routines

- `miro.discover_files` and `miro.load_folder`: recursive `.txt` discovery,
  semicolon/decimal-comma parsing, SHA-256 duplicate-file suppression, strict
  `%d.%m.%Y %H:%M:%S,%f` timestamps, chronological sort, and duplicate-time
  removal.
- `gas_unit_scale`: H2O `%`, CO2/CH4 `ppm`, and other gases `ppb`.
- `_stable_ambient_frame`: valve-state handling, time-gap fallback, and the
  post-transition exclusion interval.
- The explicit rule that valve=0 values are MIRO's already
  background-corrected ambient output and receive no second zero subtraction.
- `_fft_lowpass`, `_block_average`, `_allan`, `_segmented_allan`,
  `_minimum_allan`, `_welch_psd`, and `_wall_clock_payload`.
- `analyze`: filtering, stable-ambient selection, block-local detrending,
  residual calculation, ambient/segmented-residual/zero-air Allan products,
  Welch PSD, warnings, and statistics.
- `export.miro_figure` and the MIRO scope of `export.export_figures`.

## Adapter boundaries

The adapter adds schema checks, progress/log forwarding, cooperative
cancellation between safe legacy calls, standardized result construction, and
output-root enforcement. MIRO timestamps remain timezone-naive; the adapter
does not invent UTC values or modify raw timestamps.

Picarro is not integrated by this change. The legacy export module imports its
existing Picarro helper as a dependency, but the adapter never calls Picarro
loading, analysis, comparison, or export paths.
