"""Typed loading and validation for configurable instrument detection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .exceptions import ConfigurationError
from .instrument_registry import DEFAULT_INSTRUMENTS

LOGGER = logging.getLogger(__name__)

_PATTERN_FIELDS = (
    "likely_folder_names",
    "filename_prefixes",
    "filename_suffixes",
    "file_extensions",
    "required_csv_columns",
    "optional_csv_columns",
    "metadata_keys",
    "camera_exif_tags",
    "header_text",
    "exclusion_patterns",
)
_RULE_FIELDS = (
    "instrument_id",
    "display_name",
    "pattern_set",
    "requires_confirmation",
    "todo",
)


@dataclass(frozen=True, slots=True)
class FilePatternSet:
    pattern_id: str
    likely_folder_names: tuple[str, ...]
    filename_prefixes: tuple[str, ...]
    filename_suffixes: tuple[str, ...]
    file_extensions: tuple[str, ...]
    required_csv_columns: tuple[str, ...]
    optional_csv_columns: tuple[str, ...]
    metadata_keys: tuple[str, ...]
    camera_exif_tags: tuple[str, ...]
    header_text: tuple[str, ...]
    exclusion_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DetectionRule:
    instrument_id: str
    display_name: str
    pattern_set: str
    requires_confirmation: bool
    todo: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DetectionConfiguration:
    schema_version: int
    rules: tuple[DetectionRule, ...]
    pattern_sets: Mapping[str, FilePatternSet]

    def rule_for(self, instrument_id: str) -> DetectionRule:
        for rule in self.rules:
            if rule.instrument_id == instrument_id:
                return rule
        raise KeyError(f"No detection rule for instrument_id: {instrument_id}")

    def patterns_for(self, instrument_id: str) -> FilePatternSet:
        rule = self.rule_for(instrument_id)
        return self.pattern_sets[rule.pattern_set]


def load_detection_configuration(
    rules_path: Path,
    patterns_path: Path,
    *,
    valid_instrument_ids: frozenset[str] | None = None,
) -> DetectionConfiguration:
    """Load and validate the two detection configuration documents."""
    # Local import avoids a circular import through core.configuration exports.
    from .configuration import load_configuration

    rules_document = load_configuration(rules_path)
    patterns_document = load_configuration(patterns_path)
    rules_version = _schema_version(rules_document, rules_path)
    patterns_version = _schema_version(patterns_document, patterns_path)
    if rules_version != patterns_version:
        raise ConfigurationError(
            "Detection and file-pattern schema_version values must match"
        )

    allowed_ids = valid_instrument_ids or frozenset(
        item.instrument_id for item in DEFAULT_INSTRUMENTS
    )
    patterns = _parse_pattern_sets(patterns_document, patterns_path)
    rules = _parse_rules(rules_document, rules_path, allowed_ids, patterns)

    configured_ids = {rule.instrument_id for rule in rules}
    missing_ids = sorted(allowed_ids - configured_ids)
    if missing_ids:
        raise ConfigurationError(
            "Missing detection rules for instrument IDs: " + ", ".join(missing_ids)
        )

    for rule in rules:
        if rule.requires_confirmation:
            # A standing property of the configuration, not something that went
            # wrong during this scan. It was logged at WARNING on every load -
            # twice per scan, once per root - which read like a fault each time.
            # The operator still sees it where it matters: the rule attaches the
            # same note to every matching file, so the instrument card carries it.
            LOGGER.info(
                "Detection rule for %s is marked as requiring confirmation: %s",
                rule.instrument_id,
                "; ".join(rule.todo),
            )

    return DetectionConfiguration(
        schema_version=rules_version,
        rules=rules,
        pattern_sets=MappingProxyType(patterns),
    )


def _schema_version(document: Mapping[str, Any], path: Path) -> int:
    if "schema_version" not in document:
        raise ConfigurationError(f"{path}: missing required field schema_version")
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ConfigurationError(
            f"{path}: schema_version must be a positive integer"
        )
    return version


def _parse_pattern_sets(
    document: Mapping[str, Any], path: Path
) -> dict[str, FilePatternSet]:
    raw_sets = document.get("pattern_sets")
    if not isinstance(raw_sets, list):
        raise ConfigurationError(f"{path}: pattern_sets must be a list")

    result: dict[str, FilePatternSet] = {}
    required_fields = {"pattern_id", *_PATTERN_FIELDS}
    for index, value in enumerate(raw_sets):
        location = f"{path}: pattern_sets[{index}]"
        mapping = _mapping(value, location)
        _require_fields(mapping, required_fields, location)
        pattern_id = _nonempty_string(mapping["pattern_id"], f"{location}.pattern_id")
        if pattern_id in result:
            raise ConfigurationError(f"Duplicate detection pattern: {pattern_id}")

        list_values = {
            name: _string_tuple(mapping[name], f"{location}.{name}")
            for name in _PATTERN_FIELDS
        }
        for extension in list_values["file_extensions"]:
            if not extension.startswith(".") or extension != extension.lower():
                raise ConfigurationError(
                    f"{location}.file_extensions: extensions must be lowercase "
                    "and begin with '.'"
                )
        result[pattern_id] = FilePatternSet(
            pattern_id=pattern_id,
            **list_values,
        )
    return result


def _parse_rules(
    document: Mapping[str, Any],
    path: Path,
    allowed_ids: frozenset[str],
    patterns: Mapping[str, FilePatternSet],
) -> tuple[DetectionRule, ...]:
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list):
        raise ConfigurationError(f"{path}: rules must be a list")

    result: list[DetectionRule] = []
    seen: set[str] = set()
    for index, value in enumerate(raw_rules):
        location = f"{path}: rules[{index}]"
        mapping = _mapping(value, location)
        _require_fields(mapping, set(_RULE_FIELDS), location)
        instrument_id = _nonempty_string(
            mapping["instrument_id"], f"{location}.instrument_id"
        )
        if instrument_id not in allowed_ids:
            raise ConfigurationError(
                f"{location}: invalid instrument_id '{instrument_id}'"
            )
        if instrument_id in seen:
            raise ConfigurationError(
                f"Duplicate detection rule for instrument_id: {instrument_id}"
            )
        seen.add(instrument_id)

        pattern_set = _nonempty_string(
            mapping["pattern_set"], f"{location}.pattern_set"
        )
        if pattern_set not in patterns:
            raise ConfigurationError(
                f"{location}: unknown pattern_set '{pattern_set}'"
            )
        confirmation = mapping["requires_confirmation"]
        if not isinstance(confirmation, bool):
            raise ConfigurationError(
                f"{location}.requires_confirmation must be boolean"
            )
        todo = _string_tuple(mapping["todo"], f"{location}.todo")
        if confirmation and not todo:
            raise ConfigurationError(
                f"{location}: incomplete rule requires at least one TODO"
            )
        if not confirmation and todo:
            raise ConfigurationError(
                f"{location}: TODO entries require requires_confirmation=true"
            )

        result.append(
            DetectionRule(
                instrument_id=instrument_id,
                display_name=_nonempty_string(
                    mapping["display_name"], f"{location}.display_name"
                ),
                pattern_set=pattern_set,
                requires_confirmation=confirmation,
                todo=todo,
            )
        )
    return tuple(result)


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{location} must be a mapping")
    return value


def _require_fields(
    mapping: Mapping[str, Any], required: set[str], location: str
) -> None:
    missing = sorted(required - set(mapping))
    if missing:
        raise ConfigurationError(
            f"{location}: missing required fields: {', '.join(missing)}"
        )


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{location} must be a non-empty string")
    return value


def _string_tuple(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{location} must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _nonempty_string(item, f"{location}[{index}]")
        if text in seen:
            raise ConfigurationError(f"{location}: duplicate value '{text}'")
        seen.add(text)
        result.append(text)
    return tuple(result)
