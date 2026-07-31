"""Check whether a newer release has been published, without ever installing it.

The software runs offline by design — a campaign disk on a laptop with no
network is a normal case — so the check must never block startup, never raise
into the interface, and never change what the operator sees when it simply
cannot reach the network.

Nothing is downloaded or installed. The result is a notice and a link.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .version import (
    SOFTWARE_VERSION,
    UPDATE_CHECK_ENVIRONMENT_VARIABLE,
    UPDATE_DOWNLOAD_URL,
    UPDATE_MANIFEST_URL,
)

REQUEST_TIMEOUT_SECONDS = 5.0
MAXIMUM_MANIFEST_BYTES = 64 * 1024
_DISABLED_VALUES = {"off", "0", "false", "no", "disabled"}
_VERSION_PATTERN = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """What the interface needs in order to show, or not show, a notice."""

    current_version: str = SOFTWARE_VERSION
    latest_version: str | None = None
    update_available: bool = False
    notice: str = ""
    download_url: str = UPDATE_DOWNLOAD_URL
    released_utc: str | None = None
    checked: bool = False
    enabled: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "notice": self.notice,
            "download_url": self.download_url,
            "released_utc": self.released_utc,
            "checked": self.checked,
            "enabled": self.enabled,
            "reason": self.reason,
        }


def update_check_enabled(environment: dict[str, str] | None = None) -> bool:
    """Whether the operator has left the check switched on.

    The check contacts a server, which reveals when and from where the software
    runs. Setting CCFLUX_UPDATE_CHECK=off turns it off for good.
    """
    source = os.environ if environment is None else environment
    value = str(source.get(UPDATE_CHECK_ENVIRONMENT_VARIABLE, "on")).strip().lower()
    return value not in _DISABLED_VALUES


def version_tuple(value: str) -> tuple[int, ...]:
    """Numeric parts of a version, so 1.10.0 sorts above 1.9.0."""
    match = _VERSION_PATTERN.match(str(value))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str, installed: str = SOFTWARE_VERSION) -> bool:
    """Whether ``candidate`` is a later release than ``installed``."""
    left, right = version_tuple(candidate), version_tuple(installed)
    if not left or not right:
        # An unreadable version is never treated as an update; announcing one
        # that does not exist is worse than announcing nothing.
        return False
    length = max(len(left), len(right))
    return left + (0,) * (length - len(left)) > right + (0,) * (length - len(right))


def check_for_update(
    *,
    manifest_url: str = UPDATE_MANIFEST_URL,
    current_version: str = SOFTWARE_VERSION,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    opener=urllib.request.urlopen,
    environment: dict[str, str] | None = None,
) -> UpdateStatus:
    """Fetch the manifest and report whether a newer release exists.

    Every failure — no network, a timeout, a malformed manifest — returns a
    status saying the check did not complete, never an exception.
    """
    if not update_check_enabled(environment):
        return UpdateStatus(
            current_version=current_version,
            enabled=False,
            reason="The update check is switched off for this installation.",
        )
    try:
        request = urllib.request.Request(
            manifest_url,
            headers={"User-Agent": f"CCFLUX/{current_version}"},
        )
        with opener(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read(MAXIMUM_MANIFEST_BYTES + 1))
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return UpdateStatus(
            current_version=current_version,
            reason=f"No update information could be retrieved: {error}",
        )
    except (json.JSONDecodeError, ValueError) as error:
        return UpdateStatus(
            current_version=current_version,
            reason=f"The update manifest could not be read: {error}",
        )
    if not isinstance(payload, dict):
        return UpdateStatus(
            current_version=current_version,
            reason="The update manifest is not an object.",
        )

    latest = str(payload.get("latest_version") or "").strip()
    if not latest:
        return UpdateStatus(
            current_version=current_version,
            checked=True,
            reason="The update manifest does not name a version.",
        )
    return UpdateStatus(
        current_version=current_version,
        latest_version=latest,
        update_available=is_newer(latest, current_version),
        notice=str(payload.get("notice") or "").strip(),
        download_url=str(payload.get("download_url") or UPDATE_DOWNLOAD_URL),
        released_utc=(
            str(payload["released_utc"]) if payload.get("released_utc") else None
        ),
        checked=True,
        reason="",
    )
