"""Instrument files written in cp1252 must load, not end the run.

Acquisition software on Windows writes headers in cp1252, where a degree sign
is the single byte 0xb0. UTF-8 rejects it outright, so a column named
``Temp [°C]`` failed a whole project with

    'utf-8' codec can't decode byte 0xb0 in position 37: invalid start byte

and named no file, leaving nothing to act on. The measurements themselves are
ASCII; it is the unit in the header that carries the byte.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from core.text_encoding import FALLBACK, UTF8, detect_encoding, open_text, read_text

ROOT = Path(__file__).resolve().parents[1]

# The reported failure, reproduced exactly. The three leading columns occupy 37
# bytes, so the degree sign lands on the position the operator's error named.
PREFIX = "TIMESTAMP,Latitude_deg,Longitude_deg,"
assert len(PREFIX.encode("cp1252")) == 37
HEADER = PREFIX + "°C,Temp [°C],Humidity\n"
ROWS = "2026-07-27T07:19:28Z,47.5735,9.2152,25.8,25.8,41.2\n"


def _cp1252_file(tmp_path: Path, name: str = "instrument.csv") -> Path:
    path = tmp_path / name
    path.write_bytes((HEADER + ROWS).encode("cp1252"))
    return path


def test_the_reported_byte_is_where_the_error_said_it_was(tmp_path):
    """Anchors the fixture to the operator's message rather than a guess."""
    raw = _cp1252_file(tmp_path).read_bytes()

    with pytest.raises(UnicodeDecodeError) as failure:
        raw.decode("utf-8")

    assert failure.value.object[failure.value.start] == 0xB0
    assert failure.value.start == 37, "the reported position"


def test_a_cp1252_file_is_detected(tmp_path):
    assert detect_encoding(_cp1252_file(tmp_path)) == FALLBACK


def test_a_utf8_file_is_left_alone(tmp_path):
    path = tmp_path / "utf8.csv"
    path.write_text(HEADER + ROWS, encoding="utf-8")

    assert detect_encoding(path) == UTF8


def test_the_degree_sign_survives_the_fallback(tmp_path):
    """cp1252 turns 0xb0 back into the character the instrument meant, rather
    than the replacement character a lenient UTF-8 read would leave."""
    text = read_text(_cp1252_file(tmp_path))

    assert "Temp [°C]" in text
    assert "�" not in text


def test_the_values_are_read_correctly(tmp_path):
    import csv

    with open_text(_cp1252_file(tmp_path), newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert rows[0]["Latitude_deg"] == "47.5735"
    assert rows[0]["Temp [°C]"] == "25.8"


def test_a_stray_byte_deep_in_a_utf8_file_does_not_end_the_run(tmp_path):
    """Beyond the probe, one character degrades instead of an hour of work."""
    path = tmp_path / "mostly_utf8.csv"
    padding = ("x" * 99 + "\n").encode("utf-8") * 20_000     # ~2 MB, past the probe
    path.write_bytes(HEADER.encode("utf-8") + padding + b"tail,\xb0,1,2,3\n")

    assert detect_encoding(path) == UTF8
    text = read_text(path)

    assert "TIMESTAMP" in text, "the file still loads"
    assert "�" in text, "the unreadable byte degrades to one character"


def test_an_unreadable_file_is_not_reported_as_an_encoding_problem(tmp_path):
    missing = tmp_path / "gone.csv"

    assert detect_encoding(missing) == UTF8


def test_a_short_file_is_decided_on_what_it_has(tmp_path):
    path = tmp_path / "tiny.csv"
    path.write_bytes("a,b\n1,2\n".encode("utf-8"))

    assert detect_encoding(path) == UTF8


def test_writing_through_the_reader_is_refused(tmp_path):
    with pytest.raises(ValueError, match="reads"):
        open_text(_cp1252_file(tmp_path), "w")


@pytest.mark.parametrize(
    "script",
    [
        "instruments/sif/legacy/noseboom_gimbal_for_sif.py",
        "instruments/sif/legacy/convert_gremsy_gimbal_to_sif.py",
        "legacy_integration/Noseboom/noseboom_browser_GUI.py",
    ],
)
def test_the_standalone_scripts_carry_their_own_detection(script):
    """They run without importing core, so each needs the check itself."""
    source = (ROOT / script).read_text(encoding="utf-8")

    assert "def detect_encoding(" in source
    assert "cp1252" in source
    assert ".open(\"r\", newline=\"\", encoding=\"utf-8-sig\")" not in source


def test_the_sif_preparation_script_reads_a_cp1252_noseboom_file(tmp_path):
    """The end the operator actually hit: SIF preparing its position input."""
    spec = importlib.util.spec_from_file_location(
        "nb_gimbal_sif", ROOT / "instruments/sif/legacy/noseboom_gimbal_for_sif.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    path = _cp1252_file(tmp_path, "NoseBoom.csv")

    assert module.detect_encoding(path) == "cp1252"
    with module.open_text(path) as stream:
        assert "Temp [°C]" in stream.read()
