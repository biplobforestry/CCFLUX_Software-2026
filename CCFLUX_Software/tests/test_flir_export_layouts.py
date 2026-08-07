"""A FLIR export is read whichever way its frame documents are separated.

Exports arrive in two container layouts, decided by how the recording was taken
off the camera rather than by the campaign:

    JSON array          [{...},{...}]      separated by "},{"
    newline-delimited   {...}\\n{...}      separated by "}\\n{"   (mongoexport)

Only the first was recognised. Flight_CC0806's 32 GB FLIR_backup.json is the
second, so no document boundary was found anywhere in it: frames one and two
survived on the head-of-file fallback and frame three raised
"could not locate object start before byte 1231567", which killed the
temperature pass and left the workspace with an empty map.

The fixtures here are faithful to that delivery - the real 640x480 geometry, the
real ten calibration fields, plain JSON numbers - so these tests stand or fall
with the format the campaign actually produces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")

from instruments.flir.level2_bridge import LegacyFlirLevel2Bridge

health = LegacyFlirLevel2Bridge().health

# Verbatim from Flight_CC0806's FLIR_backup.json: all ten fields the radiometry
# requires, flat, and written as plain JSON numbers rather than extended-JSON
# type wrappers.
REAL_CALIBRATION = {
    "R": 23844.2734375,
    "B": 1537.800048828125,
    "F": 1.0499999523162842,
    "J0": 19528,
    "J1": 35.69045639038086,
    "X": 0.7319999933242798,
    "alpha1": 1.2390000136974777e-08,
    "alpha2": 1.1094999585736787e-08,
    "beta1": 0.0031799999997019768,
    "beta2": 0.0031802000012248755,
}
# The camera is a fixed 640x480 core and the conversion refuses any other
# geometry, so the fixture cannot be miniaturised.
ROWS, COLUMNS = health.EXPECTED_HEIGHT, health.EXPECTED_WIDTH
# A count in the range the flight actually recorded.
REAL_COUNTS = 24200


def _document(index: int, *, counts: int | None = REAL_COUNTS) -> str:
    """One frame document with the real key order, shape and value types.

    ``counts=None`` writes the all-zero warm-up frame the export opens with.
    """
    stamp = f"2026-08-06T05:32:{8 + index * 2:02d}.638Z"
    value = 0 if counts is None else counts + index
    grid = [[value] * COLUMNS for _ in range(ROWS)]
    total = ROWS * COLUMNS
    return json.dumps(
        {
            "_id": {"$oid": f"6a741c587e847e8a2a6290{index:02x}"},
            "timestamp": {"$date": stamp},
            "calibration": REAL_CALIBRATION,
            "raw_stats": {"min": value, "max": value, "mean": float(value)},
            "raw": grid,
        },
        separators=(",", ":"),
    ), total


def _write(path: Path, layout: str, count: int, *, first_is_warm_up: bool = False) -> Path:
    documents = [
        _document(index, counts=None if (first_is_warm_up and index == 0) else REAL_COUNTS)[0]
        for index in range(count)
    ]
    if layout == "array":
        path.write_text("[" + ",".join(documents) + "]", encoding="utf-8")
    else:
        path.write_text("\n".join(documents) + "\n", encoding="utf-8")
    return path


def _entries(path: Path):
    _, found = health.scan_one_range((path, 0, path.stat().st_size, 4096))
    return found


@pytest.mark.parametrize("layout", ["array", "newline"])
def test_every_frame_document_is_located(tmp_path: Path, layout: str) -> None:
    path = _write(tmp_path / "export.json", layout, count=4)
    entries = _entries(path)
    assert len(entries) == 4

    spans = health.object_spans(entries)

    assert len(spans) == 4
    for start, end in spans:
        assert end > start
    # Spans must tile the file in order and never overlap.
    for (_, first_end), (second_start, _) in zip(spans, spans[1:]):
        assert first_end == second_start
    # Each document's own timestamp has to fall inside its own span.
    for (start, end), entry in zip(spans, entries):
        assert start <= entry[3] < end


@pytest.mark.parametrize("layout", ["array", "newline"])
def test_the_whole_temperature_path_runs_on_this_format(
    tmp_path: Path, layout: str
) -> None:
    """Boundaries, headers, the raw array and the conversion, end to end."""
    path = _write(tmp_path / "export.json", layout, count=3)
    entries = _entries(path)
    spans = health.object_spans(entries)

    rows, _ = health.inspect_all_headers(entries, 2)
    assert [row["header_status"] for row in rows] == ["PASS"] * 3
    assert all(row["calibration_complete"] for row in rows)
    assert not any(row["missing_calibration_fields"] for row in rows)
    assert all(row["raw_stats_present"] and row["raw_stats_consistent"] for row in rows)

    for index, (start, end) in enumerate(spans):
        values, dimensions = health.raw_array_from_object(path, start, end)
        assert dimensions == (ROWS, COLUMNS)
        assert values.size == ROWS * COLUMNS

        result = health.process_one_temperature(
            (index + 1, rows[index], entries[index], (start, end)),
            None, None, None, None,
        )
        assert result["temperature_error"] == ""
        assert result["dimension_status"] == "PASS"
        assert result["temperature_status"] == "PASS"
        assert result["temperature_c_valid_pixel_count"] == ROWS * COLUMNS
        # A real temperature, not a placeholder.
        assert -50.0 < float(result["temperature_c_mean"]) < 200.0


@pytest.mark.parametrize("layout", ["array", "newline"])
def test_the_final_document_in_the_file_is_complete(
    tmp_path: Path, layout: str
) -> None:
    """The last frame has no following separator to bound it."""
    path = _write(tmp_path / "export.json", layout, count=3)
    entries = _entries(path)
    spans = health.object_spans(entries)

    assert spans[-1][1] == path.stat().st_size

    values, dimensions = health.raw_array_from_object(path, *spans[-1])

    assert dimensions == (ROWS, COLUMNS)
    assert values.size == ROWS * COLUMNS


def test_newline_delimited_was_the_failure(tmp_path: Path) -> None:
    """The exact shape that raised: no "},{" anywhere in the file."""
    path = _write(tmp_path / "backup.json", "newline", count=4)

    assert b"},{" not in path.read_bytes()

    spans = health.object_spans(_entries(path))

    assert len(spans) == 4
    assert spans[0][0] == 0


def test_a_real_sized_frame_exceeds_the_old_backtrack_ceiling(
    tmp_path: Path,
) -> None:
    """The ladder stopped at 1 MiB; a 640x480 frame of five-digit counts is more.

    So the ceiling was not merely too low for this delivery - it was below the
    size of an ordinary frame, and no fixed ceiling could have been right.
    """
    path = _write(tmp_path / "big.json", "newline", count=3)
    entries = _entries(path)

    assert entries[1][3] - entries[0][3] > 1024 * 1024

    spans = health.object_spans(entries)

    assert len(spans) == 3
    for (start, end), entry in zip(spans, entries):
        assert start <= entry[3] < end


def test_frame_size_varies_with_content_within_one_file(tmp_path: Path) -> None:
    """Zeros are one byte a pixel, five-digit counts are six; both must parse."""
    path = _write(tmp_path / "mixed.json", "newline", count=3, first_is_warm_up=True)
    entries = _entries(path)
    spans = health.object_spans(entries)

    warm_up = spans[0][1] - spans[0][0]
    settled = spans[1][1] - spans[1][0]
    assert settled > warm_up * 2, "the fixture must reproduce the real size spread"

    for start, end in spans:
        _, dimensions = health.raw_array_from_object(path, start, end)
        assert dimensions == (ROWS, COLUMNS)


def test_a_warm_up_frame_of_zeros_is_flagged_not_converted(tmp_path: Path) -> None:
    """The real export opens with all-zero frames before the sensor settles."""
    path = _write(tmp_path / "export.json", "newline", count=3, first_is_warm_up=True)
    entries = _entries(path)
    rows, _ = health.inspect_all_headers(entries, 2)

    assert rows[0]["raw_all_zero"] is True
    assert rows[0]["header_status"] == "FAIL"
    assert "all_zero_frame" in rows[0]["health_flags"]
    assert [row["raw_all_zero"] for row in rows[1:]] == [False, False]

    # ...and excluded from the selection rather than converted to nonsense.
    chosen = health.select_indices(
        entries, rows, entries[0][0], entries[-1][0], 1, False
    )

    assert 0 not in chosen
    assert chosen == [1, 2]


def test_an_unrecognised_layout_says_so(tmp_path: Path) -> None:
    """A file with no separator at all must explain itself, not guess."""
    path = tmp_path / "odd.json"
    padding = "x" * 300_000
    path.write_text(
        '{"a":"' + padding + '"} garbage '
        '"timestamp":{"$date":"2026-08-06T05:32:08.638Z"}',
        encoding="utf-8",
    )
    entries = _entries(path)
    assert entries, "the probe needs a timestamp to look for"

    with pytest.raises(ValueError) as error:
        health.find_object_start(path, entries[0][3], entries[0][3] - 200_000)

    message = str(error.value)
    assert "no document separator was found" in message
    assert "odd.json" in message


def test_the_documented_format_matches_what_the_reader_accepts() -> None:
    """The module states its input contract; it must be the real one."""
    source = Path(health.__file__).read_text(encoding="utf-8")

    assert "newline-delimited" in source
    assert "mongoexport" in source
    # Relaxed extended JSON is required; canonical would wrap every number.
    assert "$numberDouble" in source, "the unsupported mode must be named"
    assert "Nothing here may assume a frame length" in source


class TestTheInterfaceHasNoSecondStage:
    """Selecting FLIR converts temperature. Nothing else is to be started."""

    backend_source = (
        Path(__file__).parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")
    flir_script = (
        Path(__file__).parents[1] / "app" / "assets" / "flir.js"
    ).read_text(encoding="utf-8")

    def test_no_operator_facing_level_2_wording_remains(self):
        for phrase in (
            "Run confirmed FLIR Level 2",
            "Configure FLIR Level 2",
            "Configure the available FLIR Level 2 routines",
        ):
            assert phrase not in self.backend_source, phrase
        assert "Level 2" not in self.flir_script

    def test_the_dead_flir_detailed_queue_branch_is_gone(self):
        assert 'job.job_id == "flir_detailed"' not in self.backend_source

    def test_a_failed_conversion_writes_its_reason_to_the_workspace(self):
        assert "_publish_flir_temperature_failure" in self.backend_source


def test_a_failed_conversion_replaces_the_placeholder(tmp_path: Path) -> None:
    """An empty map must say why, not repeat the pre-conversion placeholder."""
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    quicklook = tmp_path / "flir_browser.json"
    backend._instruments["flir"].quicklook = {
        "available": True,
        "temperature_available": False,
        "temperature_reason": (
            "Temperature conversion and Noseboom georeferencing are running "
            "in this same job."
        ),
        "map_points": [{"frame_id": "1"}],
    }

    backend._publish_flir_temperature_failure(
        quicklook, "Temperature conversion and georeferencing did not complete: boom"
    )

    payload = backend._instruments["flir"].quicklook
    assert payload["temperature_available"] is False
    assert "did not complete" in payload["temperature_reason"]
    assert "running in this same job" not in payload["temperature_reason"]
    assert payload["map_points"] == []
    # The acquisition metadata the run did produce is untouched.
    assert payload["available"] is True
    assert json.loads(quicklook.read_text(encoding="utf-8"))["temperature_reason"] == (
        payload["temperature_reason"]
    )
