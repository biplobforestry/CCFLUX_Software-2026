from pathlib import Path

from app.noseboom_statistics_export import export_noseboom_statistics


def _scientific_payload() -> dict[str, object]:
    values = [float(index % 31) / 3 for index in range(240)]
    frequency = [0.01 + index * 0.01 for index in range(160)]
    return {
        "time_bounds": {
            "start": "2026-07-27T05:22:04+00:00",
            "end": "2026-07-27T10:19:58+00:00",
        },
        "hist": {
            "wind_mps": values,
            "wind_u_mps": [value - 4 for value in values],
            "wind_v_mps": [value - 3 for value in values],
            "wind_w_mps": [value / 10 - 0.5 for value in values],
            "air_temp_degC": [15 + value / 20 for value in values],
            "rel_humidity_pct": [60 + value for value in values],
        },
        "frequency": [
            {"frequency_hz": 100.0 + (index % 3) / 100}
            for index in range(120)
        ],
        "altitude_profile": [
            {
                "gnss_msl_m": 400 + index,
                "ins_ellipsoid_m": 445 + index,
                "dtm_m": 320 + index / 4,
            }
            for index in range(120)
        ],
        "spectra": {
            "wind_mps": {
                "frequency_hz": frequency,
                "psd": [value ** (-5 / 3) for value in frequency],
            },
            "wind_w_mps": {
                "frequency_hz": frequency,
                "psd": [0.2 * value ** (-5 / 3) for value in frequency],
            },
        },
    }


def test_publication_export_writes_both_layouts_in_all_formats(
    tmp_path: Path,
):
    progress: list[tuple[float, str]] = []

    outputs = export_noseboom_statistics(
        _scientific_payload(),
        tmp_path,
        "Flight_2707",
        ("pdf", "svg", "png"),
        150,
        lambda percent, step: progress.append((percent, step)),
    )

    assert len(outputs) == 6
    assert {path.suffix for path in outputs} == {".pdf", ".svg", ".png"}
    assert sum("histogram_summary" in path.name for path in outputs) == 3
    assert (
        sum("frequency_altitude_spectra" in path.name for path in outputs)
        == 3
    )
    assert all(path.is_file() and path.stat().st_size > 1000 for path in outputs)
    assert progress[-1] == (100, "Publication figures are ready")
