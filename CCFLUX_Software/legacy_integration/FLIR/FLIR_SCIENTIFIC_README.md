# FLIR A70 scientific health and temperature workflow

This folder processes the Zeppelin `camera.FLIR_Zeppelin.json` export without
loading the complete multi-gigabyte file into memory.

## Files

- `flir_health_temperature.py`: timestamp index, full-frame metadata health,
  time filtering, temperature statistics, CSV output, reports, and plots.
- `flir_radiometry.py`: independently testable FLIR radiometric equations.
- `test_flir_radiometry.py`: synthetic forward/inverse and streaming tests.
- `requirements.txt`: NumPy and Pillow dependencies.
- `environment_measurements_template.csv`: field values that must be recorded
  and synchronized for quantitative correction.

## Fast post-flight protocol

### 1. Acquisition health first

```bash
python3 FLIR/flir_health_temperature.py \
  "/path/to/camera.FLIR_Zeppelin.json" \
  -o "/path/to/flir_health" \
  --health-only \
  --expected-rate-hz 0.5
```

Review:

- timestamp count, duplicates, ordering, intervals, and gaps;
- comparison against the *planned* recording rate;
- missing or changed `R/B/F/J0/J1/X/alpha/beta` calibration values;
- all-zero startup or failed frames;
- invalid `raw_stats` ordering;
- the raw-signal time plot for discontinuities and warm-up drift.

The A70 sensor supports up to 30 Hz, but the sample was recorded at about
0.49 Hz. Data-loss conclusions must use the configured acquisition rate, not
the sensor's maximum rate.

### 2. Rapid apparent-temperature check

Use a small UTC interval first:

```bash
python3 FLIR/flir_health_temperature.py \
  "/path/to/camera.FLIR_Zeppelin.json" \
  -o "/path/to/flir_apparent_check" \
  --index-cache "/path/to/flir_health/timestamp_index.csv" \
  --start-time "2026-07-15T13:30:00Z" \
  --end-time "2026-07-15T13:31:00Z" \
  --mode apparent
```

Apparent mode uses factory calibration with emissivity 1, transmission 1, and
no reflected/path correction. It is suitable for sensor/data sanity checks,
but not publication-grade target surface temperature.

For faster exploratory plots, add `--every-nth-frame 5`. Do not use temporal
subsampling or smoothing for the final scientific product unless it is
documented and justified.

Every run writes `timestamp_index.csv`. Supplying it with `--index-cache` on
later runs skips the multi-gigabyte timestamp scan. The cache is accepted only
when every source path, byte size, and nanosecond modification time still
matches.

### 3. Record quantitative correction inputs

For every scientifically analyzed interval or target, record:

- target emissivity and how it was measured/selected;
- reflected apparent temperature, preferably by FLIR's reflector method;
- camera-to-target distance;
- atmospheric temperature and relative humidity along the optical path;
- external window/optics transmission and temperature, if present;
- UTC synchronization source and uncertainty;
- active camera measurement range;
- reference blackbody/target temperature before and after the flight.

Static command-line inputs are valid only for intervals where those parameters
can reasonably be treated as constant. For an airborne flight with changing
altitude or weather, divide the flight into valid intervals or synchronize
per-frame values from the navigation/weather stream before final processing.

### 4. Environment-corrected temperature

```bash
python3 FLIR/flir_health_temperature.py \
  "/path/to/camera.FLIR_Zeppelin.json" \
  -o "/path/to/flir_corrected" \
  --index-cache "/path/to/flir_health/timestamp_index.csv" \
  --start-time "2026-07-15T13:30:00Z" \
  --end-time "2026-07-15T13:35:00Z" \
  --mode corrected \
  --environment-inputs-provenance measured \
  --emissivity 0.95 \
  --distance-m 20 \
  --atmospheric-temp-c 18.4 \
  --reflected-temp-c 17.9 \
  --relative-humidity-percent 62 \
  --external-optics-transmission 1.0 \
  --valid-temperature-min-c -20 \
  --valid-temperature-max-c 250
```

Replace every example number with a traceable measurement. If a protective
window exists, replace transmission `1.0` and provide
`--external-optics-temp-c`.

Use `--environment-inputs-provenance assumed_for_testing` only for debugging.
Those results are explicitly marked non-quantitative in the CSV.

## Radiometric equation

The code follows Teledyne FLIR's reference `counts2temp` implementation:

```text
data_radiance = (DN - J0) / J1

K2 = reflected_radiance_term
   + atmospheric_radiance_term
   + external_optics_radiance_term

object_radiance =
    data_radiance / (emissivity * atmospheric_tau * optics_tau) - K2

temperature_C =
    B / ln(R / object_radiance + F) - 273.15
```

Atmospheric transmission uses the stored `X`, `alpha1`, `alpha2`, `beta1`,
and `beta2` coefficients with distance, air temperature, and humidity.

Official equation:
https://flir.custhelp.com/app/answers/detail/a_id/3321/~/flir-cameras---temperature-measurement-formula

## Camera specification and validity limits

The project data sheet identifies an FLIR A70 Thermal Core 95 degree,
part 89995-0101:

- 640 x 480 infrared pixels;
- 7.5-14 micrometre uncooled microbolometer;
- 35 mK NETD;
- maximum image frequency 30 Hz;
- selectable measurement ranges -20 to 175 C, -20 to 250 C, or 175 to
  1000 C;
- stated accuracy under 15-35 C ambient: +/-2 C up to 100 C, then +/-2%,
  depending on the active range.

The JSON does not embed the model or active measurement range. Therefore the
software checks 640 x 480 dimensions but requires the operator to provide the
valid active range. Accuracy must be verified with a traceable reference
target; scene consistency alone cannot prove calibration accuracy.

## Outputs

- `frame_health.csv`: one lightweight health record for every frame.
- `temperature_frames.csv`: temperature and raw-DN statistics for processed
  frames, calibration constants, correction values, equation, and status.
- `acquisition_gaps.csv`: gaps exceeding the configured/derived threshold.
- `summary.json`: machine-readable acquisition and runtime summary.
- `timestamp_index.csv`: verified reusable byte-offset index for fast later
  time-window runs.
- `SCIENTIFIC_QC_REPORT.md`: concise interpretation and limitations.
- `acquisition_interval_over_time.png`
- `raw_signal_health_over_time.png`
- `fast_apparent_temperature_proxy_over_time.png`: full-flight
  `temperature(mean DN)` health proxy without loading pixel arrays; this is not
  the exact mean of per-pixel temperature and is not environment-corrected.
- `temperature_over_time.png` when temperatures are processed.

Add `--save-temperature-npz` only when full per-pixel temperature maps are
needed. CSV stores frame statistics; NPZ preserves the 2-D scientific array.
