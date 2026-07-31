# MicaSense Level 1 provenance

This adapter preserves the metadata/QC conventions confirmed in:

- `MicaSense_Metadata.py`: capture ID fallback from `IMG_<capture>_<band>.tif`,
  bands 1-6, EXIF `DateTimeOriginal` plus `SubSecTime`, file-modification-time
  fallback, capture completeness, and trigger intervals.
- `MicaSense_Flight_Metadata_Audit.py`: complete capture means exactly one file
  for every expected band, duplicate-band reporting, GPS inspection, exposure
  inspection, and metadata health summaries.
- `camera_micasense_metadata_safe.py`: file-size integrity is treated as a
  first-level acquisition check.

The Level 1 implementation reads metadata and verifies TIFF containers in
bounded batches. Only a configured, limited sample is decoded to create
thumbnails.

It deliberately does not call or reproduce the repository's calibration,
panel, irradiance comparison, radiometric correction, alignment, reflectance,
vegetation-index, or georeferencing routines. Those remain outside Level 1.

The bundled ExifTool package is Windows-specific and cannot be loaded by the
macOS Perl runtime. The adapter therefore uses Pillow's bounded TIFF/EXIF
reader. MicaSense XMP fields unavailable through Pillow are reported as
missing; they are never fabricated.
