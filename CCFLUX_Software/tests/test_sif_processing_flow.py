"""SIF asks for its settings, reports progress where the run lives, and stops.

Reported from a run: starting SIF opened a second browser window on the SIF
workspace, which sat at "Processing SIF / FLOX — Waiting — 0% complete" for the
whole run and stayed there for good when the run never finished, while the main
window said it was processing too. Two windows claiming the same job, one of
them wrong.

The workspace is a results page. It now opens only when the operator clicks the
SIF card, and it never presents itself as running the job. Processing is driven
from the main window: the settings are asked for first, the stages are shown
there while it runs, and it says Done at the end.
"""

from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
DASHBOARD = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
SIF_PAGE = (ASSETS / "sif.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The workspace no longer opens itself
# --------------------------------------------------------------------------
def test_processing_does_not_open_the_workspace():
    assert "ccflux-sif-workspace" not in DASHBOARD
    assert "'/sif/overview'" not in DASHBOARD, (
        "the workspace must not be opened from the dashboard script"
    )


def test_the_card_still_opens_it_on_click():
    markup = (ASSETS / "dashboard.html").read_text(encoding="utf-8")

    assert 'href="/sif/overview"' in markup
    assert 'target="_blank"' in markup


def test_the_workspace_never_blocks_on_a_progress_overlay():
    """A spinner that owns the screen cannot be right about a job it does not run."""
    start = SIF_PAGE.index("if (!response.ready)")
    body = SIF_PAGE[start : start + 2000]

    assert "keepBusy = true" not in body, "the overlay is held open again"
    assert "busy').classList.remove('show')" in body
    # And it says plainly where the work is happening.
    assert "Processing in the main window" in SIF_PAGE
    assert "Not processed yet" in SIF_PAGE


def test_the_workspace_still_follows_a_run_it_did_not_start():
    """Opened mid-run it should fill in when the products appear, not sit dead."""
    assert "setTimeout(load, 2000)" in SIF_PAGE


# --------------------------------------------------------------------------
# Settings are asked for before the run
# --------------------------------------------------------------------------
def test_the_settings_are_asked_for_when_processing_starts():
    assert "openSifConfiguration({ beforeProcessing: true })" in DASHBOARD
    assert "Save and start processing" in DASHBOARD
    assert "Cancel processing" in DASHBOARD


def test_backing_out_of_the_settings_starts_nothing():
    start = DASHBOARD.index("async function beginRegisteredProcessing")
    body = DASHBOARD[start : DASHBOARD.index("\n  async function refreshProcessingState")]

    assert "if (!proceed)" in body
    assert "return;" in body
    # The refusal must come before the request that starts the run.
    assert body.index("if (!proceed)") < body.index("/api/processing/start")


def test_the_settings_are_only_asked_for_when_sif_is_selected():
    start = DASHBOARD.index("async function beginRegisteredProcessing")
    body = DASHBOARD[start : DASHBOARD.index("\n  async function refreshProcessingState")]

    assert "if (sifSelected) {" in body
    assert "job.job_id === 'sif'" in body


# --------------------------------------------------------------------------
# Progress lives in the window that owns the run
# --------------------------------------------------------------------------
def test_progress_opens_in_the_main_window():
    start = DASHBOARD.index("async function beginRegisteredProcessing")
    body = DASHBOARD[start : DASHBOARD.index("\n  async function refreshProcessingState")]

    assert "openSifProgressWindow()" in body
    # After the run has been accepted, not before.
    assert body.index("/api/processing/start") < body.index("openSifProgressWindow()")


def test_the_progress_window_says_done():
    assert "&#10003; Done" in DASHBOARD
    assert "Open the SIF card for the overview and maps" in DASHBOARD


def test_a_stopped_run_is_reported_as_stopped():
    assert "Stopped:" in DASHBOARD
    assert "Processing Log" in DASHBOARD


def test_polling_stops_once_the_run_ends():
    """A window still asking every second reads as though nothing finished."""
    start = DASHBOARD.index("function openSifProgressWindow")
    body = DASHBOARD[start : start + 1400]

    assert "'complete', 'warning', 'failed', 'cancelled'" in body
    assert "clearInterval(sifProgressTimer); sifProgressTimer = null;" in body


def test_a_failed_poll_does_not_stop_the_window():
    start = DASHBOARD.index("async function refreshSifProgress")
    body = DASHBOARD[start : start + 500]

    assert "return ''" in body, "a failed poll must not look like a finished run"


# --------------------------------------------------------------------------
# The backend behind it
# --------------------------------------------------------------------------
def test_progress_reports_the_stages_and_a_terminal_status(tmp_path):
    from app.scan_backend import DashboardScanBackend
    from core.models import ProcessingStatus

    backend = DashboardScanBackend(tmp_path)
    progress = backend.sif_progress()
    assert [stage["key"] for stage in progress["stages"]] == [
        "validate", "position", "full", "fluo", "export", "complete"
    ]
    assert progress["status"] == "idle"

    backend._instruments["sif"].processing_progress = 100.0
    backend._instruments["sif"].processing_status = ProcessingStatus.COMPLETE
    finished = backend.sif_progress()

    assert finished["status"] == "complete"
    assert {stage["status"] for stage in finished["stages"]} == {"done"}
    assert finished["running"] is False


def test_the_workspace_says_where_to_process_when_it_is_empty(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    view = backend.hatchbox_view("sif")

    assert view["ready"] is False
    assert "Main GUI" in str(view.get("message", ""))
