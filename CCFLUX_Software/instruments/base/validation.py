"""Validation report primitives."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationMessage:
    code: str
    message: str
    fatal: bool = False
