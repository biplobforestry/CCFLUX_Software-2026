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

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "assets"
SCRIPTS = sorted(ASSETS.glob("*.js"))

# The MIRO Rack workspace keeps its whole interface in a string inside a Python
# file, so no glob over *.js ever sees it and nothing in the Python toolchain
# looks at it either. It is the page the gas plots are drawn on.
EMBEDDED = ROOT / "legacy_integration" / "MIRO_Rack" / "MIRO_Rack_GUI.py"
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


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


def _chromium() -> str | None:
    """Edge ships with Windows, and Edge is V8.

    Without this the check skipped on every Windows machine in the campaign,
    which is all of them - so the scripts were only ever parsed if someone
    happened to have Node installed.
    """
    found = shutil.which("msedge") or shutil.which("chrome")
    if found:
        return found
    candidates = [
        Path(root) / "Microsoft/Edge/Application/msedge.exe"
        for root in filter(None, (os.environ.get("ProgramFiles"),
                                  os.environ.get("ProgramFiles(x86)")))
    ] + [
        Path(root) / "Google/Chrome/Application/chrome.exe"
        for root in filter(None, (os.environ.get("ProgramFiles"),
                                  os.environ.get("ProgramFiles(x86)")))
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


def _chromium_check(path: Path) -> tuple[bool, str]:
    """Parse without executing: new Function compiles and stops.

    --dump-dom is how the answer comes back out of a headless browser.
    """
    browser = _chromium()
    with tempfile.TemporaryDirectory() as folder:
        page = Path(folder) / "check.html"
        page.write_text(
            "<!doctype html><body><pre id=out>pending</pre><script type=module>\n"
            f"const src = await (await fetch({json.dumps(path.as_uri())})).text();\n"
            "let answer;\n"
            "try { new Function(src); answer = 'OK'; }\n"
            "catch (error) { answer = 'FAIL ' + error.name + ': ' + error.message; }\n"
            "document.getElementById('out').textContent = answer;\n"
            "</script></body>",
            encoding="utf-8",
        )
        result = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--allow-file-access-from-files", "--virtual-time-budget=6000",
             "--dump-dom", page.as_uri()],
            capture_output=True, text=True, check=False, timeout=180,
        )
    match = re.search(r'<pre id="out">(.*?)</pre>', result.stdout, re.S)
    if match is None:
        pytest.skip("Headless browser returned no answer")
    answer = html.unescape(match.group(1)).strip()
    return answer == "OK", answer


def _checker():
    if shutil.which("node"):
        return _node_check
    if sys.platform == "darwin" and shutil.which("osascript"):
        return _javascriptcore_check
    if _chromium():
        return _chromium_check
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


def _workspace_blocks() -> list[str]:
    source = EMBEDDED.read_text(encoding="utf-8")
    return [
        block for block in INLINE_SCRIPT.findall(source) if block.strip()
    ]


def test_the_workspace_page_has_a_script_to_check():
    blocks = _workspace_blocks()
    assert blocks, "no inline script found in the MIRO Rack workspace"
    assert any("renderMiro" in block for block in blocks)


def test_the_workspace_script_parses(tmp_path):
    """Every button on the page dies together on one syntax error, and the gas
    plots are drawn by this script."""
    checker = _checker()
    if checker is None:
        pytest.skip("No JavaScript engine available to parse with")

    for index, block in enumerate(_workspace_blocks(), start=1):
        path = tmp_path / f"workspace_{index}.js"
        path.write_text(block, encoding="utf-8")

        ok, detail = checker(path)

        assert ok, f"MIRO Rack workspace block {index} does not parse: {detail}"


# --------------------------------------------------------------------------
# An engine-free structural check, because the parse above skips on any machine
# without Node or JavaScriptCore - Windows, most CI images - and that is where a
# broken script would reach a release unnoticed. Unbalanced brackets are the
# failure this catches, which is the common shape of a bad scripted edit.
OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {value: key for key, value in OPENERS.items()}
# A slash after one of these starts a regular expression; after anything else it
# is division. The standard heuristic, and enough for this code.
REGEX_MAY_FOLLOW = set("(,=:[!&|?{};+-*%~^<>") | {""}


def delimiter_faults(source: str) -> list[str]:
    """Report unbalanced brackets, ignoring strings, comments and regexes."""
    faults: list[str] = []
    stack: list[tuple[str, int]] = []
    # Each entry is a template literal whose ${...} we are currently inside.
    template_depth: list[int] = []
    index = 0
    line = 1
    previous = ""
    length = len(source)
    while index < length:
        char = source[index]
        following = source[index + 1] if index + 1 < length else ""
        if char == "\n":
            line += 1
            index += 1
            continue
        if char == "/" and following == "/":
            index = source.find("\n", index)
            if index == -1:
                break
            continue
        if char == "/" and following == "*":
            end = source.find("*/", index + 2)
            if end == -1:
                faults.append(f"line {line}: unterminated block comment")
                break
            line += source.count("\n", index, end)
            index = end + 2
            continue
        if char in "\"'":
            index += 1
            while index < length and source[index] != char:
                if source[index] == "\\":
                    index += 1
                elif source[index] == "\n":
                    faults.append(f"line {line}: unterminated string")
                    break
                index += 1
            index += 1
            previous = "x"
            continue
        if char == "`":
            template_depth.append(len(stack))
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "`":
                    template_depth.pop()
                    index += 1
                    break
                if source[index] == "$" and source[index + 1: index + 2] == "{":
                    # Back to ordinary code until the matching brace.
                    stack.append(("{", line))
                    index += 2
                    break
                if source[index] == "\n":
                    line += 1
                index += 1
            else:
                faults.append(f"line {line}: unterminated template literal")
                break
            previous = "x"
            continue
        if char == "/" and previous in REGEX_MAY_FOLLOW:
            index += 1
            in_class = False
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "[":
                    in_class = True
                elif source[index] == "]":
                    in_class = False
                elif source[index] == "/" and not in_class:
                    break
                elif source[index] == "\n":
                    faults.append(f"line {line}: unterminated regular expression")
                    break
                index += 1
            index += 1
            previous = "x"
            continue
        if char in OPENERS:
            stack.append((char, line))
        elif char in CLOSERS:
            if not stack:
                faults.append(f"line {line}: stray '{char}'")
            else:
                opener, opened = stack.pop()
                if OPENERS[opener] != char:
                    faults.append(
                        f"line {line}: '{char}' closes '{opener}' opened on line {opened}"
                    )
                elif (
                    char == "}" and template_depth
                    and len(stack) == template_depth[-1]
                ):
                    # The end of a ${...}; the template continues.
                    index += 1
                    while index < length:
                        if source[index] == "\\":
                            index += 2
                            continue
                        if source[index] == "`":
                            template_depth.pop()
                            index += 1
                            break
                        if source[index] == "$" and source[index + 1: index + 2] == "{":
                            stack.append(("{", line))
                            index += 2
                            break
                        if source[index] == "\n":
                            line += 1
                        index += 1
                    previous = "x"
                    continue
        if not char.isspace():
            previous = char
        index += 1
    for opener, opened in stack:
        faults.append(f"line {opened}: '{opener}' is never closed")
    return faults


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_script_brackets_balance(script):
    """Runs everywhere, including where no JavaScript engine exists."""
    faults = delimiter_faults(script.read_text(encoding="utf-8"))

    assert not faults, f"{script.name}: " + "; ".join(faults)


class TestTheStructuralCheckIsWorthTrusting:
    """A checker that never reports anything would be worse than none."""

    @pytest.mark.parametrize("source,expected", [
        ("function a() { return 1;", "never closed"),
        ("const a = [1, 2;", "never closed"),
        ("if (a) { b(); ]", "closes"),
        ("const a = 1; }", "stray"),
    ])
    def test_it_finds_real_breakage(self, source, expected):
        faults = delimiter_faults(source)
        assert faults and any(expected in fault for fault in faults), faults

    @pytest.mark.parametrize("source", [
        "const path = '/api/{unclosed';",              # brace inside a string
        'const re = /[)]/;  const b = (1);',           # bracket inside a regex
        "const t = `a ${ {x: 1}.x } b`;",              # object inside a template
        "const t = `${ `${ 1 }` }`;",                  # nested template
        "// a stray ( in a comment\nconst a = 1;",
        "/* a stray } in a block */\nconst a = 1;",
        "const division = (4) / 2 / 1;",               # slashes that are not regexes
        "const s = \"a \\\" ) quote\";",               # escaped quote
    ])
    def test_it_does_not_cry_wolf(self, source):
        assert delimiter_faults(source) == []


def test_the_declaration_that_broke_the_interface_is_gone():
    """Pinned by name, because this one cost a working interface for several
    releases and the failure gives no clue in the browser."""
    source = (ASSETS / "dashboard.js").read_text(encoding="utf-8")

    assert "function openSifConfiguration(options" not in source
    assert "function openSifConfiguration(dialogOptions" in source


def test_no_source_file_contains_a_control_byte():
    """A NUL reached app/assets/sif.js through a scripted edit and shipped in
    three commits. JavaScript accepts it inside a string literal, so it parsed,
    ran, and quietly used a NUL where a space was written -
    `names.join('\\0')`. Nothing looked wrong until the byte was printed.
    """
    root = Path(__file__).resolve().parents[1]
    suspect = []
    for folder in ("app", "core", "instruments", "tests"):
        for path in sorted((root / folder).rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".js", ".html", ".css"}:
                continue
            if "__pycache__" in path.parts:
                continue
            data = path.read_bytes()
            # Tab, newline and carriage return are the legitimate ones.
            found = {byte for byte in data if byte < 0x20 and byte not in (0x09, 0x0A, 0x0D)}
            if found:
                suspect.append(f"{path.relative_to(root)}: {sorted(hex(b) for b in found)}")

    assert not suspect, "control bytes in source: " + "; ".join(suspect)
