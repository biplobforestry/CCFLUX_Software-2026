"""Every browser script must actually parse.

A single syntax error takes the whole file with it: the browser refuses to run
any of it, so every button on the page stops responding at once and nothing in
the interface says why. That shipped — `openSifConfiguration(options = {})` was
given a parameter named `options` while the body already declared
`const options`, and from that commit until it was found, no button in the main
window worked at all.

Nothing caught it. The other tests assert that particular text appears in the
scripts, which a broken file satisfies perfectly well.

macOS provides JavaScriptCore through `osascript -l JavaScript`, and Node is
used when it is present, so on a developer machine this is a real parse. Where
neither exists the check skips rather than pretending.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
SCRIPTS = sorted(ASSETS.glob("*.js"))


def _node_check(path: Path) -> tuple[bool, str]:
    result = subprocess.run(
        ["node", "--check", str(path)], capture_output=True, text=True, check=False
    )
    return result.returncode == 0, result.stderr.strip()


def _javascriptcore_check(path: Path) -> tuple[bool, str]:
    """Parse without executing: new Function(src) compiles and stops."""
    script = (
        'ObjC.import("Foundation");\n'
        f"const src = $.NSString.stringWithContentsOfFileEncodingError({json.dumps(str(path))}, 4, null).js;\n"
        'try { new Function(src); "OK"; } catch (e) { "FAIL " + e.message; }'
    )
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"JavaScriptCore unavailable: {result.stderr.strip()}")
    answer = result.stdout.strip()
    return answer == "OK", answer


def _checker():
    if shutil.which("node"):
        return _node_check
    if sys.platform == "darwin" and shutil.which("osascript"):
        return _javascriptcore_check
    return None


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_script_parses(script):
    checker = _checker()
    if checker is None:
        pytest.skip("No JavaScript engine available to parse with")

    ok, detail = checker(script)

    assert ok, f"{script.name} does not parse: {detail}"


def test_there_are_scripts_to_check():
    """A glob that quietly matches nothing would make this suite meaningless."""
    assert len(SCRIPTS) >= 8
    assert any(path.name == "dashboard.js" for path in SCRIPTS)


def test_the_declaration_that_broke_the_interface_is_gone():
    """Pinned by name, because this one cost a working interface for several
    releases and the failure gives no clue in the browser."""
    source = (ASSETS / "dashboard.js").read_text(encoding="utf-8")

    assert "function openSifConfiguration(options" not in source
    assert "function openSifConfiguration(dialogOptions" in source
