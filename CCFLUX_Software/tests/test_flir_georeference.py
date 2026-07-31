from core.flir_georeference import georeference_temperature_records


def test_flir_temperature_records_match_nearest_noseboom_position():
    records = [
        {
            "timestamp.$date": "2026-07-27 05:24:17.500",
            "record_index_in_selected_scan": "7",
            "calculated_temperature.status": (
                "ok_apparent_no_atmospheric_correction"
            ),
            "pixel_temperature.valid_pixel_count": "307200",
            "pixel_temperature.min_c": "12.5",
            "pixel_temperature.max_c": "31.25",
            "pixel_temperature.mean_c": "21.4",
            "pixel_temperature.median_c": "21.1",
            "pixel_temperature.std_c": "2.3",
        }
    ]
    navigation = [
        {
            "time": "2026-07-27T05:24:17Z",
            "lat": 47.65,
            "lon": 9.38,
            "altitude_m": 525.5,
        }
    ]

    matched = georeference_temperature_records(records, navigation)

    assert len(matched) == 1
    assert matched[0]["frame_id"] == "7"
    assert matched[0]["latitude"] == 47.65
    assert matched[0]["longitude"] == 9.38
    assert matched[0]["altitude_m"] == 525.5
    assert matched[0]["temperature_median_c"] == 21.1
    assert matched[0]["time_delta_seconds"] == 0.5


def test_flir_temperature_records_outside_tolerance_are_not_mapped():
    records = [
        {
            "timestamp.$date": "2026-07-27T05:24:30Z",
            "pixel_temperature.median_c": "20",
        }
    ]
    navigation = [
        {
            "time": "2026-07-27T05:24:17Z",
            "lat": 47.65,
            "lon": 9.38,
        }
    ]

    assert not georeference_temperature_records(records, navigation)
