"""Serializable worker message models."""

from dataclasses import dataclass
from .compat import StrEnum
from typing import Any


class WorkerMessageType(StrEnum):
    STARTED = "started"
    PROGRESS = "progress"
    RESULT = "result"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WorkerMessage:
    message_type: WorkerMessageType
    job_id: str
    payload: dict[str, Any]
