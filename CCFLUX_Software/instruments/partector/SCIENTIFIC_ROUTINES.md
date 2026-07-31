# Partector Pro Scientific Routine Preservation

The adapter dynamically loads the immutable processor at
`/Volumes/Biplob/Zeppelin_System/Integration_code/Hatchbox/partector_quicklook.py`.
It does not copy or rewrite its scientific formulas.

The following routines are called unchanged:

- `canonicalize_columns`: processed Influx-field precedence and documented
  aliases.
- `require_columns` and `find_size_columns`: the validated canonical schema and
  requirement for exactly eight diameter-named channels.
- `assign_sessions`: timestamp-gap/instrument-clock-reset segmentation and
  logger-duplicate flagging.
- `QCConfig` and `apply_qc`: independent instrument and housekeeping flags,
  start trimming, and combined validity.
- `logarithmic_bin_edges` and `integrate_size_distribution`: integration of
  dN/dlog10(D), including partial logarithmic overlap for the four size bands.
- `session_table`, `choose_sessions`, `pressure_relative_altitude_m`,
  `percentile_text`, and `create_summary`.
- `plot_quicklook` and `write_methodology`.

Raw timestamps are never rewritten. As in the existing code, a trailing `Z` or
numeric offset is removed without converting the recorded wall clock. No
interpolation, smoothing, source attribution, missing columns, size channels,
or processing formulas were added.

The adapter adds only modular detection, bounded header inspection, selected
time filtering, progress/log forwarding, standardized results, cancellation at
safe routine boundaries, output-root confinement, and overwrite protection.
