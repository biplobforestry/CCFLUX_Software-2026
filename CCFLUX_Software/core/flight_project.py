"""Human-readable flight-project persistence and raw-file change detection."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from .compat import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .exceptions import (
    DuplicateFlightIDError,
    ProjectFileError,
    ProjectOverwriteError,
    ProjectValidationError,
)

PROJECT_SCHEMA_VERSION = 1
PROJECT_SUFFIX = ".ccflux"
# Projects are named after the flight, so several campaign projects sitting in
# one folder — or attached to one message — stay distinguishable without being
# opened. Both earlier fixed names still open unchanged.
PROJECT_FILENAME = "flight_project.ccflux"
LEGACY_PROJECT_FILENAME = "flight_project.json"


def project_filename_for(flight_id: str) -> str:
    """The .ccflux filename for one flight, e.g. ``Flight_2707.ccflux``."""
    safe = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in str(flight_id).strip()
    ).strip("._")
    return f"{safe or 'flight_project'}{PROJECT_SUFFIX}"

# A .ccflux is a deflate-compressed archive so one file can be handed to a
# colleague complete. Older plain-JSON projects still open unchanged.
PROJECT_MANIFEST_NAME = "project.json"
BUNDLE_MANIFEST_NAME = "manifest.json"
BUNDLE_PREFIX = "products/"
# Browser payloads and diagnostics are always bundled; other products are
# bundled while they stay small, so a project never silently grows to gigabytes.
ALWAYS_BUNDLED_SUFFIXES = frozenset({".json", ".jsonl", ".md", ".txt"})
# Captured imagery is kept out of the project file: a .ccflux carries the
# processed results a colleague needs to see the plots and maps, and for GoPro
# the image identifiers only. Science plots are images too, so the rule is
# scoped to the instruments whose output is a copy of a photograph.
#
# FLIR is deliberately not in this set. Its sample frames are false-colour
# renderings of the thermal array, not copies of a picture, the FLIR page shows
# them by URL, and they are under a megabyte. The 39 GB thermal export itself is
# raw input and was never a candidate for bundling.
CAMERA_IMAGE_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng", ".raw", ".mp4", ".mov",
})
CAMERA_INSTRUMENT_IDS = frozenset({"gopro", "micasense"})


def _is_camera_image_product(relative: Path) -> bool:
    if relative.suffix.casefold() not in CAMERA_IMAGE_SUFFIXES:
        return False
    parts = {part.casefold() for part in relative.parts}
    return bool(parts & CAMERA_INSTRUMENT_IDS)
# Per-file ceiling for bundling. It was 8 MB, which left the instruments'
# evaluated CSVs - the actual scientific products, 10-12 MB each - sitting
# outside the project file. They compress well, and the total below is the real
# backstop.
BUNDLED_FILE_BYTE_LIMIT = 64 * 1024 * 1024
BUNDLED_TOTAL_BYTE_LIMIT = 512 * 1024 * 1024
INSTRUMENT_IDS = (
    "noseboom",
    "miro",
    "picarro",
    "opc_hbx4",
    "opc_hbx5",
    "partector",
    "ins_gimbal",
    "sif",
    "micasense",
    "flir",
    "gopro",
)
# Only what is actually written to. "metadata" and "thumbnails" were created
# empty on every run, and "project" is gone because the .ccflux now sits at the
# top of the Output Folder rather than three levels inside it.
OUTPUT_DIRECTORIES = (
    "quicklooks",
    "reports",
    "logs",
)


class RawFileState(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    MISSING = "missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class RawFileFingerprint:
    path: Path
    size_bytes: int
    modification_time_ns: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RawFileChange:
    path: Path
    state: RawFileState
    reason: str


@dataclass(slots=True)
class InstrumentProjectState:
    instrument_id: str
    selected_source_files: list[Path] = field(default_factory=list)
    selected_source_folders: list[Path] = field(default_factory=list)
    detection_confidence: float | None = None
    ambiguous_candidates: list[Path] = field(default_factory=list)
    original_start_time: datetime | None = None
    original_end_time: datetime | None = None
    utc_start_time: datetime | None = None
    utc_end_time: datetime | None = None
    analysis_start_time: datetime | None = None
    analysis_end_time: datetime | None = None
    time_offset_seconds: float = 0.0
    timestamp_warnings: list[str] = field(default_factory=list)
    # What the source files cover one by one. Saved so that a reopened project
    # still knows where the recording stopped and started again, instead of
    # offering a gap as available and failing once processing reaches it.
    coverage_segments: list[tuple[datetime, datetime]] = field(default_factory=list)
    processing_priority: int = 1
    enabled: bool = True
    output_locations: list[Path] = field(default_factory=list)

    def validate(self) -> None:
        if self.instrument_id not in INSTRUMENT_IDS:
            raise ProjectValidationError(
                f"Unknown instrument ID: {self.instrument_id}"
            )
        if self.detection_confidence is not None and not (
            0.0 <= self.detection_confidence <= 1.0
        ):
            raise ProjectValidationError(
                f"{self.instrument_id}: detection confidence must be between 0 and 1"
            )
        if self.processing_priority < 1:
            raise ProjectValidationError(
                f"{self.instrument_id}: processing priority must be positive"
            )
        _validate_range(
            self.original_start_time,
            self.original_end_time,
            f"{self.instrument_id} original",
        )
        _validate_range(
            self.utc_start_time, self.utc_end_time, f"{self.instrument_id} UTC"
        )
        _validate_range(
            self.analysis_start_time,
            self.analysis_end_time,
            f"{self.instrument_id} analysis",
        )


@dataclass(slots=True)
class FlightProject:
    flight_id: str
    flight_folder_path: Path
    output_folder_path: Path
    camera_folder_path: Path | None = None
    detected_instruments: dict[str, InstrumentProjectState] = field(default_factory=dict)
    original_start_time: datetime | None = None
    original_end_time: datetime | None = None
    utc_start_time: datetime | None = None
    utc_end_time: datetime | None = None
    selected_analysis_start: datetime | None = None
    selected_analysis_end: datetime | None = None
    display_timezone: str = "UTC"
    cpu_allocation: int = 1
    ram_allocation_bytes: int = 0
    processing_priority: list[str] = field(default_factory=list)
    enabled_instruments: list[str] = field(default_factory=list)
    completed_jobs: list[str] = field(default_factory=list)
    failed_jobs: list[str] = field(default_factory=list)
    cancelled_jobs: list[str] = field(default_factory=list)
    instrument_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    output_locations: dict[str, Path] = field(default_factory=dict)
    software_version: str = "0.0.0"
    configuration_version: str = "1"
    raw_file_fingerprints: dict[str, RawFileFingerprint] = field(default_factory=dict)
    project_id: UUID = field(default_factory=uuid4)
    created_at_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    schema_version: int = PROJECT_SCHEMA_VERSION

    @property
    def flight_output_root(self) -> Path:
        return self.output_folder_path / self.flight_id

    @property
    def project_file(self) -> Path:
        """The one file to keep, at the top of the Output Folder.

        It used to sit at <output>/<flight>/project/, so the Output Folder
        showed a flight folder holding seven folders of intermediates with the
        thing you actually want buried three levels inside one of them.
        """
        return self.output_folder_path / project_filename_for(self.flight_id)

    @property
    def superseded_project_files(self) -> tuple[Path, ...]:
        """Earlier fixed-name files for this same flight.

        Saving removes them once the flight-named file is written. Leaving them
        would put two projects for one flight in the folder, and only one of
        them would be current.
        """
        current = self.project_file
        legacy_folder = self.flight_output_root / "project"
        candidates = (
            legacy_folder / project_filename_for(self.flight_id),
            legacy_folder / PROJECT_FILENAME,
            legacy_folder / LEGACY_PROJECT_FILENAME,
            self.output_folder_path / PROJECT_FILENAME,
        )
        return tuple(
            candidate for candidate in candidates
            if candidate != current and candidate.exists()
        )

    def validate(self, *, require_raw_folder: bool = True) -> None:
        if not self.flight_id.strip():
            raise ProjectValidationError("flight_id cannot be blank")
        if any(character in self.flight_id for character in ("/", "\\", "\0")):
            raise ProjectValidationError("flight_id cannot contain path separators")
        if self.schema_version != PROJECT_SCHEMA_VERSION:
            raise ProjectValidationError(
                f"Unsupported project schema version: {self.schema_version}"
            )
        try:
            ZoneInfo(self.display_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ProjectValidationError(
                f"Unknown display timezone: {self.display_timezone}"
            ) from exc
        if require_raw_folder and not self.flight_folder_path.is_dir():
            raise ProjectValidationError(
                f"Raw flight folder does not exist: {self.flight_folder_path}"
            )
        if (
            require_raw_folder
            and self.camera_folder_path is not None
            and not self.camera_folder_path.is_dir()
        ):
            raise ProjectValidationError(
                f"Raw camera folder does not exist: {self.camera_folder_path}"
            )
        raw = self.flight_folder_path.resolve(strict=False)
        output = self.output_folder_path.resolve(strict=False)
        if raw == output or _is_relative_to(output, raw) or _is_relative_to(raw, output):
            raise ProjectValidationError(
                "Output Folder must be independent from the raw Flight Folder"
            )
        if self.cpu_allocation < 0:
            raise ProjectValidationError("CPU allocation cannot be negative")
        if self.ram_allocation_bytes < 0:
            raise ProjectValidationError("RAM allocation cannot be negative")
        _validate_range(
            self.original_start_time, self.original_end_time, "project original"
        )
        _validate_range(self.utc_start_time, self.utc_end_time, "project UTC")
        _validate_range(
            self.selected_analysis_start,
            self.selected_analysis_end,
            "selected analysis",
        )
        for key, state in self.detected_instruments.items():
            if key != state.instrument_id:
                raise ProjectValidationError(
                    f"Instrument mapping key {key!r} does not match "
                    f"{state.instrument_id!r}"
                )
            state.validate()
        for queue_id in self.processing_priority:
            if not isinstance(queue_id, str) or not queue_id.strip():
                raise ProjectValidationError(
                    "Processing priority entries must be non-empty strings"
                )
        for instrument_id in self.enabled_instruments:
            if instrument_id not in INSTRUMENT_IDS:
                raise ProjectValidationError(
                    f"Unknown instrument ID in project settings: {instrument_id}"
                )


@dataclass(frozen=True, slots=True)
class ProjectOpenResult:
    project: FlightProject
    raw_file_changes: tuple[RawFileChange, ...]
    rescan_required: bool
    reused_saved_scan: bool
    restored_products: tuple[Path, ...] = ()

    @property
    def missing_raw_files(self) -> tuple[Path, ...]:
        return tuple(
            item.path
            for item in self.raw_file_changes
            if item.state is RawFileState.MISSING
        )


class FlightProjectStore:
    """Create and persist projects without writing in the raw flight folder."""

    def create_project(
        self,
        *,
        flight_id: str,
        flight_folder_path: Path,
        output_folder_path: Path,
        detected_instruments: Mapping[str, InstrumentProjectState] | None = None,
        cpu_allocation: int = 1,
        ram_allocation_bytes: int = 0,
        software_version: str = "0.0.0",
        configuration_version: str = "1",
        checksum_mode: bool = False,
    ) -> FlightProject:
        project = FlightProject(
            flight_id=flight_id,
            flight_folder_path=Path(flight_folder_path),
            output_folder_path=Path(output_folder_path),
            detected_instruments=dict(detected_instruments or {}),
            cpu_allocation=cpu_allocation,
            ram_allocation_bytes=ram_allocation_bytes,
            software_version=software_version,
            configuration_version=configuration_version,
        )
        project.processing_priority = [
            key
            for key, value in sorted(
                project.detected_instruments.items(),
                key=lambda item: item[1].processing_priority,
            )
        ]
        project.enabled_instruments = [
            key for key, value in project.detected_instruments.items() if value.enabled
        ]
        project.output_locations = {
            name: project.flight_output_root / name
            for name in (*OUTPUT_DIRECTORIES, "processed")
        }
        project.validate()
        if project.flight_output_root.exists():
            raise DuplicateFlightIDError(
                f"Output already exists for Flight ID {flight_id!r}: "
                f"{project.flight_output_root}"
            )
        self._create_output_structure(project)
        project.raw_file_fingerprints = self.capture_raw_file_fingerprints(
            project, checksum_mode=checksum_mode
        )
        return project

    def save_project(
        self,
        project: FlightProject,
        *,
        overwrite: bool = False,
        bundle_products: bool = True,
    ) -> Path:
        """Write the project as a compressed, self-contained ``.ccflux``.

        The archive holds the project manifest plus the generated products that
        the browser pages need, so a single file can be handed to a colleague
        and opened on their machine. Raw campaign data is never copied in.
        """
        project.validate(require_raw_folder=False)
        self._create_output_structure(project)
        destination = project.project_file
        if destination.exists() and not overwrite:
            raise ProjectOverwriteError(
                f"Project file already exists and was not overwritten: {destination}"
            )
        project.updated_at_utc = datetime.now(timezone.utc)
        payload = _project_to_dict(project)
        # Unique per call, not per process: a throttled checkpoint running on a
        # worker thread and an operator pressing Save otherwise raced for the
        # same temporary name, and whichever renamed second failed with
        # FileNotFoundError. Writing an archive widened that window.
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid4().hex}.temporary"
        )
        try:
            with zipfile.ZipFile(
                temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                archive.writestr(
                    PROJECT_MANIFEST_NAME,
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                )
                bundled, skipped = (
                    self._bundle_products(archive, project)
                    if bundle_products
                    else ([], [])
                )
                archive.writestr(
                    BUNDLE_MANIFEST_NAME,
                    json.dumps(
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "flight_id": project.flight_id,
                            "written_at_utc": project.updated_at_utc.isoformat(),
                            "bundled_products": bundled,
                            "skipped_products": skipped,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
            temporary.replace(destination)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProjectFileError(f"Could not save project: {destination}") from exc
        # Only after the new file is safely in place. A failed save must never
        # leave the flight without a project.
        for superseded in project.superseded_project_files:
            try:
                superseded.unlink()
            except OSError:
                pass  # A read-only disk is not a reason to fail a good save.
        return destination

    @staticmethod
    def _bundle_products(
        archive: zipfile.ZipFile, project: FlightProject
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Copy generated products below the flight output root into the archive."""
        root = project.flight_output_root.resolve(strict=False)
        candidates: list[Path] = [
            Path(value) for value in project.output_locations.values()
        ]
        for state in project.detected_instruments.values():
            candidates.extend(Path(value) for value in state.output_locations)
        # Everything the run produced, not only what an adapter remembered to
        # register. Bundling just the registered outputs left nine files behind
        # on Flight_2707 - evaluated CSVs, the FLIR frame index and health
        # table, the SIF position log - so an operator keeping only the .ccflux
        # lost them. The Output Folder is ours; anything in it is a product.
        if root.is_dir():
            candidates.extend(path for path in root.rglob("*") if path.is_file())

        bundled: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        seen: set[Path] = set()
        total = 0
        for candidate in candidates:
            path = Path(candidate).resolve(strict=False)
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                relative = path.relative_to(root)
            except ValueError:
                # Products written outside the flight output root are left as
                # references; copying them would break the read-only contract.
                skipped.append({"path": str(path), "reason": "outside output root"})
                continue
            if _is_camera_image_product(relative):
                # Camera imagery never travels in the project: the rule for the
                # campaign is that a .ccflux carries the processed results a
                # colleague needs to see the plots and maps, and for GoPro the
                # image identifiers only. The pictures stay in the Output Folder
                # beside the project, and the GoPro page reconnects to the media
                # disk when one is available.
                skipped.append(
                    {"path": str(relative), "reason": "camera imagery is not bundled",
                     "size_bytes": path.stat().st_size}
                )
                continue
            size = path.stat().st_size
            allowed = (
                path.suffix.casefold() in ALWAYS_BUNDLED_SUFFIXES
                or size <= BUNDLED_FILE_BYTE_LIMIT
            )
            if not allowed:
                skipped.append(
                    {"path": str(relative), "reason": "larger than the bundle limit",
                     "size_bytes": size}
                )
                continue
            if total + size > BUNDLED_TOTAL_BYTE_LIMIT:
                skipped.append(
                    {"path": str(relative), "reason": "bundle size limit reached",
                     "size_bytes": size}
                )
                continue
            archive.write(path, BUNDLE_PREFIX + relative.as_posix())
            total += size
            bundled.append(
                {"path": str(relative), "size_bytes": size, "role": relative.parts[0]}
            )
        return bundled, skipped

    def load_project(self, project_file: Path) -> FlightProject:
        path = Path(project_file).resolve(strict=False)
        try:
            payload = self.read_manifest(path)
        except (OSError, json.JSONDecodeError, KeyError, zipfile.BadZipFile) as exc:
            raise ProjectFileError(f"Invalid or unreadable project file: {path}") from exc
        try:
            project = _project_from_dict(payload)
            project.validate(require_raw_folder=False)
        except (KeyError, TypeError, ValueError, ProjectValidationError) as exc:
            raise ProjectFileError(f"Invalid project file: {path}: {exc}") from exc
        self.rebase_output_paths(project, path)
        return project

    @staticmethod
    def read_manifest(path: Path) -> Any:
        """Read the manifest from a compressed project, or a legacy plain file."""
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                return json.loads(archive.read(PROJECT_MANIFEST_NAME))
        # Projects written before compression are plain JSON documents.
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def is_compressed(path: Path) -> bool:
        return zipfile.is_zipfile(Path(path))

    @staticmethod
    def bundled_products(project_file: Path) -> tuple[str, ...]:
        """Names of the products carried inside a compressed project."""
        path = Path(project_file)
        if not zipfile.is_zipfile(path):
            return ()
        with zipfile.ZipFile(path) as archive:
            return tuple(
                sorted(
                    name[len(BUNDLE_PREFIX):]
                    for name in archive.namelist()
                    if name.startswith(BUNDLE_PREFIX) and not name.endswith("/")
                )
            )

    @staticmethod
    def extract_products(
        project_file: Path, project: FlightProject, *, overwrite: bool = False
    ) -> tuple[Path, ...]:
        """Restore bundled products beside the project, without clobbering newer ones.

        This is what makes a shared ``.ccflux`` usable: the recipient's output
        tree is empty, so the browser payloads are written out of the archive
        before the dashboard reads them.
        """
        path = Path(project_file)
        if not zipfile.is_zipfile(path):
            return ()
        root = project.flight_output_root
        restored: list[Path] = []
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.startswith(BUNDLE_PREFIX) or name.endswith("/"):
                    continue
                relative = PurePosixPath(name[len(BUNDLE_PREFIX):])
                if relative.is_absolute() or ".." in relative.parts:
                    # Never let an archive entry escape the output root.
                    continue
                target = root.joinpath(*relative.parts)
                if target.exists() and not overwrite:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                restored.append(target)
        return tuple(restored)

    @staticmethod
    def rebase_output_paths(project: FlightProject, project_file: Path) -> bool:
        """Re-anchor saved output paths onto wherever the project now lives.

        A project records absolute paths from the machine that saved it. Opening
        used to require the file to still sit at exactly that path, so any
        project that was moved, copied to another disk, or shared with a
        colleague refused to open. The on-disk layout is self-describing —
        ``<output folder>/<flight id>/project/<file>`` — so the output root is
        derived from the file's own location instead, and every stored output
        path below the old root is re-pointed at the new one.

        Raw input paths are deliberately left untouched: they belong to the
        recipient's own disks, and ``detect_changed_raw_files`` reports them as
        missing so the operator is told to re-select and rescan.

        Returns True when paths were re-anchored.
        """
        if (
            project_file.parent.name == "project"
            and project_file.parent.parent.name == project.flight_id
        ):
            # Opened in place inside a normal output tree.
            flight_root = project_file.parent.parent
        else:
            # A project handed over on its own — a colleague saving the file to
            # their Desktop, say. Adopt the folder it now sits in, so bundled
            # products unpack beside it instead of at a path from another
            # machine that may not exist or be writable.
            flight_root = project_file.parent / project.flight_id
        previous_root = project.flight_output_root.resolve(strict=False)
        if previous_root == flight_root.resolve(strict=False):
            return False

        def rebase(value: Path) -> Path:
            try:
                relative = Path(value).resolve(strict=False).relative_to(previous_root)
            except ValueError:
                return Path(value)
            return flight_root / relative

        project.output_folder_path = flight_root.parent
        project.output_locations = {
            key: rebase(value) for key, value in project.output_locations.items()
        }
        for state in project.detected_instruments.values():
            state.output_locations = [
                rebase(value) for value in state.output_locations
            ]
        return True

    def validate_project(
        self, project: FlightProject, *, require_raw_folder: bool = True
    ) -> None:
        project.validate(require_raw_folder=require_raw_folder)

    def open_project(
        self,
        project_file: Path,
        *,
        force_rescan: bool = False,
        checksum_mode: bool = False,
    ) -> ProjectOpenResult:
        project = self.load_project(project_file)
        # A project received from someone else has an empty output tree; its
        # browser payloads live inside the archive until they are written out.
        restored = self.extract_products(project_file, project)
        changes = self.detect_changed_raw_files(
            project, checksum_mode=checksum_mode
        )
        changed = any(item.state is not RawFileState.UNCHANGED for item in changes)
        return ProjectOpenResult(
            project=project,
            raw_file_changes=changes,
            rescan_required=force_rescan or changed,
            reused_saved_scan=not force_rescan and not changed,
            restored_products=restored,
        )

    def force_rescan(
        self, project_file: Path, *, checksum_mode: bool = False
    ) -> ProjectOpenResult:
        return self.open_project(
            project_file, force_rescan=True, checksum_mode=checksum_mode
        )

    def capture_raw_file_fingerprints(
        self, project: FlightProject, *, checksum_mode: bool = False
    ) -> dict[str, RawFileFingerprint]:
        fingerprints: dict[str, RawFileFingerprint] = {}
        for path in _selected_files(project):
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            fingerprint = RawFileFingerprint(
                path=path,
                size_bytes=stat.st_size,
                modification_time_ns=stat.st_mtime_ns,
                sha256=_sha256(path) if checksum_mode else None,
            )
            fingerprints[str(path)] = fingerprint
        return fingerprints

    def detect_changed_raw_files(
        self, project: FlightProject, *, checksum_mode: bool = False
    ) -> tuple[RawFileChange, ...]:
        changes: list[RawFileChange] = []
        for fingerprint in project.raw_file_fingerprints.values():
            path = fingerprint.path
            if not path.exists():
                changes.append(
                    RawFileChange(path, RawFileState.MISSING, "source file is missing")
                )
                continue
            try:
                stat = path.stat()
                if not path.is_file():
                    changes.append(
                        RawFileChange(
                            path, RawFileState.CHANGED, "source is no longer a file"
                        )
                    )
                    continue
                reasons: list[str] = []
                if stat.st_size != fingerprint.size_bytes:
                    reasons.append("file size changed")
                if stat.st_mtime_ns != fingerprint.modification_time_ns:
                    reasons.append("modification time changed")
                if checksum_mode:
                    current_checksum = _sha256(path)
                    if fingerprint.sha256 is None:
                        reasons.append("saved fingerprint has no checksum")
                    elif current_checksum != fingerprint.sha256:
                        reasons.append("SHA-256 checksum changed")
                state = RawFileState.CHANGED if reasons else RawFileState.UNCHANGED
                changes.append(
                    RawFileChange(
                        path,
                        state,
                        "; ".join(reasons) if reasons else "size and modification time match",
                    )
                )
            except OSError as exc:
                changes.append(
                    RawFileChange(path, RawFileState.UNREADABLE, str(exc))
                )
        return tuple(changes)

    @staticmethod
    def _create_output_structure(project: FlightProject) -> None:
        root = project.flight_output_root
        for name in OUTPUT_DIRECTORIES:
            (root / name).mkdir(parents=True, exist_ok=True)
        processed = root / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        for instrument_id in INSTRUMENT_IDS:
            (processed / instrument_id).mkdir(exist_ok=True)


def _selected_files(project: FlightProject) -> Iterable[Path]:
    seen: set[Path] = set()
    for state in project.detected_instruments.values():
        for path in state.selected_source_files:
            normalized = Path(path)
            if normalized not in seen:
                seen.add(normalized)
                yield normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _project_to_dict(project: FlightProject) -> dict[str, Any]:
    return _json_value(asdict(project))


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _project_from_dict(data: Any) -> FlightProject:
    if not isinstance(data, dict):
        raise TypeError("project root must be a JSON object")
    instruments_data = data.get("detected_instruments", {})
    if not isinstance(instruments_data, dict):
        raise TypeError("detected_instruments must be an object")
    instruments: dict[str, InstrumentProjectState] = {}
    for key, value in instruments_data.items():
        if not isinstance(value, dict):
            raise TypeError(f"instrument {key} must be an object")
        instruments[key] = InstrumentProjectState(
            instrument_id=value["instrument_id"],
            selected_source_files=[
                Path(item) for item in value.get("selected_source_files", [])
            ],
            selected_source_folders=[
                Path(item) for item in value.get("selected_source_folders", [])
            ],
            detection_confidence=value.get("detection_confidence"),
            ambiguous_candidates=[
                Path(item) for item in value.get("ambiguous_candidates", [])
            ],
            original_start_time=_datetime(value.get("original_start_time")),
            original_end_time=_datetime(value.get("original_end_time")),
            utc_start_time=_datetime(value.get("utc_start_time")),
            utc_end_time=_datetime(value.get("utc_end_time")),
            analysis_start_time=_datetime(value.get("analysis_start_time")),
            analysis_end_time=_datetime(value.get("analysis_end_time")),
            time_offset_seconds=float(value.get("time_offset_seconds", 0.0)),
            timestamp_warnings=list(value.get("timestamp_warnings", [])),
            coverage_segments=_coverage_segments(value.get("coverage_segments")),
            processing_priority=int(value.get("processing_priority", 1)),
            enabled=bool(value.get("enabled", True)),
            output_locations=[
                Path(item) for item in value.get("output_locations", [])
            ],
        )
    fingerprints_data = data.get("raw_file_fingerprints", {})
    if not isinstance(fingerprints_data, dict):
        raise TypeError("raw_file_fingerprints must be an object")
    fingerprints = {
        key: RawFileFingerprint(
            path=Path(value["path"]),
            size_bytes=int(value["size_bytes"]),
            modification_time_ns=int(value["modification_time_ns"]),
            sha256=value.get("sha256"),
        )
        for key, value in fingerprints_data.items()
    }
    return FlightProject(
        flight_id=data["flight_id"],
        flight_folder_path=Path(data["flight_folder_path"]),
        output_folder_path=Path(data["output_folder_path"]),
        camera_folder_path=(
            Path(data["camera_folder_path"])
            if data.get("camera_folder_path")
            else None
        ),
        detected_instruments=instruments,
        original_start_time=_datetime(data.get("original_start_time")),
        original_end_time=_datetime(data.get("original_end_time")),
        utc_start_time=_datetime(data.get("utc_start_time")),
        utc_end_time=_datetime(data.get("utc_end_time")),
        selected_analysis_start=_datetime(data.get("selected_analysis_start")),
        selected_analysis_end=_datetime(data.get("selected_analysis_end")),
        display_timezone=str(data.get("display_timezone", "UTC")),
        cpu_allocation=int(data.get("cpu_allocation", 1)),
        ram_allocation_bytes=int(data.get("ram_allocation_bytes", 0)),
        processing_priority=list(data.get("processing_priority", [])),
        enabled_instruments=list(data.get("enabled_instruments", [])),
        completed_jobs=list(data.get("completed_jobs", [])),
        failed_jobs=list(data.get("failed_jobs", [])),
        cancelled_jobs=list(data.get("cancelled_jobs", [])),
        instrument_options={
            str(key): dict(value)
            for key, value in data.get("instrument_options", {}).items()
            if isinstance(value, dict)
        },
        output_locations={
            key: Path(value) for key, value in data.get("output_locations", {}).items()
        },
        software_version=str(data.get("software_version", "unknown")),
        configuration_version=str(data.get("configuration_version", "unknown")),
        raw_file_fingerprints=fingerprints,
        project_id=UUID(data["project_id"]),
        created_at_utc=_required_datetime(data.get("created_at_utc"), "created_at_utc"),
        updated_at_utc=_required_datetime(data.get("updated_at_utc"), "updated_at_utc"),
        schema_version=int(data.get("schema_version", -1)),
    )


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("datetime values must be ISO-8601 strings")
    return datetime.fromisoformat(value)


def _coverage_segments(value: Any) -> list[tuple[datetime, datetime]]:
    """Read the per-file coverage a project was saved with.

    Projects written before coverage was recorded simply carry none, and the
    start-to-end envelope stays the only thing known about them.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError("coverage_segments must be a list")
    segments: list[tuple[datetime, datetime]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise TypeError("each coverage segment must be a start and an end")
        first, last = _datetime(item[0]), _datetime(item[1])
        if first is None or last is None:
            continue
        if last < first:
            raise ValueError("a coverage segment cannot end before it starts")
        segments.append((first, last))
    return segments


def _required_datetime(value: Any, name: str) -> datetime:
    parsed = _datetime(value)
    if parsed is None:
        raise ValueError(f"{name} is required")
    return parsed


def _validate_range(
    start: datetime | None, end: datetime | None, label: str
) -> None:
    if start is not None and end is not None and end < start:
        raise ProjectValidationError(f"{label} end cannot precede start")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


# The directories a project writes its products into. A recorded product path
# always passes through one of them, which is what makes it relocatable.
PRODUCT_DIRECTORIES = (*OUTPUT_DIRECTORIES, "processed", "exports")


def relocate_product_path(recorded: object, output_root: Path) -> Path:
    """Point a recorded product path at the copy under *output_root*.

    A project carries its products inside the .ccflux archive, but records where
    they were when they were written. Open a project processed on Windows from a
    Mac, or from a USB stick, and every one of those paths is dead - so nothing
    was restored and every workspace reported that its instrument had not been
    processed, while the products sat correctly extracted beside the project.

    Recorded separators may not be this platform's: 'C:\\Output\\...' parsed here
    is a single meaningless filename, so the text is split on both. A path that
    still resolves is returned untouched, which is every same-machine case.
    """
    if not recorded:
        return Path("")
    original = Path(str(recorded))
    if original.is_file():
        return original

    parts = [part for part in str(recorded).replace("\\", "/").split("/") if part not in ("", ".")]
    root = Path(output_root)
    # The last match wins: a flight folder may itself be called "processed".
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] in PRODUCT_DIRECTORIES:
            candidate = root.joinpath(*parts[index:])
            if candidate.is_file():
                return candidate
            break
    return original


def relocate_output_locations(
    locations: dict[str, Path], output_root: Path
) -> dict[str, Path]:
    """Every recorded product path, resolved against the extracted tree."""
    return {
        key: relocate_product_path(value, output_root)
        for key, value in (locations or {}).items()
    }


def read_bundled_product(project_file: object, recorded: object) -> bytes | None:
    """The bytes of one product, read straight out of the .ccflux.

    Extraction writes products beside the project, which assumes that location
    is writable and still attached. A project opened from read-only media, or
    from a volume that is removed afterwards, therefore restores nothing - and
    every workspace then reports that its instrument was never processed, with
    the products sitting in the archive the whole time.

    Reading from the archive removes that assumption: the file is the source,
    the extracted copy only a convenience.
    """
    if not project_file or not recorded:
        return None
    path = Path(str(project_file))
    if not path.is_file() or not zipfile.is_zipfile(path):
        return None
    parts = [p for p in str(recorded).replace("\\", "/").split("/") if p not in ("", ".")]
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] in PRODUCT_DIRECTORIES:
            name = BUNDLE_PREFIX + "/".join(parts[index:])
            try:
                with zipfile.ZipFile(path) as archive:
                    return archive.read(name)
            except (KeyError, OSError, zipfile.BadZipFile):
                return None
    return None
