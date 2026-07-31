"""Application-agnostic orchestration models and services."""

from .enums import DetectionStatus, ProcessingStatus
from .instrument_registry import (
    InstrumentRegistration,
    InstrumentRegistry,
    PhysicalGroup,
)
from .models import InstrumentResult
from .processing_manager import (
    JobOutcome,
    ProcessingContext,
    ProcessingJob,
    ProcessingPriorityQueue,
    ProcessingScheduler,
    WorkerGroup,
    worker_group_capacities,
)
from .resource_manager import (
    BoundedThumbnailCache,
    CameraBatchPolicy,
    ResourceLimits,
    ResourceManager,
    SystemResources,
    detect_total_ram_bytes,
    iter_batches,
    iter_memory_bounded_batches,
    release_large_temporary,
)
from .dashboard_time import (
    DashboardTimeState,
    InstrumentTimeSelection,
    parse_dashboard_datetime,
)
from .logging_manager import (
    GUIProcessingLogState,
    LogLevel,
    LogRecord,
    ProcessingLogManager,
    WorkerError,
)
from .flight_project import (
    FlightProject,
    FlightProjectStore,
    InstrumentProjectState,
    ProjectOpenResult,
    RawFileChange,
    RawFileFingerprint,
    RawFileState,
)
from .time_extraction import TimestampExtractor
from .time_manager import (
    GlobalTimeRangeResult,
    TimeRangeResult,
    TimestampQualityFlag,
    TimestampQualitySample,
)

__all__ = [
    "DetectionStatus",
    "InstrumentRegistration",
    "InstrumentRegistry",
    "InstrumentResult",
    "PhysicalGroup",
    "ProcessingStatus",
    "JobOutcome",
    "ProcessingContext",
    "ProcessingJob",
    "ProcessingPriorityQueue",
    "ProcessingScheduler",
    "WorkerGroup",
    "worker_group_capacities",
    "BoundedThumbnailCache",
    "CameraBatchPolicy",
    "ResourceLimits",
    "ResourceManager",
    "SystemResources",
    "detect_total_ram_bytes",
    "iter_batches",
    "iter_memory_bounded_batches",
    "release_large_temporary",
    "DashboardTimeState",
    "InstrumentTimeSelection",
    "parse_dashboard_datetime",
    "GUIProcessingLogState",
    "LogLevel",
    "LogRecord",
    "ProcessingLogManager",
    "WorkerError",
    "FlightProject",
    "FlightProjectStore",
    "InstrumentProjectState",
    "ProjectOpenResult",
    "RawFileChange",
    "RawFileFingerprint",
    "RawFileState",
    "GlobalTimeRangeResult",
    "TimeRangeResult",
    "TimestampExtractor",
    "TimestampQualityFlag",
    "TimestampQualitySample",
]
