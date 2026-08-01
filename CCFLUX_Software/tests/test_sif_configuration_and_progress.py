"""Operator-supplied SIF calibration files, and the processing progress window.

The bundled CAL_FROG and Indices_ICOS files are the validated default, but an
instrument gets recalibrated and index definitions change, so the dialog has to
accept the operator's own files. A wrong pick has to be reported while the
dialog is open, not an hour into a run.

The progress window turns the percentage the adapter already reports into a
stage checklist, so a long SIF run shows which part of the pipeline it is in
rather than one moving bar.
"""

from pathlib import Path

import pytest

from app.scan_backend import (
    DEFAULT_SIF_OPTIONS,
    SIF_PROGRESS_STAGES,
    DashboardScanBackend,
)

BUNDLED = Path(__file__).resolve().parents[1] / "instruments" / "sif" / "essentials"


def test_the_bundled_files_are_the_default():
    for key in ("calibration_full", "calibration_fluo", "indices_file"):
        assert DEFAULT_SIF_OPTIONS[key] is None, f"{key} must default to bundled"


def test_an_operator_file_is_accepted(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    indices = tmp_path / "My_Indices.txt"
    indices.write_text("NDVI;(a-b)/(a+b);800,670;10,10;R\n", encoding="utf-8")

    options = backend.update_sif_options({"indices_file": str(indices)})

    assert options["indices_file"] == str(indices.resolve())
    assert options["calibration_full"] is None, "the others stay on the default"


def test_a_missing_file_is_refused_with_its_path(tmp_path):
    backend = DashboardScanBackend(tmp_path)

    with pytest.raises(ValueError, match="does not exist"):
        backend.update_sif_options({"calibration_full": str(tmp_path / "nope.csv")})


def test_a_directory_is_refused(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    folder = tmp_path / "calibration"
    folder.mkdir()

    with pytest.raises(ValueError):
        backend.update_sif_options({"calibration_fluo": str(folder)})


def test_an_empty_value_reverts_to_the_bundled_file(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    indices = tmp_path / "My_Indices.txt"
    indices.write_text("x", encoding="utf-8")
    backend.update_sif_options({"indices_file": str(indices)})

    options = backend.update_sif_options({"indices_file": ""})

    assert options["indices_file"] is None


def test_an_absent_key_keeps_the_current_choice(tmp_path):
    """Saving the rest of the dialog must not silently clear a chosen file."""
    backend = DashboardScanBackend(tmp_path)
    indices = tmp_path / "My_Indices.txt"
    indices.write_text("x", encoding="utf-8")
    backend.update_sif_options({"indices_file": str(indices)})

    options = backend.update_sif_options({"raw_min_kb": 120})

    assert options["indices_file"] == str(indices.resolve())
    assert options["raw_min_kb"] == 120


def test_an_unknown_selection_kind_is_refused(tmp_path):
    backend = DashboardScanBackend(tmp_path)

    with pytest.raises(ValueError, match="Unsupported SIF file selection"):
        backend.select_sif_essential_file("something_else")


def test_the_bridge_falls_back_to_the_bundled_file():
    from instruments.sif.legacy_bridge import LegacySifBridge

    bridge = LegacySifBridge()
    calibration, indices = bridge.essentials("FULL")

    assert calibration.parent == BUNDLED
    assert indices.parent == BUNDLED
    # No override, and a partial override, both keep the bundled files.
    assert bridge.essentials("FULL", {})[0] == calibration
    assert bridge.essentials("FULL", {"calibration_fluo": None})[0] == calibration


def test_the_bridge_uses_the_operator_file(tmp_path):
    from instruments.sif.legacy_bridge import LegacySifBridge

    bridge = LegacySifBridge()
    mine = tmp_path / "My_Indices.txt"
    mine.write_text("x", encoding="utf-8")

    calibration, indices = bridge.essentials("FULL", {"indices_file": str(mine)})

    assert indices == mine
    assert calibration.parent == BUNDLED, "only the index file was overridden"


def test_the_bridge_names_a_file_that_disappeared(tmp_path):
    """Chosen at configuration time, deleted before the run."""
    from instruments.sif.legacy_bridge import LegacySifBridge

    bridge = LegacySifBridge()
    gone = tmp_path / "deleted.csv"

    with pytest.raises(FileNotFoundError, match="deleted.csv"):
        bridge.essentials("FLUO", {"calibration_fluo": str(gone)})


def test_full_and_fluo_take_their_own_calibration(tmp_path):
    from instruments.sif.legacy_bridge import LegacySifBridge

    bridge = LegacySifBridge()
    full = tmp_path / "CAL_FULL.csv"
    fluo = tmp_path / "CAL_FLUO.csv"
    for path in (full, fluo):
        path.write_text("x", encoding="utf-8")
    overrides = {"calibration_full": str(full), "calibration_fluo": str(fluo)}

    assert bridge.essentials("FULL", overrides)[0] == full
    assert bridge.essentials("FLUO", overrides)[0] == fluo


def test_the_progress_stages_cover_the_pipeline_in_order():
    keys = [stage["key"] for stage in SIF_PROGRESS_STAGES]
    percents = [stage["percent"] for stage in SIF_PROGRESS_STAGES]

    assert keys == ["validate", "position", "full", "fluo", "export", "complete"]
    assert percents == sorted(percents), "stages must be monotonic"
    assert percents[-1] == 100.0


def test_progress_before_processing_is_all_pending(tmp_path):
    backend = DashboardScanBackend(tmp_path)

    progress = backend.sif_progress()

    assert progress["percent"] == 0.0
    assert progress["running"] is False
    assert {stage["status"] for stage in progress["stages"]} == {"pending"}
    assert progress["calibration"] == {
        "calibration_full": None, "calibration_fluo": None, "indices_file": None
    }


def test_progress_marks_one_stage_active(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    state = backend.snapshot()["instruments"]["sif"]
    assert state is not None
    backend._instruments["sif"].processing_progress = 30.0
    backend._instruments["sif"].processing_step = "Combining FULL spectral files"
    from core.models import ProcessingStatus
    backend._instruments["sif"].processing_status = ProcessingStatus.PROCESSING

    progress = backend.sif_progress()
    statuses = [stage["status"] for stage in progress["stages"]]

    assert progress["running"] is True
    assert statuses.count("active") == 1, "exactly one stage may be active"
    # Everything below the reported percentage is finished.
    assert statuses[0] == "done" and statuses[1] == "done"
    assert statuses[2] == "active"


def test_progress_after_completion_is_all_done(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    from core.models import ProcessingStatus
    backend._instruments["sif"].processing_progress = 100.0
    backend._instruments["sif"].processing_status = ProcessingStatus.COMPLETE

    progress = backend.sif_progress()

    assert {stage["status"] for stage in progress["stages"]} == {"done"}


def test_progress_reports_the_files_actually_in_use(tmp_path):
    backend = DashboardScanBackend(tmp_path)
    indices = tmp_path / "My_Indices.txt"
    indices.write_text("x", encoding="utf-8")
    backend.update_sif_options({"indices_file": str(indices)})

    assert backend.sif_progress()["calibration"]["indices_file"] == str(
        indices.resolve()
    )


def test_the_dialog_offers_the_file_pickers_and_the_progress_window():
    script = (Path(__file__).parents[1] / "app" / "assets" / "dashboard.js").read_text(
        encoding="utf-8"
    )

    for kind in ("calibration_full", "calibration_fluo", "indices_file"):
        assert f"sifEssentialRow('{kind}'" in script, f"no picker for {kind}"
    assert "/api/sif/select-file" in script
    assert "Use default" in script, "an operator must be able to revert"
    assert "openSifProgressWindow" in script
    assert "/api/sif/progress" in script
    # The poll must stop when the window closes rather than run forever.
    assert "clearInterval(sifProgressTimer)" in script


def test_the_adapter_passes_the_overrides_to_the_bridge():
    adapter = (Path(__file__).parents[1] / "instruments" / "sif" / "adapter.py").read_text(
        encoding="utf-8"
    )

    assert "essentials_overrides" in adapter
    assert "self.bridge.essentials(mode, essentials_overrides)" in adapter
    # The files actually used are recorded with the result, not just assumed.
    assert "calibration_files" in adapter and "index_file" in adapter
