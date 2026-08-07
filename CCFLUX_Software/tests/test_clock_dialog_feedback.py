"""Answering a clock question shows that it is working, then closes itself.

Declaring the SIF record clock is not a quick save: the answer is applied by
re-reading every SIF spectral file and rewriting the Flight Project, and the
GoPro answer rereads the EXIF header of every frame on the card. Both ran with
the choice dialog still on screen showing its own buttons, so the window looked
stuck and there was no way to tell a slow answer from a dead one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
SCRIPT = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
MARKUP = (ASSETS / "dashboard.html").read_text(encoding="utf-8")


def _handler(name: str) -> str:
    """The body of one 'use this setting' click handler."""
    start = SCRIPT.index(f"document.getElementById('{name}').onclick")
    end = SCRIPT.index("\n    };", start)
    return SCRIPT[start:end]


class TestTheWorkingPanel:
    def test_it_exists(self):
        assert "function showWorkingDialog(title, message)" in SCRIPT

    def test_it_shows_movement_rather_than_a_frozen_form(self):
        body = SCRIPT[SCRIPT.index("function showWorkingDialog("):]
        body = body[: body.index("\n  //")]
        assert "scan-progress indeterminate" in body
        assert "showModal()" in body

    def test_the_animation_it_uses_is_defined(self):
        """A class with no CSS behind it is an invisible progress bar."""
        assert ".scan-progress.indeterminate span" in MARKUP
        assert "@keyframes scan-indeterminate" in MARKUP

    def test_it_promises_to_close_itself(self):
        assert "closes itself when it is finished" in SCRIPT


@pytest.mark.parametrize("handler,endpoint", [
    ("sifTimezoneUse", "/api/sif/timezone"),
    ("goproTimezoneUse", "/api/gopro/timezone"),
])
class TestBothClockDialogs:
    def test_the_panel_is_shown_before_the_request_is_awaited(self, handler, endpoint):
        body = _handler(handler)
        assert "showWorkingDialog(" in body, f"{handler} never shows it"
        assert body.index("showWorkingDialog(") < body.index(f"api('{endpoint}'"), (
            f"{handler} awaits the request before showing that it is working"
        )

    def test_the_window_closes_when_the_answer_lands(self, handler, endpoint):
        body = _handler(handler)
        assert "modal.classList.remove('show')" in body
        assert body.index(f"api('{endpoint}'") < body.index(
            "modal.classList.remove('show')"
        ), f"{handler} closes before the answer is stored"

    def test_the_refresh_runs_after_the_window_closes(self, handler, endpoint):
        """pollScan asks the next clock question; closing after it would shut
        that dialog the instant it opened."""
        body = _handler(handler)
        assert body.index("modal.classList.remove('show')") < body.index("pollScan()")

    def test_a_failure_leaves_the_reason_on_screen(self, handler, endpoint):
        body = _handler(handler)
        assert "showDialogFailure(" in body, (
            f"{handler} reports failure only through a toast, which a reader "
            "who looked away has already missed"
        )


class TestTheFailurePanel:
    def test_it_offers_a_way_out_and_a_way_back(self):
        body = SCRIPT[SCRIPT.index("function showDialogFailure("):]
        body = body[: body.index("\n  // FLOX and FULL")]
        assert "dialogFailureClose" in body
        assert "dialogFailureRetry" in body

    def test_retrying_reopens_the_question(self):
        assert "() => askSifTimezone()" in SCRIPT
        assert "() => askGoproTimezone()" in SCRIPT

    def test_the_flag_is_still_only_latched_on_success(self):
        """A failed dialog must be shown again, or the clock stays undeclared."""
        for handler, flag in (
            ("sifTimezoneUse", "sifTimezoneAsked"),
            ("goproTimezoneUse", "goproTimezoneAsked"),
        ):
            body = _handler(handler)
            assert f"{flag} = true" in body
            assert body.index(f"{flag} = true") > body.index("api('/api/")


def test_no_message_is_styled_with_a_class_that_does_not_exist():
    """class="danger" has no rule behind it; the reason printed as plain text."""
    assert 'class="danger"' not in SCRIPT
    used = set(re.findall(r'class="(danger[a-z-]*)"', SCRIPT))
    for name in used:
        assert f".{name}" in MARKUP, f"{name} has no style"
