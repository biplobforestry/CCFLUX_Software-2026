"""MIRO and Picarro are separate instruments and a flight may carry only one.

The MIRO Rack page assumed both. `app.meta.<instrument>` only exists once that
instrument has been processed, so reading `.start` off it threw for a flight
carrying one; the investigation filter demanded an interval inside *both*
instruments' bounds, which no single-instrument flight can satisfy; and the
project was saved only when both were loaded, so a flight with one never saved
its MIRO Rack products at all.

The map builder was already right - it fails only when neither instrument
produced layers - so the fixes are around it, not in it.
"""

from pathlib import Path

import pytest

BRIDGE = (Path(__file__).resolve().parents[1] / "app" / "miro_rack_bridge.py").read_text(
    encoding="utf-8"
)
MAP_SCRIPT = (
    Path(__file__).resolve().parents[1] / "app" / "assets" / "miro_rack_map.js"
).read_text(encoding="utf-8")


def test_the_payload_says_which_instruments_produced_layers():
    assert '"available": {' in BRIDGE
    assert "bool(any(layers.values()))" in BRIDGE


def test_the_map_is_built_when_only_one_instrument_has_layers():
    """Pinned because it was already correct and must stay so."""
    assert 'if not any(map_layers["MIRO"].values()) and not any(' in BRIDGE
    assert "No 1 Hz MIRO or Picarro concentration values overlap" in BRIDGE


def test_the_project_is_saved_when_either_instrument_is_loaded():
    assert "any_loaded = any(" in BRIDGE
    assert "both_loaded" not in BRIDGE


def test_a_missing_instrument_does_not_break_the_time_filter():
    """app.meta.<instrument> is absent until that instrument is processed."""
    assert "const info = app.meta[name];" in BRIDGE
    assert "if (!info || !info.start || !info.end) return null;" in BRIDGE
    assert "if (!miroRange && !picarroRange)" in BRIDGE
    assert "does not overlap MIRO or Picarro data" in BRIDGE


def test_the_filter_bounds_check_skips_an_absent_instrument():
    body = BRIDGE[BRIDGE.index("function ccfluxFilterInside("):]
    body = body[: body.index("\n}")]

    assert "const range = bounds[name];" in body
    assert "if (!range) return true;" in body
    assert "if (!bounds.miro && !bounds.picarro) return false;" in body


def test_a_null_range_is_not_destructured():
    """`[a, b] = null` throws, which is what happened for the absent one."""
    assert "if (miroRange) [miroStart.value, miroEnd.value] = miroRange;" in BRIDGE
    assert "if (picarroRange) [picarroStart.value, picarroEnd.value] = picarroRange;" in BRIDGE


def test_the_investigation_note_names_a_missing_instrument():
    assert "no data available" in BRIDGE
    assert "const describe = (label, range)" in BRIDGE


@pytest.mark.parametrize(
    "phrase",
    [
        "Process or load MIRO or Picarro data first.",
        "must remain inside the selected main GUI Time Filter for the available instruments.",
    ],
)
def test_the_wording_no_longer_demands_both(phrase):
    assert phrase in BRIDGE


def test_the_map_page_offers_only_the_instruments_that_are_present():
    assert "const availability = payload.available || {};" in MAP_SCRIPT
    assert "option.disabled = true" in MAP_SCRIPT


def test_the_map_page_names_a_missing_instrument():
    assert "no data available" in MAP_SCRIPT
    assert "document.getElementById('message')" in MAP_SCRIPT


def test_the_map_page_falls_back_for_a_project_saved_before_the_flag():
    """An older Mapview product has no `available` field; emptiness of its
    layers is what says the instrument was absent."""
    block = MAP_SCRIPT[MAP_SCRIPT.index("const availability = payload.available"):]
    block = block[: block.index("const absent")]

    assert "payload.layers" in block
    assert ".some(rows => (rows || []).length)" in block


def _rack_module():
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "legacy_integration" / "MIRO_Rack"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import MIRO_Rack_GUI

    return MIRO_Rack_GUI


def _frame(start_hour):
    import pandas as pd

    return pd.DataFrame(
        {
            "timestamp": pd.date_range(f"2026-08-03 {start_hour}:00", periods=60, freq="2s"),
            "CO2 raw": range(60),
        }
    )


def _store(module, *, miro, picarro):
    with module.LOCK:
        module.STORE.update(
            {
                "miro": miro,
                "picarro": picarro,
                "meta": {"paths": {}},
                "results": {"picarro": {"ok": True}},
                "project": None,
            }
        )


def test_a_picarro_only_flight_saves_and_restores_its_session(tmp_path):
    """Flight_CCT0803 carried no MIRO, and its Picarro analysis was lost on
    reopening because the session refused to save without both analyzers."""
    module = _rack_module()
    target = tmp_path / "picarro_only.hdf"
    _store(module, miro=None, picarro=_frame(12))

    module.save_project_worker(str(target), {"results_current": True})
    _store(module, miro="cleared", picarro="cleared")
    module.load_project_worker(str(target))

    with module.LOCK:
        assert module.STORE["miro"] is None
        assert len(module.STORE["picarro"]) == 60
        assert module.STORE["project"]["results_available"] is True


def test_a_miro_only_flight_saves_and_restores_its_session(tmp_path):
    module = _rack_module()
    target = tmp_path / "miro_only.hdf"
    _store(module, miro=_frame(13), picarro=None)

    module.save_project_worker(str(target), {"results_current": True})
    _store(module, miro="cleared", picarro="cleared")
    module.load_project_worker(str(target))

    with module.LOCK:
        assert len(module.STORE["miro"]) == 60
        assert module.STORE["picarro"] is None


def test_both_analyzers_still_round_trip(tmp_path):
    module = _rack_module()
    target = tmp_path / "both.hdf"
    _store(module, miro=_frame(13), picarro=_frame(12))

    module.save_project_worker(str(target), {"results_current": True})
    _store(module, miro="cleared", picarro="cleared")
    module.load_project_worker(str(target))

    with module.LOCK:
        assert len(module.STORE["miro"]) == 60
        assert len(module.STORE["picarro"]) == 60


def test_a_session_with_neither_analyzer_is_refused(tmp_path):
    import pytest

    module = _rack_module()
    _store(module, miro=None, picarro=None)

    with pytest.raises(RuntimeError, match="MIRO or Picarro"):
        module.save_project_worker(str(tmp_path / "empty.hdf"), {})
