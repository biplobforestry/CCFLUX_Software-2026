"""The SIF settings dialog gates a processing run, so dismissing it must answer.

`beginRegisteredProcessing` awaits `openSifConfiguration({beforeProcessing:true})`
before it starts anything. That promise used to settle only from the dialog's own
Cancel and Save buttons -- but the window can also be closed with the header X or
by clicking the backdrop, and both of those went straight to
`modal.classList.remove('show')` without resolving anything. The run then waited
forever on an answer that could no longer arrive: the dialog vanished, no
instrument started, no error appeared, and from the operator's side SIF had
simply failed to process.

These are static checks against the script. There is no DOM here, so they assert
the wiring that made the hang possible cannot come back.
"""

from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
SCRIPT = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
PAGE = (ASSETS / "dashboard.html").read_text(encoding="utf-8")


def test_closing_the_window_answers_a_waiting_caller():
    """Both dismissal routes go through closeModal, which settles the promise."""
    assert "document.getElementById('closeModal').addEventListener('click', closeModal);" in SCRIPT
    assert "if (event.target === modal) closeModal();" in SCRIPT

    close = SCRIPT[SCRIPT.index("function closeModal()"):]
    close = close[: close.index("\n  }")]
    assert "settleModalAnswer(false)" in close, (
        "closeModal must answer a pending dialog, or the run waits forever"
    )


def test_the_gating_dialog_registers_before_it_can_be_dismissed():
    """Registration has to happen before any await, or there is a window in
    which the X is live and nothing is listening."""
    body = SCRIPT[SCRIPT.index("function openSifConfiguration("):]
    body = body[: body.index("\n  // ------")]

    assert "if (beforeProcessing) modalPendingAnswer = settle;" in body
    registration = body.index("modalPendingAnswer = settle")
    code = [
        line for line in body[:registration].splitlines()
        if not line.lstrip().startswith("//")
    ]
    assert not any("await " in line for line in code)


def test_a_new_dialog_clears_a_minimized_one():
    """The body is shared, so a minimised window cannot outlive its content."""
    show = SCRIPT[SCRIPT.index("function showModal()"):]
    show = show[: show.index("\n  }")]

    assert "modalMinimized = false" in show
    assert "modalRestore.hidden = true" in show
    # Every dialog opens through showModal; only the restore handler may set the
    # class directly, or minimising would be cleared by the act of restoring.
    assert SCRIPT.count("modal.classList.add('show')") == 1


def test_minimizing_keeps_the_progress_poller_running():
    """A minimised window still owns the run it reports on."""
    assert "if (!modalIsOpen()) {" in SCRIPT

    is_open = SCRIPT[SCRIPT.index("function modalIsOpen()"):]
    is_open = is_open[: is_open.index("\n  }")]
    assert "modalMinimized" in is_open


def test_minimizing_does_not_answer_the_dialog():
    """Setting a window aside is not a decision; only closing it is."""
    minimize = SCRIPT[SCRIPT.index("function minimizeModal()"):]
    minimize = minimize[: minimize.index("\n  }")]

    assert "settleModalAnswer" not in minimize
    assert "modalPendingAnswer" not in minimize


def test_reopening_after_a_completed_run_offers_to_restart():
    assert "'Save and restart processing'" in SCRIPT
    assert "canRestart = !beforeProcessing && Boolean(sifJob && sifJob.previously_completed)" in SCRIPT


def test_configuration_stays_reachable_after_a_completed_run():
    """A completed job used to be given Reprocess *instead of* Configure, so
    once SIF finished there was no route back to its settings and the
    save-and-restart offer could never be seen."""
    actions = SCRIPT[SCRIPT.index("const actions = []"):]
    actions = actions[: actions.index("const selectable")]

    sif_branch = actions.index("job.instrument_id === 'sif'")
    completed_branch = actions.index("job.previously_completed")
    assert sif_branch < completed_branch, (
        "the completed-job branch must not shadow SIF's configuration button"
    )
    assert "configure_sif" in actions


def test_the_restart_requeues_explicitly():
    """The backend refuses to overwrite a completed result without confirmation,
    so saving settings alone would leave them unused."""
    save = SCRIPT[SCRIPT.index("document.getElementById('saveSifOptions').onclick"):]
    save = save[: save.index("\n    };")]

    assert "'reprocess'" in save and "confirmed: true" in save
    assert "'/api/processing/start'" in save
    assert "openSifProgressWindow()" in save


@pytest.mark.parametrize(
    "element", ["minimizeModal", "modalRestore", "modalRestoreLabel"]
)
def test_the_page_provides_what_the_script_reaches_for(element):
    """A missing id throws on load, which would disable the whole interface."""
    assert f'id="{element}"' in PAGE
    assert element in SCRIPT
