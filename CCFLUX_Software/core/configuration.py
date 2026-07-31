"""Safe configuration loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError


def load_configuration(path: Path) -> dict[str, Any]:
    """Load a mapping from JSON or YAML.

    JSON is accepted directly. JSON-compatible YAML is parsed without an
    optional dependency; general YAML requires PyYAML.
    """
    if not path.is_file():
        raise ConfigurationError(f"Configuration file does not exist: {path}")
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        elif suffix in {".yaml", ".yml"}:
            text = path.read_text(encoding="utf-8")
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                try:
                    import yaml
                except ImportError as exc:
                    raise ConfigurationError(
                        "Non-JSON YAML requires the optional PyYAML dependency"
                    ) from exc
                try:
                    value = yaml.safe_load(text)
                except yaml.YAMLError as exc:
                    raise ConfigurationError(
                        f"Invalid YAML configuration: {path}: {exc}"
                    ) from exc
        else:
            raise ConfigurationError(
                f"Unsupported configuration extension: {path.suffix}"
            )
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Could not load configuration: {path}") from exc

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError("Configuration root must be a mapping")
    return value


from .detection_configuration import (  # noqa: E402
    DetectionConfiguration,
    DetectionRule,
    FilePatternSet,
    load_detection_configuration,
)

__all__ = [
    "DetectionConfiguration",
    "DetectionRule",
    "FilePatternSet",
    "load_configuration",
    "load_detection_configuration",
]
