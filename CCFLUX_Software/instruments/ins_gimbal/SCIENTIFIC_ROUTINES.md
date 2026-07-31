# INS Gimbal / Gremsy scientific preservation

The user-facing instrument is **INS Gimbal** (`ins_gimbal`). Its scientific
source remains the immutable `gremsy_full_flight_quicklook.py`.

The adapter calls the existing timestamp parsing, count-to-g/count-to-deg/s
conversion, session description, rolling RMS, Welch ASD, spectrogram, dominant
frequency, plotting, and methodology routines unchanged. It does not filter or
interpolate recorded signals. Timezone suffixes are removed without clock
conversion, matching the source processor.
