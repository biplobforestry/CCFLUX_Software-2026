"""The running software version, and how an available update is discovered.

Keep ``SOFTWARE_VERSION`` in step with ``pyproject.toml``; a test asserts they
match, because an update check comparing the wrong number is worse than none.
"""

from __future__ import annotations

SOFTWARE_VERSION = "1.1.0"

# The manifest is served from the repository over raw.githubusercontent.com, so
# publishing a release is a commit rather than separate hosting.
UPDATE_MANIFEST_URL = (
    "https://raw.githubusercontent.com/biplobforestry/"
    "CCFLUX_Software-2026/main/update_manifest.json"
)

# Where an operator goes to get the new version. Nothing is ever downloaded or
# installed automatically: a scientific run must not have its software replaced
# underneath it, and a project written by one version being reopened by another
# mid-campaign is exactly the situation to avoid.
UPDATE_DOWNLOAD_URL = "https://github.com/biplobforestry/CCFLUX_Software-2026"

# Setting this to off/0/false/no disables the check completely.
UPDATE_CHECK_ENVIRONMENT_VARIABLE = "CCFLUX_UPDATE_CHECK"


# A version number alone cannot answer "am I running the code I just pulled?".
# It only changes at a release, so an operator who fetched instead of pulled, or
# who started the wrong copy, sees the same 1.0.1 either way. The fingerprint is
# derived from the source actually loaded, so it changes whenever the code does.
import hashlib
from pathlib import Path as _Path

_FINGERPRINT_ROOTS = (
    ("app", "*.py"), ("core", "*.py"), ("instruments", "*.py"),
    ("app/assets", "*.js"), ("app/assets", "*.html"),
)
_fingerprint_cache: str | None = None


def build_fingerprint(application_root: _Path | None = None) -> str:
    """Short digest of the source this process would run.

    Cached: it is read once per process, and the files cannot change under a
    running server in any way that would already be safe.
    """
    global _fingerprint_cache
    if _fingerprint_cache is not None:
        return _fingerprint_cache
    root = _Path(application_root or _Path(__file__).resolve().parent.parent)
    digest = hashlib.sha256()
    counted = 0
    for folder, pattern in _FINGERPRINT_ROOTS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(pattern)):
            if "__pycache__" in path.parts or ".venv" in str(path):
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
            counted += 1
    # A digest of nothing is a perfectly stable-looking value that means the
    # root was wrong - which is precisely the mistake this is meant to catch.
    _fingerprint_cache = digest.hexdigest()[:10] if counted else "unknown"
    return _fingerprint_cache
