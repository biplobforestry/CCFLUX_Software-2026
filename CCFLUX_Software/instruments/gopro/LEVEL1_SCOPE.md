# GoPro Level 1 scope

The repository contains no pre-existing GoPro processor or representative
flight dataset. This adapter therefore implements only generic, auditable media
inventory behavior supported by the official Open GoPro API's media-list,
media-metadata, thumbnail, and screennail concepts.

Level 1 reads:

- still-image EXIF acquisition time;
- MP4/MOV movie-header or FFprobe creation time and duration;
- file sizes and basic readability;
- a configured representative subset of stills and video frames.

It never connects to or controls a camera. It does not extract every video
frame, perform detailed visual review, read or generate geotags, merge flight
telemetry, or create final flight-video products.

Video sampling uses FFmpeg when available. If it is absent, all file counts,
timestamps, durations, sizes, and gap checks still run and the missing sampled
thumbnail is reported as a warning.
