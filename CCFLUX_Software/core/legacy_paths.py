"""Portable, read-only lookup for protected legacy scientific modules."""

from __future__ import annotations

import os
from pathlib import Path


_ENVIRONMENT_VARIABLE = "CCFLUX_LEGACY_ROOT"
_BUNDLED_ROOT = Path(__file__).resolve().parents[1] / "legacy_integration"
_WINDOWS_ROOT = Path(
    r"C:\My_PC\Zeppelin\3_Quick_look\Dashboard\Zeppelin_System\Integration_code"
)
_MACOS_ROOT = Path("/Volumes/Biplob/Zeppelin_System/Integration_code")


def legacy_integration_root() -> Path:
    """Return the first available legacy Integration_code root without writing to it."""
    configured = os.environ.get(_ENVIRONMENT_VARIABLE)
    candidates = tuple(
        path
        for path in (
            Path(configured).expanduser() if configured else None,
            _BUNDLED_ROOT,
            _WINDOWS_ROOT,
            _MACOS_ROOT,
        )
        if path is not None
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def legacy_integration_path(*parts: str) -> Path:
    """Resolve a protected legacy module or fixture below Integration_code."""
    return legacy_integration_root().joinpath(*parts)
