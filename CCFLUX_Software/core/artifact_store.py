"""Artifact destination model; publishing behavior is deferred."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ArtifactLocation:
    project_root: Path
    instrument_id: str

    @property
    def instrument_root(self) -> Path:
        return self.project_root / "instruments" / self.instrument_id
