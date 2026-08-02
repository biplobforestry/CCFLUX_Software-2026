"""Open instrument text files that are not always UTF-8.

Loggers and acquisition software on Windows commonly write their headers in
cp1252, where a degree sign is the single byte 0xb0. UTF-8 rejects that byte
outright, so a column named ``Temp [°C]`` ended a whole run with

    'utf-8' codec can't decode byte 0xb0 in position 37: invalid start byte

and nothing said which file was at fault. The measurements themselves are
ASCII; it is almost always a unit in the header that carries the byte.

So the encoding is decided from the head of the file, where such a name lives.
If that region is valid UTF-8 the file is read as UTF-8, tolerating a stray byte
further in rather than losing an hour of processing to it. If it is not, the
file is read as cp1252, which has no invalid bytes and turns 0xb0 back into the
degree sign the instrument meant.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO

# Enough to cover any realistic header line, and cheap to read.
PROBE_BYTES = 1 << 20

UTF8 = "utf-8-sig"
FALLBACK = "cp1252"


def detect_encoding(path: Path | str, *, probe_bytes: int = PROBE_BYTES) -> str:
    """Return the encoding to read *path* with.

    A file too short to fill the probe is decided on what it has. An unreadable
    file is reported as UTF-8 and left for the caller's own error handling, so
    that a permissions problem is not reported as an encoding problem.
    """
    try:
        with Path(path).open("rb") as stream:
            head = stream.read(probe_bytes)
    except OSError:
        return UTF8

    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return FALLBACK
    return UTF8


def open_text(
    path: Path | str, mode: str = "r", *, newline: str | None = None, **kwargs
) -> IO:
    """Open *path* for reading with the encoding it is actually written in.

    UTF-8 files are opened with ``errors="replace"`` so that a single unexpected
    byte deep inside a large file degrades one character instead of ending the
    run. cp1252 needs no such guard: every byte decodes.
    """
    if "r" not in mode:
        raise ValueError("open_text reads; write with an explicit encoding")
    encoding = detect_encoding(path)
    if encoding == UTF8:
        kwargs.setdefault("errors", "replace")
    return Path(path).open(mode, encoding=encoding, newline=newline, **kwargs)


def read_text(path: Path | str) -> str:
    with open_text(path) as stream:
        return stream.read()
