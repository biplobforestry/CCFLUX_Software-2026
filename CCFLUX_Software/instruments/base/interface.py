"""Abstract interface implemented by future instrument adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.detector import InputCandidate
from core.models import (
    FigureArtifact,
    InstrumentDescriptor,
    InstrumentResult,
    Metadata,
    OutputFile,
    ProgressUpdate,
)
from core.scanner import ScanIndex
from core.time_manager import TimeRange

ProgressCallback = Callable[[ProgressUpdate], None]


class InstrumentBase(ABC):
    """Behavioral boundary between the application and instrument code."""

    descriptor: InstrumentDescriptor

    @abstractmethod
    def detect(self, scan_index: ScanIndex) -> Sequence[InputCandidate]:
        """Return candidates only; detection must not imply validation."""

    @abstractmethod
    def inspect_metadata(self, candidate: InputCandidate) -> Metadata:
        """Read bounded metadata without performing scientific processing."""

    @abstractmethod
    def extract_time_range(self, candidate: InputCandidate) -> TimeRange:
        """Report native and justified UTC ranges without silent correction."""

    @abstractmethod
    def validate(self, candidate: InputCandidate) -> InstrumentResult:
        """Validate required files/schema and return factual status."""

    @abstractmethod
    def load(self, candidate: InputCandidate) -> Any:
        """Load an adapter-owned data handle for later processing."""

    @abstractmethod
    def process_quicklook(
        self, loaded: Any, options: Mapping[str, Any]
    ) -> InstrumentResult:
        """Run the validated quicklook path."""

    @abstractmethod
    def process_detailed(
        self, loaded: Any, options: Mapping[str, Any]
    ) -> InstrumentResult:
        """Run validated detailed processing when supported."""

    @abstractmethod
    def create_plots(
        self, result: InstrumentResult, output_directory: Path
    ) -> Sequence[FigureArtifact]:
        """Create plots from a completed result."""

    @abstractmethod
    def export_results(
        self,
        result: InstrumentResult,
        output_directory: Path,
        formats: Sequence[str],
    ) -> Sequence[OutputFile]:
        """Export result artifacts to an assigned output directory."""

    @abstractmethod
    def cancel(self) -> None:
        """Request cooperative cancellation at an adapter-safe boundary."""

    @abstractmethod
    def report_progress(self, callback: ProgressCallback | None) -> None:
        """Register or invoke the progress reporting boundary."""
