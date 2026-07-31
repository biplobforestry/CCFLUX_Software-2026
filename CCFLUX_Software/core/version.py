"""The running software version, and how an available update is discovered.

Keep ``SOFTWARE_VERSION`` in step with ``pyproject.toml``; a test asserts they
match, because an update check comparing the wrong number is worse than none.
"""

from __future__ import annotations

SOFTWARE_VERSION = "1.0.0"

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
UPDATE_DOWNLOAD_URL = "https://uni-koeln.sciebo.de/s/CCFLUX"

# Setting this to off/0/false/no disables the check completely.
UPDATE_CHECK_ENVIRONMENT_VARIABLE = "CCFLUX_UPDATE_CHECK"
