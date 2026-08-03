"""A page script must come after every element it wires up.

flir.html declared its export dialog *below* the script tag. So when flir.js
ran, `$('exportClose')` was null, setting .onclick on it threw at module scope,
and `load()` on the last line never ran. The loading overlay is shown by the
markup and only removed by `load()`, so the page sat on "Preparing FLIR
workspace" for ever - with a working backend answering /api/flir in 60 ms.

Checking that an id exists in the file is not enough, and that is exactly the
check that passed while the page was broken. Position is what matters.
"""

import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
PAGES = sorted(ASSETS.glob("*.html"))


def _page_script(html):
    """The page's own script tag, ignoring vendored libraries."""
    for match in re.finditer(r'<script[^>]*src="([^"]+)"', html):
        if not match.group(1).startswith("/vendor/"):
            return match
    return None


def _script_source(html):
    match = _page_script(html)
    if match is None:
        return None
    candidate = ASSETS / Path(match.group(1)).name
    return candidate if candidate.is_file() else None


@pytest.mark.parametrize("page", PAGES, ids=lambda path: path.name)
def test_no_element_is_declared_after_the_script_that_uses_it(page):
    html = page.read_text(encoding="utf-8")
    match = _page_script(html)
    source = _script_source(html)
    if match is None or source is None:
        pytest.skip(f"{page.name} has no page script of its own")

    script = source.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", script)) | set(
        re.findall(r"getElementById\('([A-Za-z0-9_]+)'\)", script)
    )
    late = sorted(
        name for name in referenced
        if 0 <= html.find(f'id="{name}"') > match.start()
    )

    assert not late, (
        f"{page.name} declares {late} after {match.group(1)}; the script cannot "
        "reach them, and the error stops the whole file"
    )


def test_the_flir_page_loads_its_script_last():
    """Pinned by name: this is the page that shipped broken."""
    html = (ASSETS / "flir.html").read_text(encoding="utf-8")
    script = html.index('<script src="/flir.js">')

    assert html.index('id="exportModal"') < script
    assert html.rstrip().endswith("</body>\n</html>")


@pytest.mark.parametrize("page", PAGES, ids=lambda path: path.name)
def test_the_loading_overlay_is_only_removed_by_script(page):
    """The overlay starts visible in the markup, so any module-scope error
    leaves it up with nothing on screen saying why."""
    html = page.read_text(encoding="utf-8")
    if 'class="busy show"' not in html:
        pytest.skip(f"{page.name} has no loading overlay")
    source = _script_source(html)
    if source is None:
        pytest.skip(f"{page.name} has no page script of its own")

    script = source.read_text(encoding="utf-8")
    assert "classList.remove('show')" in script or 'classList.remove("show")' in script
