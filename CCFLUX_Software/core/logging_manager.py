"""Structured processing logs with independent persistent and GUI views."""

from __future__ import annotations

import json
from collections import deque
import threading
import traceback as traceback_module
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from .compat import StrEnum
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .exceptions import ProjectOverwriteError


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    SUCCESS = "SUCCESS"


@dataclass(frozen=True, slots=True)
class LogRecord:
    severity: LogLevel
    component: str
    message: str
    instrument: str | None = None
    job_id: str | None = None
    exception_type: str | None = None
    traceback: str | None = None
    file_path: Path | None = None
    processing_step: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.component.strip():
            raise ValueError("log component cannot be blank")
        if not self.message.strip():
            raise ValueError("log message cannot be blank")
        if self.timestamp.tzinfo is None:
            raise ValueError("log timestamp must be timezone-aware")

    @property
    def user_message(self) -> str:
        """Short card-safe text; detailed traceback stays in diagnostics."""
        return self.message

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["severity"] = self.severity.value
        value["timestamp"] = self.timestamp.isoformat()
        value["file_path"] = str(self.file_path) if self.file_path else None
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "LogRecord":
        return cls(
            timestamp=datetime.fromisoformat(str(value["timestamp"])),
            severity=LogLevel(str(value["severity"])),
            component=str(value["component"]),
            instrument=_optional_string(value.get("instrument")),
            job_id=_optional_string(value.get("job_id")),
            message=str(value["message"]),
            exception_type=_optional_string(value.get("exception_type")),
            traceback=_optional_string(value.get("traceback")),
            file_path=Path(str(value["file_path"])) if value.get("file_path") else None,
            processing_step=_optional_string(value.get("processing_step")),
        )


@dataclass(frozen=True, slots=True)
class WorkerError:
    component: str
    message: str
    instrument: str | None = None
    job_id: str | None = None
    exception_type: str | None = None
    traceback: str | None = None
    file_path: str | None = None
    processing_step: str | None = None
    critical: bool = False

    @classmethod
    def from_exception(
        cls,
        exception: BaseException,
        *,
        component: str,
        message: str,
        instrument: str | None = None,
        job_id: str | None = None,
        file_path: Path | None = None,
        processing_step: str | None = None,
        critical: bool = False,
    ) -> "WorkerError":
        return cls(
            component=component,
            message=message,
            instrument=instrument,
            job_id=job_id,
            exception_type=type(exception).__name__,
            traceback="".join(
                traceback_module.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            ),
            file_path=str(file_path) if file_path else None,
            processing_step=processing_step,
            critical=critical,
        )


@dataclass(slots=True)
class GUIProcessingLogState:
    auto_scroll: bool = True
    collapsed: bool = False

    def pause_auto_scroll(self) -> None:
        self.auto_scroll = False

    def resume_auto_scroll(self) -> None:
        self.auto_scroll = True

    def collapse(self) -> None:
        self.collapsed = True

    def expand(self) -> None:
        self.collapsed = False


LogCallback = Callable[[LogRecord], None]


class ProcessingLogManager:
    """Thread-safe diagnostics log for main and worker processing events."""

    # The in-memory views are what the GUI reads; the persistent JSONL on disk
    # is the complete record and is never trimmed. A campaign day with camera
    # processing produces a lot of entries, and both lists grew without bound -
    # the oldest are the least useful on screen, and they are still on disk.
    IN_MEMORY_RECORD_LIMIT = 20_000

    def __init__(
        self, persistent_log_file: Path, *, memory_limit: int | None = None
    ) -> None:
        self.persistent_log_file = Path(persistent_log_file)
        self.persistent_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.gui_state = GUIProcessingLogState()
        limit = self.IN_MEMORY_RECORD_LIMIT if memory_limit is None else memory_limit
        self.memory_limit = int(limit)
        self._records: deque[LogRecord] = deque(maxlen=self.memory_limit)
        self._visible_records: deque[LogRecord] = deque(maxlen=self.memory_limit)
        self._callbacks: list[LogCallback] = []
        self._lock = threading.RLock()

    def subscribe(self, callback: LogCallback) -> Callable[[], None]:
        with self._lock:
            self._callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return unsubscribe

    def log(
        self,
        severity: LogLevel,
        component: str,
        message: str,
        *,
        instrument: str | None = None,
        job_id: str | None = None,
        exception_type: str | None = None,
        traceback: str | None = None,
        file_path: Path | None = None,
        processing_step: str | None = None,
        timestamp: datetime | None = None,
    ) -> LogRecord:
        record = LogRecord(
            timestamp=timestamp or datetime.now(timezone.utc),
            severity=severity,
            component=component,
            instrument=instrument,
            job_id=job_id,
            message=message,
            exception_type=exception_type,
            traceback=traceback,
            file_path=Path(file_path) if file_path else None,
            processing_step=processing_step,
        )
        encoded = json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
        with self._lock:
            try:
                with self.persistent_log_file.open("a", encoding="utf-8") as stream:
                    stream.write(encoded)
                    stream.flush()
            except OSError as exc:
                raise OSError(
                    f"Could not write persistent diagnostics log: "
                    f"{self.persistent_log_file}"
                ) from exc
            self._records.append(record)
            self._visible_records.append(record)
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(record)
            except Exception:
                # A GUI callback cannot be allowed to break processing or logging.
                continue
        return record

    def capture_exception(
        self,
        component: str,
        message: str,
        exception: BaseException,
        *,
        severity: LogLevel = LogLevel.ERROR,
        instrument: str | None = None,
        job_id: str | None = None,
        file_path: Path | None = None,
        processing_step: str | None = None,
    ) -> LogRecord:
        return self.log(
            severity,
            component,
            message,
            instrument=instrument,
            job_id=job_id,
            exception_type=type(exception).__name__,
            traceback="".join(
                traceback_module.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            ),
            file_path=file_path,
            processing_step=processing_step,
        )

    def forward_worker_error(
        self, error: WorkerError | Mapping[str, object]
    ) -> LogRecord:
        worker = error if isinstance(error, WorkerError) else WorkerError(
            component=str(error["component"]),
            message=str(error["message"]),
            instrument=_optional_string(error.get("instrument")),
            job_id=_optional_string(error.get("job_id")),
            exception_type=_optional_string(error.get("exception_type")),
            traceback=_optional_string(error.get("traceback")),
            file_path=_optional_string(error.get("file_path")),
            processing_step=_optional_string(error.get("processing_step")),
            critical=bool(error.get("critical", False)),
        )
        return self.log(
            LogLevel.CRITICAL if worker.critical else LogLevel.ERROR,
            worker.component,
            worker.message,
            instrument=worker.instrument,
            job_id=worker.job_id,
            exception_type=worker.exception_type,
            traceback=worker.traceback,
            file_path=Path(worker.file_path) if worker.file_path else None,
            processing_step=worker.processing_step,
        )

    def file_read_error(
        self, exception: BaseException, path: Path, **context: object
    ) -> LogRecord:
        return self.capture_exception(
            "file-reader",
            f"Could not read {path.name}",
            exception,
            file_path=path,
            **context,
        )

    def parser_error(
        self, exception: BaseException, path: Path, **context: object
    ) -> LogRecord:
        return self.capture_exception(
            "parser",
            f"Could not parse {path.name}",
            exception,
            file_path=path,
            **context,
        )

    def invalid_timestamp(
        self,
        message: str,
        *,
        instrument: str,
        file_path: Path | None = None,
        job_id: str | None = None,
    ) -> LogRecord:
        return self.log(
            LogLevel.WARNING,
            "timestamp-parser",
            message,
            instrument=instrument,
            job_id=job_id,
            file_path=file_path,
            processing_step="timestamp-validation",
        )

    def missing_columns(
        self,
        columns: Iterable[str],
        *,
        instrument: str,
        file_path: Path,
        job_id: str | None = None,
    ) -> LogRecord:
        return self.log(
            LogLevel.ERROR,
            "parser",
            "Missing required columns: " + ", ".join(columns),
            instrument=instrument,
            job_id=job_id,
            file_path=file_path,
            processing_step="column-validation",
        )

    def corrupted_file(
        self,
        path: Path,
        *,
        instrument: str | None = None,
        job_id: str | None = None,
        detail: str = "File appears to be corrupted",
    ) -> LogRecord:
        return self.log(
            LogLevel.ERROR,
            "file-validator",
            detail,
            instrument=instrument,
            job_id=job_id,
            file_path=path,
            processing_step="file-validation",
        )

    def output_write_failure(
        self,
        exception: BaseException,
        path: Path,
        *,
        instrument: str | None = None,
        job_id: str | None = None,
    ) -> LogRecord:
        return self.capture_exception(
            "output-writer",
            f"Could not write output {path.name}",
            exception,
            instrument=instrument,
            job_id=job_id,
            file_path=path,
            processing_step="output-write",
        )

    def cancelled_job(
        self, job_id: str, *, instrument: str | None = None
    ) -> LogRecord:
        return self.log(
            LogLevel.WARNING,
            "processing",
            "Processing job was cancelled",
            instrument=instrument,
            job_id=job_id,
            processing_step="cancelled",
        )

    def resource_warning(
        self, message: str, *, job_id: str | None = None
    ) -> LogRecord:
        return self.log(
            LogLevel.WARNING, "resource-manager", message, job_id=job_id
        )

    def configuration_error(self, exception: BaseException) -> LogRecord:
        return self.capture_exception(
            "configuration", "Invalid application configuration", exception
        )

    def records(
        self,
        *,
        severities: Iterable[LogLevel] | None = None,
        instrument: str | None = None,
        visible_only: bool = True,
    ) -> tuple[LogRecord, ...]:
        accepted = set(severities) if severities is not None else None
        with self._lock:
            source = tuple(
                self._visible_records if visible_only else self._records
            )
        return tuple(
            record
            for record in source
            if (accepted is None or record.severity in accepted)
            and (instrument is None or record.instrument == instrument)
        )

    def copy_logs(
        self,
        *,
        severities: Iterable[LogLevel] | None = None,
        instrument: str | None = None,
        visible_only: bool = True,
    ) -> str:
        return "\n".join(
            _format_record(record)
            for record in self.records(
                severities=severities,
                instrument=instrument,
                visible_only=visible_only,
            )
        )

    def clear_visible(self) -> None:
        """Clear the GUI view only; persistent and session records are retained."""
        with self._lock:
            self._visible_records.clear()

    def export_logs(
        self,
        destination: Path,
        *,
        overwrite: bool = False,
        visible_only: bool = False,
        severities: Iterable[LogLevel] | None = None,
        instrument: str | None = None,
    ) -> Path:
        destination = Path(destination)
        if destination.exists() and not overwrite:
            raise ProjectOverwriteError(
                f"Log export already exists and was not overwritten: {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Previously this copied the whole persistent log, which is appended to
        # across every run of the application, so a project carried diagnostics
        # from unrelated earlier sessions and flights. Exporting this manager's
        # own records scopes the project log to the session that produced it.
        # The complete history remains in the application's persistent log.
        records = self.records(
            severities=severities,
            instrument=instrument,
            visible_only=visible_only,
        )
        destination.write_text(
            "".join(
                json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        return destination


def _format_record(record: LogRecord) -> str:
    context = [record.component]
    if record.instrument:
        context.append(record.instrument)
    if record.job_id:
        context.append(f"job={record.job_id}")
    line = (
        f"{record.timestamp.isoformat()} [{record.severity.value}] "
        f"[{'/'.join(context)}] {record.message}"
    )
    if record.traceback:
        line += "\n" + record.traceback.rstrip()
    return line


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)
