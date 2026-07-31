"""Regressions for five defects found while running the real campaign data."""

import json
import threading
from pathlib import Path

import pytest

from core.headless_plotting import HEADLESS_BACKEND, use_headless_backend
from core.logging_manager import LogLevel, ProcessingLogManager

matplotlib = pytest.importorskip("matplotlib")


# --------------------------------------------------------------------------
# 1. Figures are rendered from processing worker threads. macOS defaults to the
#    macosx GUI backend, which aborts off the main thread. Two bundled legacy
#    modules pin no backend, and FLIR pins one only inside a function, so this
#    cannot depend on which adapter happens to be imported first.
# --------------------------------------------------------------------------
def test_headless_backend_is_selected_and_idempotent():
    matplotlib.use("Agg", force=True)
    assert use_headless_backend().casefold() == HEADLESS_BACKEND.casefold()
    # Calling again must not disturb an already-correct backend.
    assert use_headless_backend().casefold() == HEADLESS_BACKEND.casefold()


def test_figures_render_on_a_worker_thread():
    use_headless_backend()
    import matplotlib.pyplot as plt

    outcome: dict[str, object] = {}

    def render() -> None:
        try:
            figure, _ = plt.subplots(1, 1)
            figure.savefig("/dev/null", format="png")
            plt.close(figure)
            outcome["ok"] = True
        except Exception as exc:  # pragma: no cover - only on regression
            outcome["ok"] = False
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=render)
    worker.start()
    worker.join()
    assert outcome.get("ok"), outcome.get("error")


@pytest.mark.parametrize(
    "bridge_module",
    [
        "instruments.opc.legacy_bridge",
        "instruments.ins_gimbal.legacy_bridge",
        "instruments.partector.legacy_bridge",
        "instruments.miro.legacy_bridge",
        "instruments.flir.legacy_bridge",
        "instruments.noseboom.legacy_bridge",
        "instruments.sif.legacy_bridge",
    ],
)
def test_every_legacy_bridge_pins_the_backend_before_loading(bridge_module):
    """A bridge must pin the backend before its module imports pyplot."""
    import importlib

    source = Path(
        importlib.import_module(bridge_module).__file__
    ).read_text(encoding="utf-8")
    assert "use_headless_backend" in source
    executing = source.index("spec.loader.exec_module")
    assert "use_headless_backend()" in source[:executing], (
        f"{bridge_module} loads its legacy module before pinning the backend"
    )


# --------------------------------------------------------------------------
# 2. Picarro has no standalone adapter; its science lives in the preserved MIRO
#    Rack application. Only the HTTP server attached that bridge, so Picarro
#    failed in every headless run with "the shared MIRO Rack processing bridge
#    is unavailable".
# --------------------------------------------------------------------------
def test_picarro_bridge_is_built_without_the_server(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(Path.cwd())
    assert backend._miro_rack_bridge is None

    bridge = backend._require_miro_rack_bridge()

    assert hasattr(bridge, "process_picarro_from_main")
    # It is cached, not rebuilt per job.
    assert backend._require_miro_rack_bridge() is bridge


# --------------------------------------------------------------------------
# 3. Remote Sensing enabled all three camera products unconditionally, so an
#    operator who wanted everything except MicaSense had no way to say so.
# --------------------------------------------------------------------------
def _remote_sensing_source() -> str:
    import inspect

    from app.scan_backend import DashboardScanBackend

    return inspect.getsource(DashboardScanBackend.start_remote_sensing)


def test_remote_sensing_accepts_an_instrument_selection():
    source = _remote_sensing_source()
    assert 'request.get("instruments")' in source
    assert "Unknown remote-sensing instrument" in source
    assert "Select at least one camera instrument" in source
    # Omitting the field must keep the previous behaviour.
    assert "if requested is not None" in source


def test_remote_sensing_rejects_a_bad_selection(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    for payload, expected in (
        ({"instruments": "gopro"}, "list of instrument IDs"),
        ({"instruments": []}, "at least one camera instrument"),
        ({"instruments": ["noseboom"]}, "Unknown remote-sensing instrument"),
    ):
        with pytest.raises(ValueError, match=expected):
            backend.start_remote_sensing(payload)


# --------------------------------------------------------------------------
# 4. The project log copied the whole persistent file, which is appended to
#    across every run, so a project carried diagnostics from unrelated sessions.
# --------------------------------------------------------------------------
def test_project_log_holds_only_the_current_session(tmp_path):
    persistent = tmp_path / "logs" / "processing.jsonl"

    earlier = ProcessingLogManager(persistent)
    for index in range(5):
        earlier.log(LogLevel.INFO, "earlier-session", f"old record {index}")

    current = ProcessingLogManager(persistent)
    current.log(LogLevel.INFO, "this-session", "new record")
    current.log(LogLevel.ERROR, "this-session", "something failed")

    exported = current.export_logs(tmp_path / "project.jsonl", overwrite=True)
    records = [
        json.loads(line)
        for line in exported.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [record["component"] for record in records] == [
        "this-session",
        "this-session",
    ]
    # The full history is still on disk for crash investigation.
    persisted = [
        line for line in persistent.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(persisted) == 7


def test_exported_project_log_keeps_severity_and_traceback(tmp_path):
    manager = ProcessingLogManager(tmp_path / "logs" / "processing.jsonl")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        manager.capture_exception("worker", "Job failed", exc, instrument="sif")

    exported = manager.export_logs(tmp_path / "out.jsonl", overwrite=True)
    record = json.loads(exported.read_text(encoding="utf-8").splitlines()[0])

    assert record["severity"] == "ERROR"
    assert record["instrument"] == "sif"
    assert "ValueError: boom" in record["traceback"]
