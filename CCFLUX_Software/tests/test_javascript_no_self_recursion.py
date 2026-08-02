"""No browser function may call itself as its whole body.

A bulk find-and-replace across dashboard.js once rewrote the one line inside
`showModal` that actually showed the window, turning it into a call to
`showModal` itself. The result parsed cleanly, passed every static assertion
about which strings appeared where, and shipped -- but the first click on
Flight Folder recursed until the stack gave out and no dialog ever opened.

Parsing proves a file is well-formed, not that it does anything. This checks the
one shape that mistake takes: a function whose body calls nothing but itself.
Real recursion has a base case and other statements, so it is not caught here.
"""

import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
SCRIPTS = sorted(ASSETS.glob("*.js"))

DECLARATION = re.compile(r"^(\s*)function\s+([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def _body(source: str, start: int, indent: str) -> str:
    """Text from the declaration to the closing brace at its own indent."""
    closing = f"\n{indent}}}"
    end = source.find(closing, start)
    return source[start:end] if end != -1 else source[start:]


def _statements(body: str) -> list[str]:
    lines = []
    for line in body.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*"):
            continue
        lines.append(stripped)
    return lines


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_no_function_body_is_only_a_call_to_itself(script):
    source = script.read_text(encoding="utf-8")

    offenders = []
    for match in DECLARATION.finditer(source):
        indent, name = match.group(1), match.group(2)
        statements = _statements(_body(source, match.start(), indent))
        calls_self = [line for line in statements if re.match(rf"{name}\s*\(", line)]
        if not calls_self:
            continue
        # A guard or an early return means it can terminate; a body that only
        # ever calls itself cannot.
        has_escape = any(
            keyword in line
            for line in statements
            for keyword in ("if", "return", "while", "for", "?", "&&", "||")
        )
        if not has_escape:
            offenders.append(f"{name} (line {source[:match.start()].count(chr(10)) + 1})")

    assert not offenders, (
        f"{script.name}: function(s) that only call themselves: {', '.join(offenders)}"
    )


def test_show_modal_actually_shows_the_modal():
    """Pinned by name: this is the one that shipped broken."""
    source = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
    body = source[source.index("function showModal()"):]
    body = body[: body.index("\n  }")]

    assert "modal.classList.add('show')" in body
    # Comments discuss the mistake by name, so only real statements count.
    assert "showModal()" not in "\n".join(_statements(body))
