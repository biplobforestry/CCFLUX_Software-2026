"""Streaming, cancellable Flight Folder scanner.

Discovery is configuration-driven and bounded: only directory entries, names,
small file headers, bounded metadata, and configured EXIF tags are inspected.
No scientific processor is imported or executed.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import Event
from typing import Callable

from .noseboom_columns import normalize_column_name
from .detection_configuration import (
    DetectionConfiguration,
    DetectionRule,
    FilePatternSet,
)
from .exceptions import DetectionError

LOGGER = logging.getLogger(__name__)

DEFAULT_HEADER_BYTES = 64 * 1024
DEFAULT_SAMPLE_ROWS = 5
DEFAULT_CANDIDATE_FILE_SAMPLES = 20
# Cameras keep a bounded sample rather than every file; their adapters expand
# the delivery lazily when they process it.
BOUNDED_SAMPLE_INSTRUMENTS = frozenset({"micasense", "flir", "gopro"})

# Directory names this software writes its own products into. Discovery never
# descends into them: a flight folder that also holds an output tree would
# otherwise offer every product back as raw input.
PRODUCT_DIRECTORY_NAMES = frozenset(
    {"processed", "quicklooks", "reports", "exports"}
)
DEFAULT_DIAGNOSTIC_SAMPLES = 50
DEFAULT_PROGRESS_INTERVAL_SECONDS = 0.10
DEFAULT_EAGER_PROGRESS_FILES = 5


def _top_level_rank(name: str, priority: tuple[str, ...]) -> int:
    """Where a top-level entry sits in the requested order.

    Matched by substring so a delivery folder named MicaSense_Zeppelin still
    ranks as micasense. Anything unnamed sorts after everything named.
    """
    folded = name.casefold()
    for index, wanted in enumerate(priority):
        if wanted in folded:
            return index
    return len(priority)



@dataclass(frozen=True, slots=True)
class ScanEntry:
    path: Path
    size_bytes: int
    is_file: bool


@dataclass(frozen=True, slots=True)
class ScanIndex:
    root: Path
    entries: tuple[ScanEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class ScanProgress:
    current_folder: Path | None
    current_file: Path | None
    files_scanned: int
    progress: float | None
    detected_candidate_instruments: tuple[str, ...]
    phase: str
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class InstrumentCandidate:
    instrument_id: str
    candidate_path: Path
    matched_rules: tuple[str, ...]
    confidence_score: float
    matching_file_count: int
    sample_matching_files: tuple[Path, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    ambiguous: bool = False
    matching_files: tuple[Path, ...] = ()

    @property
    def all_matching_files(self) -> tuple[Path, ...]:
        """All retained sources, falling back to bounded discovery samples."""
        return self.matching_files or self.sample_matching_files


@dataclass(frozen=True, slots=True)
class ScanReport:
    root: Path
    candidates: tuple[InstrumentCandidate, ...]
    files_scanned: int
    folders_scanned: int
    inaccessible_path_count: int
    malformed_file_count: int
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    cancelled: bool

    @property
    def detected_instrument_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.instrument_id for item in self.candidates))


ProgressCallback = Callable[[ScanProgress], None]


class ScanCancellationToken:
    """Thread-safe cooperative cancellation token."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(slots=True)
class _FileInspection:
    text: str | None = None
    columns: frozenset[str] = frozenset()
    metadata_keys: frozenset[str] = frozenset()
    exif_tags: frozenset[str] = frozenset()
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    malformed: bool = False


@dataclass(slots=True)
class _CandidateAccumulator:
    instrument_id: str
    candidate_path: Path
    matched_rules: set[str] = field(default_factory=set)
    confidence_score: float = 0.0
    matching_file_count: int = 0
    sample_matching_files: list[Path] = field(default_factory=list)
    # Every camera delivery's path, so the sample can span the set instead of
    # its first arrivals. Paths are small; a long flight holds a few thousand.
    bounded_names: list[Path] = field(default_factory=list)
    matching_files: list[Path] = field(default_factory=list)
    warnings: set[str] = field(default_factory=set)
    errors: set[str] = field(default_factory=set)
    omitted_warning_count: int = 0
    omitted_error_count: int = 0

    def bounded_sample(self) -> tuple[Path, ...]:
        """The sample, extended at both ends for a bounded camera delivery.

        A camera names its captures in acquisition order, so the earliest and
        latest names are the earliest and latest acquisitions. Taking both ends
        makes coverage span the delivery; taking whichever arrived first makes
        it span the start, which is what happened.
        """
        sample = list(self.sample_matching_files)
        if self.bounded_names:
            ordered = sorted(
                dict.fromkeys(self.bounded_names),
                key=lambda path: (path.name, str(path)),
            )
            edge = max(1, DEFAULT_CANDIDATE_FILE_SAMPLES // 2)
            sample += ordered[:edge] + ordered[-edge:]
        unique = list(dict.fromkeys(sample))
        return tuple(sorted(unique, key=lambda path: (path.name, str(path))))

    def add(
        self,
        file_path: Path,
        matched_rules: set[str],
        confidence: float,
        warnings: list[str],
        errors: list[str],
        sample_limit: int,
    ) -> None:
        self.matched_rules.update(matched_rules)
        self.confidence_score = max(self.confidence_score, confidence)
        self.matching_file_count += 1
        if len(self.sample_matching_files) < sample_limit:
            self.sample_matching_files.append(file_path)
        if self.instrument_id in BOUNDED_SAMPLE_INSTRUMENTS:
            # A camera's coverage is read from this sample, so the sample has to
            # bound the set. MicaSense delivered 2 371 captures spanning 11:21 to
            # 15:51 and was reported as ending at 11:28, which put every later
            # capture outside the Time Filter. Discovery is threaded, so arrival
            # order says nothing about capture order - the names are kept and the
            # extremes taken from them at the end.
            self.bounded_names.append(file_path)
        # Scientific datasets need every segment for coverage and processing.
        # Camera datasets remain bounded and are expanded lazily by their adapters.
        if self.instrument_id not in BOUNDED_SAMPLE_INSTRUMENTS:
            self.matching_files.append(file_path)
        for warning in warnings:
            if warning in self.warnings:
                continue
            if len(self.warnings) < DEFAULT_DIAGNOSTIC_SAMPLES:
                self.warnings.add(warning)
            else:
                self.omitted_warning_count += 1
        for error in errors:
            if error in self.errors:
                continue
            if len(self.errors) < DEFAULT_DIAGNOSTIC_SAMPLES:
                self.errors.add(error)
            else:
                self.omitted_error_count += 1


class FlightFolderScanner:
    """Recursively scan one flight root without loading full datasets."""

    def __init__(
        self,
        configuration: DetectionConfiguration,
        *,
        header_bytes: int = DEFAULT_HEADER_BYTES,
        sample_rows: int = DEFAULT_SAMPLE_ROWS,
        candidate_file_sample_limit: int = DEFAULT_CANDIDATE_FILE_SAMPLES,
        progress_interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        if header_bytes < 1024:
            raise ValueError("header_bytes must be at least 1024")
        if sample_rows < 1:
            raise ValueError("sample_rows must be positive")
        if candidate_file_sample_limit < 1:
            raise ValueError("candidate_file_sample_limit must be positive")
        if progress_interval_seconds < 0:
            raise ValueError("progress_interval_seconds cannot be negative")
        self.configuration = configuration
        self.header_bytes = header_bytes
        self.sample_rows = sample_rows
        self.candidate_file_sample_limit = candidate_file_sample_limit
        self.progress_interval_seconds = progress_interval_seconds

    def scan(
        self,
        root: Path,
        *,
        cancellation: ScanCancellationToken | None = None,
        progress_callback: ProgressCallback | None = None,
        top_level_order: tuple[str, ...] = (),
    ) -> ScanReport:
        """Discover instruments below ``root``.

        ``top_level_order`` names the top-level entries to visit first, in that
        order. Traversal is otherwise driven by the filesystem, which for the
        camera folder meant MicaSense was read before GoPro and the scan window
        reported them in an order nobody chose. Deeper folders are unaffected.
        """
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise DetectionError(f"Flight folder does not exist or is not a directory: {root}")
        # Per scan, so a second scan of a changed folder re-samples it.
        self._exif_samples: dict[Path, tuple[int, frozenset[str]]] = {}

        token = cancellation or ScanCancellationToken()
        files_scanned = 0
        folders_scanned = 0
        inaccessible = 0
        malformed = 0
        global_warnings: list[str] = []
        global_errors: list[str] = []
        accumulators: dict[tuple[str, Path], _CandidateAccumulator] = {}
        detected_ids: set[str] = set()
        total_files = self._count_files(
            root, token=token, progress_callback=progress_callback
        )
        stack = [root]
        last_file_progress_emit = 0.0
        # The stack is LIFO, so the requested order is pushed reversed.
        priority = tuple(name.casefold() for name in top_level_order)

        self._emit(
            progress_callback,
            ScanProgress(
                root, None, 0, 0.0 if total_files else 100.0, (),
                "starting", token.is_cancelled,
            ),
        )

        while stack and not token.is_cancelled:
            folder = stack.pop()
            folders_scanned += 1
            self._emit(
                progress_callback,
                ScanProgress(
                    folder,
                    None,
                    files_scanned,
                    min(99.9, files_scanned * 100.0 / total_files)
                    if total_files
                    else 100.0,
                    tuple(sorted(detected_ids)),
                    "scanning_folder",
                ),
            )
            pushed_from_here = len(stack)
            try:
                with os.scandir(folder) as iterator:
                    for entry in iterator:
                        if token.is_cancelled:
                            break
                        path = Path(entry.path)
                        try:
                            if entry.is_symlink():
                                global_warnings.append(
                                    f"Skipped symbolic link during discovery: {path}"
                                )
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.casefold() in PRODUCT_DIRECTORY_NAMES:
                                    # An output tree left inside the flight
                                    # folder is this software's own work. Read
                                    # as input it becomes a rival candidate for
                                    # the instrument that produced it, which is
                                    # how a Noseboom export came to be offered
                                    # as a Noseboom source and failed the job.
                                    global_warnings.append(
                                        f"Skipped a previous output folder during "
                                        f"discovery: {path}"
                                    )
                                    continue
                                stack.append(path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError as exc:
                            inaccessible += 1
                            global_errors.append(f"Cannot inspect path {path}: {exc}")
                            continue

                        outcome = self._match_file(path, root)
                        files_scanned += 1
                        if outcome.inaccessible:
                            inaccessible += 1
                        if outcome.malformed:
                            malformed += 1
                        for message in outcome.global_errors:
                            global_errors.append(message)
                        for match in outcome.matches:
                            key = (match.instrument_id, match.candidate_path)
                            accumulator = accumulators.setdefault(
                                key,
                                _CandidateAccumulator(
                                    match.instrument_id, match.candidate_path
                                ),
                            )
                            accumulator.add(
                                path,
                                match.matched_rules,
                                match.confidence,
                                match.warnings,
                                match.errors,
                                self.candidate_file_sample_limit,
                            )
                            detected_ids.add(match.instrument_id)
                        now = time.monotonic()
                        if (
                            files_scanned <= DEFAULT_EAGER_PROGRESS_FILES
                            or files_scanned == total_files
                            or now - last_file_progress_emit
                            >= self.progress_interval_seconds
                        ):
                            self._emit(
                                progress_callback,
                                ScanProgress(
                                    folder,
                                    path,
                                    files_scanned,
                                    min(99.9, files_scanned * 100.0 / total_files)
                                    if total_files
                                    else 100.0,
                                    tuple(sorted(detected_ids)),
                                    "scanning_file",
                                ),
                            )
                            last_file_progress_emit = now
            except OSError as exc:
                inaccessible += 1
                global_errors.append(f"Cannot scan folder {folder}: {exc}")
            if priority and folder == root and len(stack) > pushed_from_here:
                # Reorder only the root's own children. The stack is LIFO, so
                # the entry to be visited first has to end up last.
                children = stack[pushed_from_here:]
                children.sort(
                    key=lambda item: (
                        _top_level_rank(item.name, priority), item.name.casefold()
                    ),
                    reverse=True,
                )
                stack[pushed_from_here:] = children

        candidates = self._finalize_candidates(accumulators)
        phase = "cancelled" if token.is_cancelled else "complete"
        self._emit(
            progress_callback,
            ScanProgress(
                None,
                None,
                files_scanned,
                None if token.is_cancelled else 100.0,
                tuple(sorted(detected_ids)),
                phase,
                token.is_cancelled,
            ),
        )
        return ScanReport(
            root=root,
            candidates=candidates,
            files_scanned=files_scanned,
            folders_scanned=folders_scanned,
            inaccessible_path_count=inaccessible,
            malformed_file_count=malformed,
            warnings=tuple(dict.fromkeys(global_warnings)),
            errors=tuple(dict.fromkeys(global_errors)),
            cancelled=token.is_cancelled,
        )

    def _count_files(
        self,
        root: Path,
        *,
        token: ScanCancellationToken,
        progress_callback: ProgressCallback | None,
    ) -> int:
        """Count entries without retaining paths so progress stays memory-bounded."""
        total = 0
        stack = [root]
        last_inventory_emit = 0.0
        first_folder = True
        while stack and not token.is_cancelled:
            folder = stack.pop()
            now = time.monotonic()
            if (
                first_folder
                or now - last_inventory_emit >= self.progress_interval_seconds
            ):
                self._emit(
                    progress_callback,
                    ScanProgress(folder, None, 0, 0.0, (), "inventory"),
                )
                last_inventory_emit = now
                first_folder = False
            try:
                with os.scandir(folder) as iterator:
                    for entry in iterator:
                        if token.is_cancelled:
                            break
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                total += 1
                        except OSError:
                            continue
            except OSError:
                continue
        return total

    def _match_file(self, path: Path, root: Path) -> "_MatchOutcome":
        matches: list[_RuleMatch] = []
        global_errors: list[str] = []
        inspection: _FileInspection | None = None
        inaccessible = False
        malformed = False

        applicable: list[tuple[DetectionRule, FilePatternSet, _NameEvidence]] = []
        for rule in self.configuration.rules:
            patterns = self.configuration.pattern_sets[rule.pattern_set]
            name_evidence = _name_evidence(path, root, patterns)
            if name_evidence.excluded or not name_evidence.extension:
                continue
            if not (
                name_evidence.folder
                or name_evidence.prefix
                or name_evidence.suffix
                or patterns.header_text
                or patterns.required_csv_columns
                or patterns.metadata_keys
                or patterns.camera_exif_tags
            ):
                continue
            applicable.append((rule, patterns, name_evidence))

        if not applicable:
            return _MatchOutcome((), False, False, ())

        text_capable = path.suffix.casefold() in {".csv", ".txt", ".dat", ".json"}
        exif_capable = path.suffix.casefold() in {
            ".jpg", ".jpeg", ".tif", ".tiff", ".png"
        }
        needs_text = text_capable and any(
            patterns.required_csv_columns
            or patterns.optional_csv_columns
            or patterns.header_text
            or patterns.metadata_keys
            for _, patterns, _ in applicable
        )
        needs_exif = exif_capable and any(
            patterns.camera_exif_tags for _, patterns, _ in applicable
        )
        sampled_exif: frozenset[str] | None = None
        if needs_exif:
            seen, established = self._exif_samples.get(path.parent, (0, frozenset()))
            if seen >= EXIF_SAMPLES_PER_FOLDER:
                # The folder's tag names are known; reuse them rather than
                # paying another file open for an answer already held.
                needs_exif = False
                sampled_exif = established
        if needs_text or needs_exif:
            inspection = _inspect_file(
                path,
                self.header_bytes,
                self.sample_rows,
                inspect_text=needs_text,
                inspect_exif=needs_exif,
            )
            inaccessible = any(
                error.startswith("Cannot read") for error in inspection.errors
            )
            malformed = inspection.malformed
            global_errors.extend(inspection.errors)
        else:
            inspection = _FileInspection()
        if sampled_exif is not None:
            inspection.exif_tags = sampled_exif
        elif needs_exif:
            seen, established = self._exif_samples.get(path.parent, (0, frozenset()))
            self._exif_samples[path.parent] = (
                seen + 1, established | inspection.exif_tags
            )

        for rule, patterns, evidence in applicable:
            match = _evaluate_rule(path, rule, patterns, evidence, inspection)
            if match is not None:
                matches.append(match)
        return _MatchOutcome(
            tuple(matches), inaccessible, malformed, tuple(global_errors)
        )

    def _finalize_candidates(
        self,
        accumulators: dict[tuple[str, Path], _CandidateAccumulator],
    ) -> tuple[InstrumentCandidate, ...]:
        counts: dict[str, int] = {}
        for instrument_id, _ in accumulators:
            counts[instrument_id] = counts.get(instrument_id, 0) + 1

        result: list[InstrumentCandidate] = []
        for accumulator in accumulators.values():
            ambiguous = counts[accumulator.instrument_id] > 1
            warnings = set(accumulator.warnings)
            if accumulator.omitted_warning_count:
                warnings.add(
                    f"{accumulator.omitted_warning_count} additional per-file "
                    "warning(s) were omitted from the dashboard report."
                )
            errors = set(accumulator.errors)
            if accumulator.omitted_error_count:
                errors.add(
                    f"{accumulator.omitted_error_count} additional per-file "
                    "error(s) were omitted from the dashboard report."
                )
            if ambiguous:
                warnings.add(
                    f"Multiple candidate paths matched {accumulator.instrument_id}; "
                    "user confirmation is required."
                )
            result.append(
                InstrumentCandidate(
                    instrument_id=accumulator.instrument_id,
                    candidate_path=accumulator.candidate_path,
                    matched_rules=tuple(sorted(accumulator.matched_rules)),
                    confidence_score=round(accumulator.confidence_score, 3),
                    matching_file_count=accumulator.matching_file_count,
                    sample_matching_files=accumulator.bounded_sample(),
                    warnings=tuple(sorted(warnings)),
                    errors=tuple(sorted(errors)),
                    ambiguous=ambiguous,
                    matching_files=tuple(accumulator.matching_files),
                )
            )
        return tuple(
            sorted(result, key=lambda item: (item.instrument_id, str(item.candidate_path)))
        )

    @staticmethod
    def _emit(
        callback: ProgressCallback | None, progress: ScanProgress
    ) -> None:
        if callback is None:
            return
        try:
            callback(progress)
        except Exception:
            LOGGER.exception("Flight Folder scan progress callback failed")


@dataclass(frozen=True, slots=True)
class _NameEvidence:
    folder: tuple[str, ...]
    candidate_path: Path
    prefix: tuple[str, ...]
    suffix: tuple[str, ...]
    extension: str | None
    excluded: bool


@dataclass(frozen=True, slots=True)
class _RuleMatch:
    instrument_id: str
    candidate_path: Path
    matched_rules: set[str]
    confidence: float
    warnings: list[str]
    errors: list[str]


@dataclass(frozen=True, slots=True)
class _MatchOutcome:
    matches: tuple[_RuleMatch, ...]
    inaccessible: bool
    malformed: bool
    global_errors: tuple[str, ...]


@lru_cache(maxsize=8192)
def _folder_matches(
    folder: Path, root: Path, likely_folder_names: tuple[str, ...]
) -> tuple[tuple[Path, str], ...]:
    """Which configured folder names this file's parent chain matches.

    Every file in a folder gives the same answer, and a GoPro delivery holds
    thousands per folder, so walking the chain per file was the scan's second
    cost after disk reads - 1.1 million fnmatch calls for 4,188 files. Folder
    names do not change while a scan runs, so the answer is cached.
    """
    matches: list[tuple[Path, str]] = []
    current = folder
    while True:
        for value in likely_folder_names:
            if fnmatch.fnmatch(current.name.casefold(), value.casefold()):
                matches.append((current, value))
        if current == root or root not in current.parents:
            break
        current = current.parent
    return tuple(matches)


def _name_evidence(
    path: Path, root: Path, patterns: FilePatternSet
) -> _NameEvidence:
    name_lower = path.name.casefold()
    extension = path.suffix.casefold()
    extension_match = extension if extension in patterns.file_extensions else None
    prefixes = tuple(
        value
        for value in patterns.filename_prefixes
        if name_lower.startswith(value.casefold())
    )
    suffixes = tuple(
        value
        for value in patterns.filename_suffixes
        if name_lower.endswith(value.casefold())
    )
    excluded = any(
        fnmatch.fnmatch(name_lower, value.casefold())
        or any(fnmatch.fnmatch(part.casefold(), value.casefold()) for part in path.parts)
        for value in patterns.exclusion_patterns
    )

    folder_matches = _folder_matches(path.parent, root, patterns.likely_folder_names)
    candidate_path = folder_matches[0][0] if folder_matches else path.parent
    return _NameEvidence(
        folder=tuple(value for _, value in folder_matches),
        candidate_path=candidate_path,
        prefix=prefixes,
        suffix=suffixes,
        extension=extension_match,
        excluded=excluded,
    )


def _inspect_file(
    path: Path,
    header_bytes: int,
    sample_rows: int,
    *,
    inspect_text: bool,
    inspect_exif: bool,
) -> _FileInspection:
    result = _FileInspection()
    if inspect_text:
        try:
            data, truncated = _read_bounded(path, header_bytes)
            text = data.decode("utf-8-sig", errors="replace")
            result.text = text
            if "\x00" in text:
                result.warnings.append(f"Binary/NUL content in header: {path}")
            columns, parse_warning, malformed = _header_columns(
                text, sample_rows, truncated
            )
            # Detection must see the same column names as everything else. A
            # Noseboom export written with the logger prefix carries
            # "NoseBoom_Airflow_UTCcorr_Nanoseconds_ns", and the rule requires
            # "Airflow_UTCcorr_Nanoseconds_ns", so a perfectly good file was not
            # recognised as Noseboom at all. Both spellings are kept: the raw
            # name still matches anything that expects it.
            result.columns = frozenset(columns) | frozenset(
                normalize_column_name(value) for value in columns
            )
            result.malformed = malformed
            if parse_warning:
                result.warnings.append(f"{path}: {parse_warning}")
        except (OSError, PermissionError) as exc:
            result.errors.append(f"Cannot read file header {path}: {exc}")
            return result

    if inspect_exif:
        tags, warning = _configured_exif_tags(path)
        result.exif_tags = frozenset(tags)
        if warning:
            result.warnings.append(warning)
    return result


def _read_bounded(path: Path, limit: int) -> tuple[bytes, bool]:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    return data[:limit], len(data) > limit


def _header_columns(
    text: str, sample_rows: int, truncated: bool
) -> tuple[set[str], str | None, bool]:
    if not text:
        return set(), "empty header", True
    lines = text.splitlines()[: sample_rows + 1]
    if not lines:
        return set(), "empty header", True
    header = lines[0]
    try:
        delimiter = _delimiter(header)
        if delimiter is None:
            columns = [value.strip().strip('"') for value in header.split()]
        else:
            reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter, strict=True)
            rows = list(reader)
            columns = [value.strip() for value in rows[0]]
        columns = [value for value in columns if value]
        warning = "bounded header was truncated" if truncated and not columns else None
        return set(columns), warning, False
    except (csv.Error, IndexError) as exc:
        return set(), f"malformed delimited header: {exc}", True


def _delimiter(header: str) -> str | None:
    counts = {value: header.count(value) for value in (",", ";", "\t")}
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count else None


# Enough for the JPEG APP1 segment. Measured against the campaign's GoPro
# frames: 64 KB reproduces the full-file tag set on every one of them, 32 KB on
# none, because the camera embeds a thumbnail alongside the metadata.
EXIF_HEADER_BYTES = 64 * 1024
# How many images per folder are opened for EXIF before the folder's tag names
# are taken as established. Detection only uses the tag *names*, and every frame
# a camera writes into one folder carries the same ones - verified across the
# campaign's four GoPro folders, 36 frames probed in each, one distinct tag set
# per folder. Opening all 3,651 cost 11 of the camera scan's 18 seconds, and the
# disk tops out near 400 files/s however many threads ask.
EXIF_SAMPLES_PER_FOLDER = 24


def _configured_exif_tags(path: Path) -> tuple[set[str], str | None]:
    """Read EXIF metadata only; Pillow does not decode the pixel array here.

    Handing Pillow the path makes it read the file in many small chunks - about
    29 reads per image - and over 3,651 GoPro frames on an external disk that
    was 13 of the camera scan's 18 seconds. One bounded sequential read into
    memory covers the JPEG APP1 segment and is roughly ten times faster for the
    same tags.

    A TIFF may place its image file directory anywhere, including past the end
    of that window, so anything the buffered attempt cannot answer falls back to
    reading the file itself.
    """
    try:
        from PIL import ExifTags, Image
    except ImportError:
        return set(), f"EXIF inspection unavailable for {path}: Pillow is not installed"

    def _tags(source) -> set[str]:
        with Image.open(source) as image:
            return {
                str(ExifTags.TAGS.get(tag_id, tag_id))
                for tag_id in image.getexif().keys()
            }

    try:
        with path.open("rb") as handle:
            head = handle.read(EXIF_HEADER_BYTES)
    except OSError as exc:
        return set(), f"Cannot inspect EXIF metadata for {path}: {exc}"
    if head:
        try:
            tags = _tags(io.BytesIO(head))
            if tags:
                return tags, None
        except (OSError, ValueError, SyntaxError):
            pass        # truncated window or a format that needs the whole file
    try:
        return _tags(path), None
    except (OSError, ValueError) as exc:
        return set(), f"Cannot inspect EXIF metadata for {path}: {exc}"


def _evaluate_rule(
    path: Path,
    rule: DetectionRule,
    patterns: FilePatternSet,
    evidence: _NameEvidence,
    inspection: _FileInspection,
) -> _RuleMatch | None:
    matched: set[str] = {f"extension:{evidence.extension}"}
    score = 0.10
    if evidence.folder:
        matched.update(f"folder:{value}" for value in evidence.folder)
        score += (
            0.25
            if any(not _is_generic_folder_pattern(value) for value in evidence.folder)
            else 0.05
        )
    if evidence.prefix:
        matched.update(f"filename_prefix:{value}" for value in evidence.prefix)
        score += 0.25
    if evidence.suffix:
        matched.update(f"filename_suffix:{value}" for value in evidence.suffix)
        score += 0.05

    required = set(patterns.required_csv_columns)
    required_match = bool(required) and required.issubset(inspection.columns)
    if required_match:
        matched.add(f"required_csv_columns:{len(required)}/{len(required)}")
        score += 0.35
    elif required:
        matched_count = len(required.intersection(inspection.columns))
        matched.add(f"required_csv_columns:{matched_count}/{len(required)}")

    optional_count = len(set(patterns.optional_csv_columns) & inspection.columns)
    if optional_count:
        matched.add(f"optional_csv_columns:{optional_count}")
        score += min(0.10, optional_count * 0.01)

    text = inspection.text or ""
    header_matches = tuple(value for value in patterns.header_text if value in text)
    if header_matches:
        matched.update(f"header_text:{value}" for value in header_matches)
        score += 0.25

    metadata_matches = _metadata_matches(text, path, patterns.metadata_keys)
    if metadata_matches:
        matched.update(f"metadata_key:{value}" for value in metadata_matches)
        score += 0.20

    exif_matches = set(patterns.camera_exif_tags) & inspection.exif_tags
    if exif_matches:
        matched.update(f"exif_tag:{value}" for value in exif_matches)
        score += 0.25

    # A generic container plus a generic timestamp column is not enough to
    # identify an instrument. This prevents every HATCH-BOX CSV containing
    # ``_time`` from being counted as Partector data. A rich confirmed schema
    # can still identify an intentionally renamed instrument file.
    only_generic_folders = not any(
        not _is_generic_folder_pattern(value) for value in evidence.folder
    )
    if (
        patterns.filename_prefixes
        and not evidence.prefix
        and only_generic_folders
        and len(required) <= 1
        and not header_matches
        and not metadata_matches
        and not exif_matches
    ):
        return None

    generic_suffixes = {
        value.casefold() for value in evidence.suffix
    } <= set(patterns.file_extensions)
    strong_identity = bool(
        evidence.prefix
        or required_match
        or header_matches
        or metadata_matches
        or exif_matches
        or (evidence.suffix and not generic_suffixes)
    )
    folder_extension = bool(
        evidence.extension
        and any(
            not _is_generic_folder_pattern(value) for value in evidence.folder
        )
    )
    if not (strong_identity or folder_extension):
        return None

    warnings = list(inspection.warnings)
    errors = list(inspection.errors)
    if required and not required_match:
        errors.append(
            f"{path}: missing required columns: "
            + ", ".join(sorted(required - inspection.columns))
        )
    if rule.requires_confirmation:
        warnings.append(
            f"Detection rule requires confirmation: {'; '.join(rule.todo)}"
        )
    if (
        not patterns.camera_exif_tags
        and not patterns.metadata_keys
        and rule.instrument_id in {"micasense", "flir", "gopro"}
    ):
        warnings.append("No confirmed camera EXIF tags are configured.")
    return _RuleMatch(
        instrument_id=rule.instrument_id,
        candidate_path=(
            _sif_candidate_path(path, evidence.candidate_path)
            if rule.instrument_id == "sif"
            else evidence.candidate_path
        ),
        matched_rules=matched,
        confidence=min(score, 1.0),
        warnings=warnings,
        errors=errors,
    )


def _sif_candidate_path(path: Path, fallback: Path) -> Path:
    """Combine complementary AirFLOX FULL and FLOX/FLUO files as one dataset."""
    for parent in path.parents:
        if parent.name.casefold().startswith("floxinside"):
            return parent
    return fallback


def _is_generic_folder_pattern(value: str) -> bool:
    """Date/glob containers provide context but not instrument identity."""
    lowered = value.casefold()
    return (
        any(character in value for character in "*?[")
        or lowered.startswith("2026")
        or lowered in {"hatch-box", "influxdb"}
    )


def _metadata_matches(
    text: str, path: Path, configured_keys: tuple[str, ...]
) -> set[str]:
    if not configured_keys:
        return set()
    matches = {value for value in configured_keys if value in text}
    if path.suffix.casefold() != ".json" or not text:
        return matches
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return matches
    if isinstance(value, dict):
        matches.update(key for key in configured_keys if key in value)
    return matches
