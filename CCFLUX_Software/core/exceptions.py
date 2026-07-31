"""Typed application errors; scientific errors remain owned by adapters."""


class CCFluxError(Exception):
    """Base exception for the new application."""


class ConfigurationError(CCFluxError):
    pass


class DetectionError(CCFluxError):
    pass


class ValidationError(CCFluxError):
    pass


class AmbiguousInputError(ValidationError):
    pass


class TimeInterpretationError(ValidationError):
    pass


class InstrumentNotIntegratedError(CCFluxError):
    pass


class ProcessingError(CCFluxError):
    pass


class ProcessingCancelledError(ProcessingError):
    pass


class ResourceLimitError(ProcessingError):
    pass


class ExportError(CCFluxError):
    pass


class ProjectError(CCFluxError):
    """Base error for flight-project creation and persistence."""


class ProjectValidationError(ProjectError, ValidationError):
    pass


class ProjectFileError(ProjectError):
    pass


class DuplicateFlightIDError(ProjectError):
    pass


class ProjectOverwriteError(ProjectError):
    pass
