from datetime import datetime, timezone

from core.gopro_georeference import camera_local_to_utc, georeference_captures


def test_camera_clock_uses_berlin_daylight_saving_time():
    assert camera_local_to_utc(datetime(2026, 1, 15, 12)).isoformat() == (
        "2026-01-15T11:00:00+00:00"
    )
    assert camera_local_to_utc(datetime(2026, 7, 26, 12)).isoformat() == (
        "2026-07-26T10:00:00+00:00"
    )


def test_images_match_nearest_noseboom_position():
    records = [
        {
            "kind": "image",
            "source_file": "/camera/GoPro/GOPR0001.jpg",
            "timestamp": datetime(2026, 7, 26, 10, 0, 0, 400000, tzinfo=timezone.utc),
        }
    ]
    points = [
        {
            "time": "2026-07-26T10:00:00Z",
            "lat": 50.912345,
            "lon": 6.412345,
            "altitude_m": 411.2,
        },
        {
            "time": "2026-07-26T10:00:01Z",
            "lat": 50.912500,
            "lon": 6.412500,
            "altitude_m": 412.0,
        },
    ]
    captures = georeference_captures(records, points)
    assert len(captures) == 1
    assert captures[0]["image_id"] == "GOPR0001"
    assert captures[0]["latitude"] == 50.912345
    assert captures[0]["longitude"] == 6.412345
    assert captures[0]["altitude_m"] == 411.2
    assert captures[0]["time_delta_seconds"] == 0.4
    assert captures[0]["capture_time_camera"].startswith("2026-07-26T12:00:00.400000")


def test_capture_outside_tolerance_is_not_mapped():
    records = [{
        "kind": "image",
        "source_file": "/camera/GoPro/GOPR0002.jpg",
        "timestamp": "2026-07-26T10:00:10Z",
    }]
    points = [{
        "time": "2026-07-26T10:00:00Z",
        "lat": 50.9,
        "lon": 6.4,
        "altitude_m": 400,
    }]
    assert georeference_captures(records, points) == []
