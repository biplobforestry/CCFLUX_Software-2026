# FLIR Level 1 provenance

The Level 1 adapter delegates to the repository's unchanged
`FLIR_Quick_look.py` routines for:

- byte-range timestamp matching (`scan_one_byte_range`);
- timestamp parsing;
- acquisition interval, rate, and gap statistics;
- representative frame selection;
- bounded top-level frame-object reading;
- removal of the large `raw` value before metadata JSON parsing;
- raw-array shape inspection.

The adapter additionally creates normalized grayscale thumbnails from only the
configured representative samples. These are display quicklooks of raw signal,
not temperature products.

The following repository routines are deliberately excluded:

- FLIR Planck conversion;
- apparent or corrected temperature calculation;
- emissivity, atmospheric, reflected-temperature, distance, or external-optics
  correction;
- noseboom/INS fusion;
- geolocation and map production.

The local Spinnaker wheel and examples are acquisition references for Windows
camera control. Level 1 reviews post-flight JSON exports and does not connect to
camera hardware, mutate GenICam nodes, or import PySpin/Harvesters.
