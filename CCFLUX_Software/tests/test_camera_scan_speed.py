"""Camera discovery must not open every frame, and must find the same things.

Scanning the campaign's camera folder took 17.9 seconds for 4,188 files, and
11.4 of those were Pillow opening each of the 3,651 GoPro frames to read EXIF —
about 29 small reads per file. Detection uses only the tag *names*, and every
frame a camera writes into one folder carries the same ones, so a sample per
folder answers for the folder. Discovery is now 1.2 seconds and reports exactly
the same instruments, file counts, confidences and matched rules.

The speed itself is not asserted here — that depends on the disk. What is
asserted is the behaviour the speed comes from, and that it does not change the
answer.
"""

import io
from pathlib import Path

import pytest

from core.scanner import (
    EXIF_HEADER_BYTES,
    EXIF_SAMPLES_PER_FOLDER,
    _configured_exif_tags,
    _folder_matches,
)

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _jpeg_with_exif(path: Path, description="CCFLUX"):
    image = Image.new("RGB", (64, 48), (90, 120, 150))
    exif = image.getexif()
    exif[270] = description            # ImageDescription
    exif[271] = "GoPro"                # Make
    exif[306] = "2026:07:27 08:15:30"  # DateTime
    image.save(path, "JPEG", exif=exif)
    return path


def test_the_exif_window_is_large_enough_for_the_camera(tmp_path):
    """GoPro embeds a thumbnail in APP1; 32 KB missed every frame, 64 KB none."""
    assert EXIF_HEADER_BYTES >= 64 * 1024


def test_tags_are_read_without_opening_the_whole_file(tmp_path):
    path = _jpeg_with_exif(tmp_path / "GPAA0001.JPG")

    tags, warning = _configured_exif_tags(path)

    assert warning is None
    assert {"Make", "DateTime", "ImageDescription"} <= tags


def test_a_file_too_short_for_the_window_still_reads(tmp_path):
    """The bounded read must not depend on the file filling the window."""
    path = _jpeg_with_exif(tmp_path / "tiny.JPG")
    assert path.stat().st_size < EXIF_HEADER_BYTES

    tags, warning = _configured_exif_tags(path)

    assert warning is None and "Make" in tags


def test_a_format_the_window_cannot_answer_falls_back(tmp_path, monkeypatch):
    """A TIFF may put its directory past the window; the file is then opened."""
    path = _jpeg_with_exif(tmp_path / "GPAA0002.JPG")
    opened: list[object] = []
    real_open = Image.open

    def recording(source, *args, **kwargs):
        opened.append(source)
        if isinstance(source, io.BytesIO):
            raise ValueError("cannot be read from this window")
        return real_open(source, *args, **kwargs)

    monkeypatch.setattr("PIL.Image.open", recording)
    tags, warning = _configured_exif_tags(path)

    assert warning is None, "the fallback must still produce the tags"
    assert "Make" in tags
    assert any(not isinstance(item, io.BytesIO) for item in opened), (
        "the file itself was never opened"
    )


def test_an_unreadable_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "missing.JPG"

    tags, warning = _configured_exif_tags(path)

    assert tags == set()
    assert warning and "missing.JPG" in warning


def test_the_folder_sample_is_a_sample_not_one_file():
    """One frame is too thin a basis; the whole folder is too expensive."""
    assert 8 <= EXIF_SAMPLES_PER_FOLDER <= 200


def test_the_folder_match_is_cached_per_folder(tmp_path):
    """Thousands of frames share a parent chain; walking it per file was the
    scan's second cost after disk reads."""
    root = tmp_path
    folder = tmp_path / "GoPro" / "DCIM" / "100GOPRO"
    folder.mkdir(parents=True)
    names = ("gopro", "dcim")

    first = _folder_matches(folder, root, names)
    hits_before = _folder_matches.cache_info().hits
    second = _folder_matches(folder, root, names)

    assert first == second
    assert _folder_matches.cache_info().hits == hits_before + 1
    assert [name for _, name in first] == ["dcim", "gopro"]


def test_the_cache_distinguishes_different_pattern_sets(tmp_path):
    root = tmp_path
    folder = tmp_path / "GoPro"
    folder.mkdir()

    assert _folder_matches(folder, root, ("gopro",)) != ()
    assert _folder_matches(folder, root, ("micasense",)) == ()


def test_sampling_does_not_change_what_is_detected(tmp_path):
    """The claim behind the speed-up, on a folder built for the purpose."""
    from core.detection_configuration import load_detection_configuration
    from core.scanner import FlightFolderScanner
    import core.scanner as scanner_module

    root = tmp_path / "Camera_System"
    folder = root / "GoPro" / "DCIM" / "100GOPRO"
    folder.mkdir(parents=True)
    for index in range(EXIF_SAMPLES_PER_FOLDER * 2 + 5):
        _jpeg_with_exif(folder / f"GPAA{index:04d}.JPG")

    configuration = load_detection_configuration(
        Path(__file__).parents[1] / "configs" / "instrument_detection.yaml",
        Path(__file__).parents[1] / "configs" / "file_patterns.yaml",
    )

    def summarise(report):
        return {
            item.instrument_id: (
                item.matching_file_count,
                round(item.confidence_score, 6),
                tuple(sorted(item.matched_rules)),
            )
            for item in report.candidates
        }

    sampled = summarise(FlightFolderScanner(configuration).scan(root))
    original = scanner_module.EXIF_SAMPLES_PER_FOLDER
    scanner_module.EXIF_SAMPLES_PER_FOLDER = 10**9      # inspect every file
    try:
        every_file = summarise(FlightFolderScanner(configuration).scan(root))
    finally:
        scanner_module.EXIF_SAMPLES_PER_FOLDER = original

    assert sampled == every_file
    assert sampled, "the fixture should have been detected as something"


def test_gopro_coverage_reads_frames_concurrently():
    """The reads are independent and the disk is the limit, not the CPU."""
    from core.time_extraction import TimestampExtractor

    assert TimestampExtractor.GOPRO_EXIF_READERS >= 2
    assert TimestampExtractor.GOPRO_PARALLEL_THRESHOLD >= 2
    source = (Path(__file__).parents[1] / "core" / "time_extraction.py").read_text(
        encoding="utf-8"
    )
    assert "ThreadPoolExecutor" in source


def test_a_small_gopro_delivery_is_read_directly(tmp_path):
    """Below the threshold a pool would cost more than it saves."""
    from core.time_extraction import TimestampExtractor

    paths = [_jpeg_with_exif(tmp_path / f"GPAA{i:04d}.JPG") for i in range(3)]
    extractor = TimestampExtractor()

    result = extractor.extract_instrument("gopro", paths)

    assert not hasattr(extractor, "_gopro_exif")
    assert result.utc_start_time is not None


def test_the_concurrent_read_gives_the_same_answer(tmp_path):
    from core.time_extraction import TimestampExtractor

    paths = [
        _jpeg_with_exif(tmp_path / f"GPAA{i:04d}.JPG")
        for i in range(TimestampExtractor.GOPRO_PARALLEL_THRESHOLD + 4)
    ]

    concurrent = TimestampExtractor().extract_instrument("gopro", paths)
    serial = TimestampExtractor()
    serial.GOPRO_PARALLEL_THRESHOLD = 10**9
    direct = serial.extract_instrument("gopro", paths)

    assert concurrent.utc_start_time == direct.utc_start_time
    assert concurrent.utc_end_time == direct.utc_end_time
    assert list(concurrent.timestamp_quality_warnings) == list(
        direct.timestamp_quality_warnings
    )


def test_an_unreadable_frame_is_still_reported_when_read_concurrently(tmp_path):
    from core.time_extraction import TimestampExtractor

    paths = [
        _jpeg_with_exif(tmp_path / f"GPAA{i:04d}.JPG")
        for i in range(TimestampExtractor.GOPRO_PARALLEL_THRESHOLD + 1)
    ]
    broken = tmp_path / "GPAA9999.JPG"
    broken.write_bytes(b"not a jpeg")
    paths.append(broken)

    result = TimestampExtractor().extract_instrument("gopro", paths)

    assert any("GPAA9999" in warning for warning in result.timestamp_quality_warnings)
    assert result.utc_start_time is not None, "one bad frame must not lose the rest"


# --------------------------------------------------------------------------
# Wide delimited deliveries
# --------------------------------------------------------------------------
def test_a_wide_csv_reads_only_up_to_the_timestamp_column(tmp_path):
    """csv.reader parsed all 143 Noseboom columns to reach column 3, which cost
    19 of the Initial Check's seconds on an 888,000-row delivery."""
    from core.time_extraction import TimestampExtractor

    path = tmp_path / "NoseBoom.csv"
    columns = ["a", "b", "c", "Airflow_UTCcorr_Nanoseconds_ns"] + [
        f"col{i}" for i in range(139)
    ]
    base = 1_785_000_000_000_000_000
    rows = [
        ",".join(["1", "2", "3", str(base + i * 1_000_000_000)] + ["0"] * 139)
        for i in range(50)
    ]
    path.write_text(",".join(columns) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    result = TimestampExtractor().extract_instrument("noseboom", [path])

    assert result.utc_start_time is not None
    assert result.utc_end_time > result.utc_start_time


def test_a_quoted_field_falls_back_to_the_real_parser(tmp_path):
    """The split is only equivalent while nothing is quoted."""
    from core.time_extraction import TimestampExtractor

    path = tmp_path / "quoted.csv"
    base = 1_785_000_000_000_000_000
    path.write_text(
        "a,b,c,Airflow_UTCcorr_Nanoseconds_ns,note\n"
        + f'1,2,3,{base},plain\n'
        + f'1,2,3,{base + 1_000_000_000},"has, a comma"\n',
        encoding="utf-8",
    )

    result = TimestampExtractor().extract_instrument("noseboom", [path])

    assert result.utc_start_time is not None
    assert result.utc_end_time > result.utc_start_time


def test_the_fallback_does_not_double_count(tmp_path):
    """The fast pass is discarded before the real one runs."""
    from core.time_extraction import TimestampExtractor

    base = 1_785_000_000_000_000_000
    header = "a,b,c,Airflow_UTCcorr_Nanoseconds_ns,note\n"
    plain = tmp_path / "plain.csv"
    quoted = tmp_path / "quoted.csv"
    body = [f'1,2,3,{base + i * 1_000_000_000},x' for i in range(6)]
    plain.write_text(header + "\n".join(body) + "\n", encoding="utf-8")
    # Same timestamps, but one row carries a quoted field.
    body[-1] = f'1,2,3,{base + 5 * 1_000_000_000},"y, z"'
    quoted.write_text(header + "\n".join(body) + "\n", encoding="utf-8")

    fast = TimestampExtractor().extract_instrument("noseboom", [plain])
    fell_back = TimestampExtractor().extract_instrument("noseboom", [quoted])

    assert fast.utc_start_time == fell_back.utc_start_time
    assert fast.utc_end_time == fell_back.utc_end_time
    assert fast.records_examined == fell_back.records_examined, (
        "the abandoned fast pass was counted twice"
    )
