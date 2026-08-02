"""Run the modal window logic, rather than reading it.

`showModal` shipped calling itself instead of adding the class that shows the
window. It parsed, it satisfied every assertion about which strings appeared
where, and the first click on Flight Folder raised "Maximum call stack size
exceeded" -- so no dialog opened anywhere in the interface.

Nothing that only reads source would have caught that. These tests lift the
modal functions out of dashboard.js and execute them against a stub DOM in
JavaScriptCore, so a function that does not do what it says fails here.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "app" / "assets" / "dashboard.js"
FUNCTIONS = ("modalIsOpen", "showModal", "settleModalAnswer", "closeModal", "minimizeModal")

HARNESS = """
function makeElement(id) {
  const el = {
    id: id, hidden: false, textContent: '', innerHTML: '', _classes: {},
    addEventListener: function () {}
  };
  el.classList = {
    add: function (n) { el._classes[n] = true; },
    remove: function (n) { delete el._classes[n]; },
    contains: function (n) { return Boolean(el._classes[n]); }
  };
  return el;
}

const modal = makeElement('modal');
const modalRestore = makeElement('modalRestore');
const modalRestoreLabel = makeElement('modalRestoreLabel');
const modalTitle = makeElement('modalTitle');
let modalPendingAnswer = null;
let modalMinimized = false;

__BODY__

const out = {};
showModal();
out.opens = modal.classList.contains('show');

let answered = 'none';
modalPendingAnswer = function (v) { answered = v; };
modalTitle.textContent = 'SIF options';
minimizeModal();
out.minimizeHides = !modal.classList.contains('show');
out.minimizeStaysOpen = modalIsOpen();
out.chipShown = modalRestore.hidden === false;
out.chipLabelled = modalRestoreLabel.textContent === 'SIF options';
out.minimizeDoesNotAnswer = answered === 'none';

closeModal();
out.closeAnswers = answered === false;
out.chipHidden = modalRestore.hidden === true;
out.nothingOpen = !modalIsOpen();

showModal();
minimizeModal();
showModal();
out.freshDialogClearsChip = modalRestore.hidden === true;
out.freshDialogShown = modal.classList.contains('show');

modalPendingAnswer = null;
closeModal();
out.closeWithoutPendingIsSafe = true;

JSON.stringify(out);
"""


def _lift(name: str, source: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index("\n  }", start) + len("\n  }")
    return source[start:end]


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("osascript"):
        pytest.skip("JavaScriptCore is only reachable through osascript on macOS")

    source = SCRIPT.read_text(encoding="utf-8")
    body = "\n\n".join(_lift(name, source) for name in FUNCTIONS)
    built = tmp_path_factory.mktemp("modal") / "runtime.js"
    built.write_text(HARNESS.replace("__BODY__", body), encoding="utf-8")

    result = subprocess.run(
        ["osascript", "-l", "JavaScript", str(built)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        # A stack overflow from a self-calling function lands here, which is
        # the whole point of running this at all.
        pytest.fail(f"the modal logic did not run: {result.stderr.strip()}")
    return json.loads(result.stdout.strip())


@pytest.mark.parametrize(
    "behaviour",
    [
        "opens",
        "minimizeHides",
        "minimizeStaysOpen",
        "chipShown",
        "chipLabelled",
        "minimizeDoesNotAnswer",
        "closeAnswers",
        "chipHidden",
        "nothingOpen",
        "freshDialogClearsChip",
        "freshDialogShown",
        "closeWithoutPendingIsSafe",
    ],
)
def test_modal_behaviour(outcome, behaviour):
    assert outcome[behaviour] is True
