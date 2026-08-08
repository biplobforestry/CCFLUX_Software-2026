"""One definition of a CC-FLUX figure, and every producer held to it.

Each script chose its own page: the OPC quicklook at seventeen inches by
sixteen with seven-point labels, the Partector at 13.2 by 9, the Gremsy at
sixteen by twelve, the Mapview export at 17.33 with no text in it at all.
Dropped into a report they are rescaled by whoever is writing it, and a
rescaled figure carries rescaled text — the OPC panel labels arrive at under
three point in a manuscript column.

Two rules now hold for every figure the project makes: seven inches wide, so it
goes into a column at its authored size, and nothing below nine point, which is
the smallest that still reads there.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import figure_standard

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def figure():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    made = plt.figure(figsize=(7.0, 4.0), constrained_layout=True)
    yield made
    plt.close(made)


class TestTheStandardItself:
    def test_the_two_rules_are_stated_once(self):
        assert figure_standard.PAGE_WIDTH_INCHES == 7.0
        assert figure_standard.MINIMUM_FONT_POINTS == 9.0

    def test_a_caption_still_fits_on_the_page(self):
        assert figure_standard.MAXIMUM_HEIGHT_INCHES <= 9.0

    def test_nothing_in_the_settings_starts_below_the_floor(self):
        sizes = [
            value for key, value in figure_standard.rc_parameters().items()
            if key.endswith("size") and isinstance(value, (int, float))
        ]
        assert sizes
        assert min(sizes) >= figure_standard.MINIMUM_FONT_POINTS

    def test_raising_the_floor_raises_the_whole_hierarchy(self):
        """A title has to keep looking like a title."""
        small = figure_standard.rc_parameters(9.0)
        large = figure_standard.rc_parameters(14.0)
        assert large["figure.titlesize"] > large["font.size"] == 14.0
        assert large["axes.titlesize"] - large["font.size"] == pytest.approx(
            small["axes.titlesize"] - small["font.size"]
        )

    def test_the_saved_size_is_the_size_that_was_asked_for(self):
        """bbox="tight" crops to the ink, which turned a portrait map into 6.02
        inches and broke the guarantee the width exists to give."""
        assert figure_standard.rc_parameters()["savefig.bbox"] is None


class TestEnforcement:
    def test_what_the_layout_shrank_is_raised_back(self, figure):
        axis = figure.add_subplot()
        axis.set_xlabel("Time", fontsize=4.0)

        sizes = figure_standard.enforce_minimum_font(figure)

        assert sizes and min(sizes) >= figure_standard.MINIMUM_FONT_POINTS
        assert axis.xaxis.label.get_fontsize() == figure_standard.MINIMUM_FONT_POINTS

    def test_a_figure_at_the_page_width_is_left_alone(self, figure):
        figure_standard.fit_to_page(figure)

        assert tuple(figure.get_size_inches()) == pytest.approx((7.0, 4.0))

    def test_a_wider_figure_is_scaled_not_cropped(self, figure):
        figure.set_size_inches(14.0, 8.0)

        figure_standard.fit_to_page(figure)

        width, height = figure.get_size_inches()
        assert width == pytest.approx(7.0)
        # Proportions kept: scaled, not trimmed to whatever the ink covers.
        assert height == pytest.approx(4.0)

    def test_the_height_stays_on_a_page(self, figure):
        figure.set_size_inches(7.0, 40.0)

        figure_standard.fit_to_page(figure)

        assert figure.get_size_inches()[1] <= figure_standard.MAXIMUM_HEIGHT_INCHES

    def test_a_degenerate_width_is_not_divided_by(self, figure):
        figure.set_size_inches(7.0, 4.0)
        figure.set_figwidth(0.0)

        figure_standard.fit_to_page(figure)  # must not raise

    def test_both_rules_are_applied_before_the_first_write(self, figure, tmp_path):
        """The PDF and the PNG of one figure must be the same figure."""
        axis = figure.add_subplot()
        axis.set_ylabel("Concentration", fontsize=5.0)
        figure.set_size_inches(11.0, 6.0)

        written = figure_standard.save(
            figure, (tmp_path / "a.png", tmp_path / "b.pdf"), dpi=100
        )

        assert [path.name for path in written] == ["a.png", "b.pdf"]
        assert figure.get_size_inches()[0] == pytest.approx(7.0)
        assert axis.yaxis.label.get_fontsize() == figure_standard.MINIMUM_FONT_POINTS

    def test_a_single_path_is_accepted(self, figure, tmp_path):
        written = figure_standard.save(figure, tmp_path / "one.png", dpi=100)
        assert len(written) == 1 and written[0].is_file()

    def test_a_missing_directory_is_created(self, figure, tmp_path):
        target = tmp_path / "deep" / "deeper" / "figure.png"
        figure_standard.save(figure, target, dpi=100)
        assert target.is_file()

    def test_the_written_width_is_seven_inches(self, figure, tmp_path):
        from PIL import Image

        figure.set_size_inches(13.2, 9.0)
        written = figure_standard.save(figure, tmp_path / "measured.png", dpi=100)

        with Image.open(written[0]) as image:
            width, _height = image.size
        assert width / 100 == pytest.approx(7.0, abs=0.02)


class TestEveryProducerUsesIt:
    """The list is the point: one standard, not one per instrument."""

    PRODUCERS = {
        "OPC (paired)": "legacy_integration/Hatchbox/opc_n3_quicklook.py",
        "OPC (single)": "instruments/opc/shared_adapter.py",
        "Partector": "legacy_integration/Hatchbox/partector_quicklook.py",
        "Gremsy": "legacy_integration/Hatchbox/gremsy_full_flight_quicklook.py",
        "FLIR": "legacy_integration/FLIR/FLIR_Quick_look.py",
        "MIRO and Picarro": "legacy_integration/MIRO_Rack/export.py",
        "Noseboom": "app/noseboom_statistics_export.py",
        "INS gimbal": "app/ins_gimbal_export.py",
        "Mapview": "core/scientific_map.py",
    }

    @pytest.mark.parametrize("name", sorted(PRODUCERS))
    def test_it_takes_its_sizes_from_the_standard(self, name):
        source = (ROOT / self.PRODUCERS[name]).read_text(encoding="utf-8")
        assert "figure_standard" in source, f"{name} still sets its own sizes"

    @pytest.mark.parametrize("name", sorted(PRODUCERS))
    def test_nothing_is_drawn_below_the_floor(self, name):
        """A single fontsize= under nine point undoes the whole standard."""
        source = (ROOT / self.PRODUCERS[name]).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = [
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg in {"fontsize", "labelsize"}
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, (int, float))
            and node.value.value < figure_standard.MINIMUM_FONT_POINTS
        ]
        assert not offenders, f"{name} sets {offenders} point"

    @pytest.mark.parametrize("name", sorted(PRODUCERS))
    def test_no_figure_is_authored_wider_than_the_page(self, name):
        """fit_to_page would scale it, but scaling only moves paper: the text
        stays where it was set and the panels crowd. The producers are authored
        at the width so the backstop stays a formality."""
        source = (ROOT / self.PRODUCERS[name]).read_text(encoding="utf-8")
        tree = ast.parse(source)
        widths = [
            node.value.elts[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg == "figsize"
            and isinstance(node.value, ast.Tuple)
            and node.value.elts
            and isinstance(node.value.elts[0], ast.Constant)
            and isinstance(node.value.elts[0].value, (int, float))
        ]
        too_wide = [
            width for width in widths
            if width > figure_standard.PAGE_WIDTH_INCHES
        ]
        assert not too_wide, f"{name} authors figures {too_wide} inches wide"


class TestTheHeightIsNotFixed:
    """A three-panel time series and a single scatter want different shapes."""

    def test_producers_choose_their_own_height(self):
        heights = set()
        for relative in TestEveryProducerUsesIt.PRODUCERS.values():
            source = (ROOT / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.keyword)
                    and node.arg == "figsize"
                    and isinstance(node.value, ast.Tuple)
                    and len(node.value.elts) == 2
                    and isinstance(node.value.elts[1], ast.Constant)
                ):
                    heights.add(node.value.elts[1].value)
        assert len(heights) > 1, "every figure came out the same shape"
