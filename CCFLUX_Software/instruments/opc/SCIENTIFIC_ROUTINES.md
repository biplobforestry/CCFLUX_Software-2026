# OPC-N3 Scientific Routine Preservation

Both OPC instruments use the immutable processor at
`/Volumes/Biplob/Zeppelin_System/Integration_code/Hatchbox/opc_n3_quicklook.py`.
They are separate adapter instances and never silently merge their records.

The adapter calls these routines unchanged:

- `required_columns`
- `parse_recorded_time`
- `load_sensor`
- `infer_bin_units` and `integer_residual`
- session segmentation and median interval calculation
- 24-bin concentration conversion and total-number calculations
- particle-rate, sample-volume, PM ordering, temperature, RH, missing-value,
  rejection, and laser diagnostics
- flag-only QC policy, preserving zeros and flagged observations
- `plot_by_session`, `concentration_limits`, `bin_heatmap`,
  `diagnostics_panel`, and `time_axis`

The paired `pair_sensors`, `comparison_summary`, `make_plot`, and CLI `main`
paths are deliberately not called. Individual adapter plots compose the
unchanged plotting helpers for one sensor at a time. No timestamp shift,
interpolation, gap filling, zero removal, or QC-based deletion was added.

The source does not define a distinct detailed-processing algorithm, so the
adapters do not invent one.
