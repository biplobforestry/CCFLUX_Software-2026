"""Hybrid processing: authorised work packages, and fusion of their results.

A flight can be processed across several computers. The primary machine decides
once what the campaign settings are and which instruments each worker takes,
then hands out one package per worker. A worker chooses only where its own data
and output live; everything scientific is fixed and cannot be edited.

What the package actually guarantees
------------------------------------
The package is encrypted with AES-256-GCM, which is authenticated encryption:
the same operation that hides the contents also detects any change to them. A
package that has been edited, truncated or re-assembled from two others fails to
decrypt and is refused - there is no "read it anyway" path.

The key is derived with scrypt from a passphrase the primary operator sets, so
the packages are readable only by workers who were given it. The passphrase is
never written into a package.

This is a symmetric scheme: every holder of the passphrase can both read and
create packages. It stops a worker processing instruments they were not given,
and it makes tampering evident. It is not a public-key signature and does not
prove *which* colleague produced a result - the audit trail records who claimed
to. That distinction is stated here rather than left for someone to assume.
"""

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID, uuid4

from .exceptions import ProjectFileError

PACKAGE_SUFFIX = ".ccflux"
WORK_PACKAGE_KIND = "hybrid_work_package"
RESULT_PACKAGE_KIND = "hybrid_result_package"

# Anyone can read these without the passphrase. They carry no configuration and
# no science - only enough to tell an operator what the file is and whether they
# hold the right passphrase for it before they are asked for one.
CLEARTEXT_HEADER_NAME = "hybrid_header.json"
SEALED_PAYLOAD_NAME = "hybrid_payload.enc"

# scrypt parameters. n=2**15 keeps derivation near a fifth of a second on a
# campaign laptop, which is unnoticeable once per package and expensive in bulk.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
# 128 * N * r is exactly OpenSSL's default 32 MB ceiling, which it rejects
# rather than allows. Ask for headroom explicitly.
SCRYPT_MAX_MEMORY = 128 * SCRYPT_N * SCRYPT_R * 2
KEY_LENGTH = 32
SALT_LENGTH = 16
NONCE_LENGTH = 12

MINIMUM_FUSION_PACKAGES = 2
MAXIMUM_FUSION_PACKAGES = 4


class HybridPackageError(ProjectFileError, ValueError):
    """A work or result package could not be produced, opened or trusted.

    Also a ValueError, because every one of these is something the operator
    needs to read: a wrong passphrase, an altered package, an instrument given
    to two computers. The HTTP layer answers ValueError with the message and
    anything else with a generic failure, so without this the useful part would
    be replaced by "the local application could not complete the request".
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:      # pragma: no cover - dependency is declared
        raise HybridPackageError(
            "Hybrid processing needs the 'cryptography' package. Run the "
            "launcher once with an internet connection to install it."
        ) from exc
    return AESGCM(key)


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Turn a passphrase into a key. Slow on purpose."""
    import hashlib

    if not passphrase or not passphrase.strip():
        raise HybridPackageError("A passphrase is required to seal a package")
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_LENGTH,
        maxmem=SCRYPT_MAX_MEMORY,
    )


@dataclass(slots=True)
class WorkerAssignment:
    """One worker's share of the flight."""

    worker_id: str
    worker_name: str
    instruments: tuple[str, ...]

    def validate(self, available: Sequence[str]) -> None:
        if not self.worker_name.strip():
            raise HybridPackageError("Every worker package needs a name")
        if not self.instruments:
            raise HybridPackageError(
                f"{self.worker_name} has no instruments assigned. Remove the "
                "package or give it something to process."
            )
        unknown = sorted(set(self.instruments) - set(available))
        if unknown:
            raise HybridPackageError(
                f"{self.worker_name} was assigned instruments that are not in "
                "this flight: " + ", ".join(unknown)
            )


@dataclass(slots=True)
class HybridPlan:
    """What the primary machine decided, before anything is written."""

    project_id: str
    flight_id: str
    campaign: str
    analysis_start: str | None
    analysis_end: str | None
    available_instruments: tuple[str, ...]
    assignments: tuple[WorkerAssignment, ...]
    primary_instruments: tuple[str, ...]
    instrument_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    display_timezone: str = "UTC"

    def validate(self) -> None:
        if not self.flight_id.strip():
            raise HybridPackageError("The flight must have an ID before it is split")
        if not self.analysis_start or not self.analysis_end:
            raise HybridPackageError(
                "Set the processing time range before creating work packages"
            )
        if not self.assignments:
            raise HybridPackageError("Create at least one worker package")
        if len(self.assignments) > MAXIMUM_FUSION_PACKAGES:
            raise HybridPackageError(
                f"At most {MAXIMUM_FUSION_PACKAGES} worker packages can be fused "
                "back together, so no more than that are created."
            )
        seen: dict[str, str] = {}
        for assignment in self.assignments:
            assignment.validate(self.available_instruments)
            for instrument in assignment.instruments:
                if instrument in seen:
                    raise HybridPackageError(
                        f"{instrument} is assigned to both {seen[instrument]} and "
                        f"{assignment.worker_name}. Each instrument belongs to one "
                        "computer, or the results would collide at fusion."
                    )
                seen[instrument] = assignment.worker_name
        overlap = sorted(set(self.primary_instruments) & set(seen))
        if overlap:
            raise HybridPackageError(
                "These are kept on the primary computer and also handed out: "
                + ", ".join(overlap)
            )

    @property
    def unassigned(self) -> tuple[str, ...]:
        """Instruments nobody will process. Allowed, but worth saying."""
        taken = set(self.primary_instruments)
        for assignment in self.assignments:
            taken.update(assignment.instruments)
        return tuple(
            item for item in self.available_instruments if item not in taken
        )


def _header(kind: str, plan_or_payload: Mapping[str, Any], salt: bytes) -> dict:
    return {
        "kind": kind,
        "format_version": 1,
        "project_id": plan_or_payload["project_id"],
        "flight_id": plan_or_payload["flight_id"],
        "campaign": plan_or_payload.get("campaign", ""),
        "worker_id": plan_or_payload.get("worker_id"),
        "worker_name": plan_or_payload.get("worker_name"),
        "created_utc": _now(),
        "salt": salt.hex(),
        "software_version": plan_or_payload.get("software_version", ""),
    }


def _seal(path: Path, kind: str, payload: dict, passphrase: str) -> Path:
    salt = os.urandom(SALT_LENGTH)
    nonce = os.urandom(NONCE_LENGTH)
    header = _header(kind, payload, salt)
    key = derive_key(passphrase, salt)
    # The header is bound into the ciphertext as associated data, so swapping a
    # package's header onto another package's payload fails to decrypt.
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
    sealed = _aesgcm(key).encrypt(
        nonce, json.dumps(payload, sort_keys=True).encode("utf-8"), header_bytes
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(CLEARTEXT_HEADER_NAME, json.dumps(header, indent=2) + "\n")
            archive.writestr(SEALED_PAYLOAD_NAME, nonce + sealed)
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HybridPackageError(f"Could not write {path}: {exc}") from exc
    return path


def read_package_header(path: Path) -> dict:
    """What a package says it is, without the passphrase."""
    try:
        with zipfile.ZipFile(path) as archive:
            if CLEARTEXT_HEADER_NAME not in archive.namelist():
                raise HybridPackageError(
                    f"{Path(path).name} is not a hybrid processing package."
                )
            return json.loads(archive.read(CLEARTEXT_HEADER_NAME))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise HybridPackageError(f"{Path(path).name} could not be read: {exc}") from exc


def _open(path: Path, passphrase: str, expected_kind: str) -> tuple[dict, dict]:
    header = read_package_header(path)
    if header.get("kind") != expected_kind:
        raise HybridPackageError(
            f"{Path(path).name} is a {header.get('kind', 'unknown')} package, "
            f"not a {expected_kind}."
        )
    try:
        with zipfile.ZipFile(path) as archive:
            blob = archive.read(SEALED_PAYLOAD_NAME)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise HybridPackageError(f"{Path(path).name} is incomplete: {exc}") from exc
    key = derive_key(passphrase, bytes.fromhex(header["salt"]))
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
    try:
        opened = _aesgcm(key).decrypt(
            blob[:NONCE_LENGTH], blob[NONCE_LENGTH:], header_bytes
        )
    except Exception as exc:
        # AES-GCM cannot tell a wrong key from an altered package, and neither
        # can we. Both mean the same thing to the operator: do not trust it.
        raise HybridPackageError(
            f"{Path(path).name} could not be opened. Either the passphrase is "
            "wrong, or the package has been altered since it was created."
        ) from exc
    return header, json.loads(opened)


def create_work_packages(
    plan: HybridPlan,
    destination: Path,
    passphrase: str,
    *,
    software_version: str,
) -> list[Path]:
    """One sealed package per worker. The primary keeps its own instruments."""
    plan.validate()
    destination = Path(destination)
    written: list[Path] = []
    for assignment in plan.assignments:
        payload = {
            "project_id": plan.project_id,
            "flight_id": plan.flight_id,
            "campaign": plan.campaign,
            "worker_id": assignment.worker_id,
            "worker_name": assignment.worker_name,
            "assigned_instruments": list(assignment.instruments),
            "analysis_start": plan.analysis_start,
            "analysis_end": plan.analysis_end,
            "display_timezone": plan.display_timezone,
            "instrument_options": {
                key: value for key, value in plan.instrument_options.items()
            },
            "software_version": software_version,
            "audit": {
                "created_utc": _now(),
                "created_by_software": software_version,
                "plan_workers": [item.worker_name for item in plan.assignments],
                "primary_instruments": list(plan.primary_instruments),
                "unassigned_instruments": list(plan.unassigned),
            },
        }
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in assignment.worker_name
        ).strip("_") or assignment.worker_id[:8]
        target = destination / f"{plan.flight_id}_{safe}{PACKAGE_SUFFIX}"
        written.append(_seal(target, WORK_PACKAGE_KIND, payload, passphrase))
    return written


@dataclass(slots=True)
class LoadedWorkPackage:
    """An opened, trusted work package. Everything here is fixed."""

    path: Path
    header: dict
    payload: dict

    @property
    def project_id(self) -> str: return str(self.payload["project_id"])
    @property
    def flight_id(self) -> str: return str(self.payload["flight_id"])
    @property
    def worker_name(self) -> str: return str(self.payload["worker_name"])
    @property
    def worker_id(self) -> str: return str(self.payload["worker_id"])
    @property
    def assigned_instruments(self) -> tuple[str, ...]:
        return tuple(self.payload.get("assigned_instruments", ()))

    def authorises(self, instrument_id: str) -> bool:
        return instrument_id in self.assigned_instruments

    def require(self, instrument_ids: Iterable[str]) -> None:
        """Refuse anything the package did not grant."""
        refused = sorted(set(instrument_ids) - set(self.assigned_instruments))
        if refused:
            raise HybridPackageError(
                "This work package does not authorise: " + ", ".join(refused)
                + ". It covers " + (", ".join(self.assigned_instruments) or "nothing")
                + "."
            )


def load_work_package(path: Path, passphrase: str) -> LoadedWorkPackage:
    header, payload = _open(Path(path), passphrase, WORK_PACKAGE_KIND)
    return LoadedWorkPackage(Path(path), header, payload)


def export_result_package(
    work_package: LoadedWorkPackage,
    project_file: Path,
    destination: Path,
    passphrase: str,
    *,
    software_version: str,
    processed_instruments: Sequence[str],
    log_records: Sequence[Mapping[str, Any]] = (),
) -> Path:
    """Seal a worker's processed project together with its authorisation."""
    import hashlib

    work_package.require(processed_instruments)
    project_file = Path(project_file)
    if not project_file.is_file():
        raise HybridPackageError(f"No processed project to export: {project_file}")
    data = project_file.read_bytes()
    payload = {
        "project_id": work_package.project_id,
        "flight_id": work_package.flight_id,
        "campaign": work_package.payload.get("campaign", ""),
        "worker_id": work_package.worker_id,
        "worker_name": work_package.worker_name,
        "assigned_instruments": list(work_package.assigned_instruments),
        "processed_instruments": sorted(set(processed_instruments)),
        "analysis_start": work_package.payload.get("analysis_start"),
        "analysis_end": work_package.payload.get("analysis_end"),
        "instrument_options": work_package.payload.get("instrument_options", {}),
        "software_version": software_version,
        "project_file_name": project_file.name,
        "project_sha256": hashlib.sha256(data).hexdigest(),
        "project_bytes": len(data),
        "project_base64": __import__("base64").b64encode(data).decode("ascii"),
        "log_records": [dict(record) for record in log_records],
        "audit": {
            "work_package_created_utc": work_package.header.get("created_utc"),
            "processed_utc": _now(),
            "processed_by_software": software_version,
        },
    }
    safe = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in work_package.worker_name
    ).strip("_") or work_package.worker_id[:8]
    target = Path(destination) / f"{work_package.flight_id}_{safe}_results{PACKAGE_SUFFIX}"
    return _seal(target, RESULT_PACKAGE_KIND, payload, passphrase)


@dataclass(slots=True)
class LoadedResultPackage:
    path: Path
    header: dict
    payload: dict

    @property
    def worker_name(self) -> str: return str(self.payload["worker_name"])
    @property
    def processed_instruments(self) -> tuple[str, ...]:
        return tuple(self.payload.get("processed_instruments", ()))

    def project_bytes(self) -> bytes:
        import base64
        import hashlib

        data = base64.b64decode(self.payload["project_base64"])
        if hashlib.sha256(data).hexdigest() != self.payload["project_sha256"]:
            raise HybridPackageError(
                f"{self.path.name}: the processed project inside it does not "
                "match its own checksum."
            )
        return data


def load_result_package(path: Path, passphrase: str) -> LoadedResultPackage:
    header, payload = _open(Path(path), passphrase, RESULT_PACKAGE_KIND)
    return LoadedResultPackage(Path(path), header, payload)


@dataclass(slots=True)
class FusionReport:
    """What fusion would do, or refused to do."""

    ok: bool
    reasons: tuple[str, ...]
    packages: tuple[dict, ...]
    instruments: tuple[str, ...]
    project_id: str | None = None
    flight_id: str | None = None


def review_fusion(packages: Sequence[LoadedResultPackage]) -> FusionReport:
    """Check that these results belong together, before merging anything."""
    reasons: list[str] = []
    if not (MINIMUM_FUSION_PACKAGES <= len(packages) <= MAXIMUM_FUSION_PACKAGES):
        reasons.append(
            f"Fusion takes {MINIMUM_FUSION_PACKAGES} to {MAXIMUM_FUSION_PACKAGES} "
            f"result packages; {len(packages)} were selected."
        )
    project_ids = {item.payload["project_id"] for item in packages}
    flight_ids = {item.payload["flight_id"] for item in packages}
    if len(project_ids) > 1:
        reasons.append(
            "These results are from different projects: " + ", ".join(sorted(project_ids))
        )
    if len(flight_ids) > 1:
        reasons.append(
            "These results are from different flights: " + ", ".join(sorted(flight_ids))
        )
    intervals = {
        (item.payload.get("analysis_start"), item.payload.get("analysis_end"))
        for item in packages
    }
    if len(intervals) > 1:
        reasons.append(
            "The processing time range differs between packages; they were not "
            "produced from the same plan."
        )
    options = {
        json.dumps(item.payload.get("instrument_options", {}), sort_keys=True)
        for item in packages
    }
    if len(options) > 1:
        reasons.append(
            "The processing configuration differs between packages; merging them "
            "would mix settings."
        )
    owner: dict[str, str] = {}
    duplicates: list[str] = []
    for item in packages:
        unauthorised = sorted(
            set(item.processed_instruments) - set(item.payload.get("assigned_instruments", ()))
        )
        if unauthorised:
            reasons.append(
                f"{item.worker_name} returned instruments it was not assigned: "
                + ", ".join(unauthorised)
            )
        for instrument in item.processed_instruments:
            if instrument in owner:
                duplicates.append(
                    f"{instrument} was processed by both {owner[instrument]} and "
                    f"{item.worker_name}"
                )
            else:
                owner[instrument] = item.worker_name
    reasons.extend(duplicates)
    for item in packages:
        try:
            item.project_bytes()
        except HybridPackageError as exc:
            reasons.append(str(exc))
    return FusionReport(
        ok=not reasons,
        reasons=tuple(reasons),
        packages=tuple(
            {
                "worker_name": item.worker_name,
                "path": str(item.path),
                "processed_instruments": list(item.processed_instruments),
                "processed_utc": item.payload.get("audit", {}).get("processed_utc"),
                "software_version": item.payload.get("software_version"),
            }
            for item in packages
        ),
        instruments=tuple(sorted(owner)),
        project_id=next(iter(project_ids), None) if len(project_ids) == 1 else None,
        flight_id=next(iter(flight_ids), None) if len(flight_ids) == 1 else None,
    )


def fuse(
    packages: Sequence[LoadedResultPackage],
    destination: Path,
    *,
    software_version: str,
) -> tuple[Path, FusionReport]:
    """Merge validated results into one project folder.

    Each worker's processed project is written out whole and a fusion manifest
    records which instruments came from whom. Nothing is merged unless every
    check in review_fusion passed - a partial fusion would be a project nobody
    could reason about.
    """
    report = review_fusion(packages)
    if not report.ok:
        raise HybridPackageError(
            "Fusion was cancelled:\n- " + "\n- ".join(report.reasons)
        )
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    contributions = []
    for item in packages:
        name = item.payload.get("project_file_name") or f"{item.worker_name}.ccflux"
        target = destination / f"{Path(name).stem}_{item.worker_name}{PACKAGE_SUFFIX}"
        target.write_bytes(item.project_bytes())
        contributions.append({
            "worker_name": item.worker_name,
            "worker_id": item.payload.get("worker_id"),
            "instruments": list(item.processed_instruments),
            "project_file": target.name,
            "processed_utc": item.payload.get("audit", {}).get("processed_utc"),
            "software_version": item.payload.get("software_version"),
            "sha256": item.payload.get("project_sha256"),
        })
    manifest = {
        "kind": "hybrid_fusion",
        "project_id": report.project_id,
        "flight_id": report.flight_id,
        "fused_utc": _now(),
        "fused_by_software": software_version,
        "instruments": list(report.instruments),
        "contributions": contributions,
    }
    manifest_path = destination / "fusion_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path, report
