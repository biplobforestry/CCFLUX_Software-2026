"""Detection-domain models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InputCandidate:
    instrument_id: str
    paths: tuple[Path, ...]
    confidence: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
