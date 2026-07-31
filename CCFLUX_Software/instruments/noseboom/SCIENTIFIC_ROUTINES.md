# Noseboom scientific routine preservation

The adapter delegates to the unchanged source:

`/Volumes/Biplob/Zeppelin_System/Integration_code/Noseboom/noseboom_browser_GUI.py`

The following validated legacy functions are called directly and were not
rewritten:

- `load_csv_files` and `simplify`;
- `one_hz` and `circular_mean_deg`;
- `detect_straight`, `evaluate_straight_window`, and `haversine_m`;
- `trim_frequency`;
- `welch_psd_from_time` and `compute_wind_spectra`;
- `make_export_source`, `resample_export_data`, and
  `export_noseboom_data`;
- the complete legacy `analyze` path for explicitly requested detailed
  processing.

`Airflow_UTCcorr_Nanoseconds_ns` remains authoritative. The adapter applies the
selected UTC interval as a row selection before invoking scientific routines;
it never changes timestamp values.

Legacy terrain sampling and HDF project creation remain part of the explicit
detailed path. They were not reproduced in the quick-look adapter because they
require network terrain tiles and PyTables. Legacy browser-rendered plots are
not represented as saved figures because no validated file-plot routine exists.
