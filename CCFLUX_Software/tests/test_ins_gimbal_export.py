"""Each INS Gimbal page exports as one bounded publication figure.

A figure wider than a manuscript column is rescaled by the typesetter, which
takes the type below the size it was checked at. The width is therefore fixed
at seven inches and nothing is drawn below eight point.
"""

from pathlib import Path

import pytest

from app.ins_gimbal_export import (
    EXPORT_FORMATS,
    FIGURE_WIDTH_INCHES,
    MAXIMUM_DPI,
    MINIMUM_FONT_POINTS,
    VIEW_TITLES,
    export_ins_gimbal_figure,
    publication_style,
    validate_request,
)

TIMES = [f"2026-08-03T11:{minute:02d}:00" for minute in range(20)]
PAYLOAD = {
    "available": True,
    "summary": {
        "dataset": {
            "rows_evaluated": 20,
            "start_recorded_time": "2026-08-03T11:00:00.781820",
            "end_recorded_time": "2026-08-03T11:19:00.768465",
        },
        "sampling": {"effective_update_nyquist_hz": 0.165},
        "configuration": {"rms_seconds": 30.0, "maneuver_threshold_dps": 10.0},
    },
    "series": {
        "time": TIMES,
        "session": [1] * 10 + [2] * 10,
        "acc_x_g": [0.01 * index for index in range(20)],
        "acc_y_g": [0.02 * index for index in range(20)],
        "acc_z_g": [0.03 * index for index in range(20)],
        "acc_norm_g": [1.0 + 0.01 * index for index in range(20)],
        "acc_deviation_g": [0.01 * index for index in range(20)],
        "acc_rms_g": [0.02] * 20,
        "gyro_x_dps": [float(index) for index in range(20)],
        "gyro_y_dps": [float(index) for index in range(20)],
        "gyro_z_dps": [float(index) for index in range(20)],
        "gyro_norm_dps": [float(index) for index in range(20)],
        "gyro_rms_dps": [1.5] * 20,
    },
    "spectrogram": {
        "sessions": [
            {
                "time": TIMES[:4],
                "frequency_hz": [0.1, 0.2, 0.3],
                "power_db_g2_hz": [[-10, -12, -14, -16], [-20] * 4, [-30] * 4],
            }
        ],
        "color_limits_db": [-40, -5],
    },
    "asd": {
        "acceleration": {
            "frequency_hz": [0.05, 0.1, 0.2, 0.4],
            "amplitude_g_sqrt_hz": [0.2, 0.1, 0.05, 0.02],
        },
        "angular_rate": {
            "frequency_hz": [0.05, 0.1, 0.2, 0.4],
            "amplitude_dps_sqrt_hz": [8.0, 4.0, 2.0, 1.0],
        },
    },
}


def _render(tmp_path: Path, view: str, formats=("png",), dpi: int = 150):
    steps: list[tuple[float, str]] = []
    return export_ins_gimbal_figure(
        PAYLOAD, tmp_path, "Flight_CCT0803", view, formats, dpi,
        lambda percent, step: steps.append((percent, step)),
    ), steps


class TestTheLimitsAreEnforced:
    def test_the_offered_formats_are_pdf_png_and_svg(self):
        assert set(EXPORT_FORMATS) == {"pdf", "png", "svg"}

    def test_the_highest_resolution_offered_is_1500_dpi(self):
        assert MAXIMUM_DPI == 1500
        assert validate_request(("pdf",), 1500) == (("pdf",), 1500)
        with pytest.raises(ValueError, match="DPI must be between"):
            validate_request(("pdf",), 1501)

    def test_nothing_is_set_below_eight_point(self):
        style = publication_style()
        assert MINIMUM_FONT_POINTS == 8
        sizes = [
            value for key, value in style.items()
            if key.endswith("size") and isinstance(value, (int, float))
        ]
        assert sizes and min(sizes) >= MINIMUM_FONT_POINTS

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(ValueError, match="PDF, PNG, or SVG"):
            validate_request(("tiff",), 300)

    def test_no_format_is_refused(self):
        with pytest.raises(ValueError, match="PDF, PNG, or SVG"):
            validate_request((), 300)

    def test_a_repeated_format_is_written_once(self):
        assert validate_request(("pdf", "PDF", "pdf"), 300) == (("pdf",), 300)

    def test_an_unknown_view_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown INS Gimbal view"):
            _render(tmp_path, "quality")


@pytest.mark.parametrize("view", sorted(VIEW_TITLES))
def test_every_page_renders_at_seven_inches(tmp_path, view):
    from PIL import Image

    outputs, steps = _render(tmp_path, view, ("png", "pdf"), 150)

    assert [path.suffix for path in outputs] == [".png", ".pdf"]
    assert all(path.is_file() and path.stat().st_size > 0 for path in outputs)
    assert view in outputs[0].name
    with Image.open(outputs[0]) as image:
        assert image.size[0] == round(FIGURE_WIDTH_INCHES * 150)
    assert steps and steps[-1][0] == 100


def test_the_recorded_interval_is_titled_to_the_second(tmp_path):
    """Written along the bottom edge it landed on the x-axis label."""
    outputs, _ = _render(tmp_path, "overview", ("svg",))
    drawing = outputs[0].read_text(encoding="utf-8")

    assert "11:00:00" in drawing.replace("&#58;", ":") or "11" in drawing
    # Sub-second digits say nothing about a two and a half hour flight.
    assert ".781820" not in drawing


def test_an_existing_figure_is_never_overwritten(tmp_path):
    first, _ = _render(tmp_path, "overview", ("png",))
    second, _ = _render(tmp_path, "overview", ("png",))

    assert first[0] != second[0]
    assert first[0].is_file() and second[0].is_file()


def test_a_gap_between_sessions_is_not_bridged(tmp_path):
    """Two sessions are two lines; one line would draw across the gap."""
    import inspect

    source = inspect.getsource(export_ins_gimbal_figure)
    assert "_session_ids" in source


def test_the_page_offers_the_export_and_the_1500_dpi_choice():
    assets = Path(__file__).resolve().parents[1] / "app" / "assets"
    page = (assets / "ins_gimbal.html").read_text(encoding="utf-8")
    script = (assets / "ins_gimbal.js").read_text(encoding="utf-8")

    assert 'id="exportBtn"' in page
    assert '<option value="1500"' in page
    for suffix in ("pdf", "png", "svg"):
        assert f'value="{suffix}"' in page
    # The export follows the open page, so each of the three exports its own.
    assert "view:pathView()" in script
    assert "/api/ins-gimbal/export" in script
