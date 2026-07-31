# FLOX / SIF scientific preservation

The adapter loads the bundled, validated `airflox_sif_automation.py`
implementation and its original FULL/FLUO calibration and vegetation-index
essentials. It
preserves spectral-block parsing, calibration and dark-count correction,
radiance/reflectance, GPS repair and UTC, solar zenith, PAR/APAR, vegetation
indices, optional nonlinearity and FULL spectral shift correction, spline gap
filling, iFLD SIF-A/SIF-B, diagnostics, time filtering, output tables, and GIS.

For UAV/Airship measurements, the existing telemetry routine either reuses a
validated SIF log or prepares one by matching Gremsy Gimbal attitude to
Noseboom latitude, longitude, and altitude. The responsive browser page only
visualizes evaluated outputs; it does not introduce new spectral equations,
interpolate missing science values, or modify raw files.
