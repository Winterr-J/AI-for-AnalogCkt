"""Explicit failures raised by automatic circuit onboarding."""

from ActiveLearning.errors import ReferenceStimulusUnavailableError


class AutoConfigError(RuntimeError):
    """Base error for Stage A/B to Active Learning conversion failures."""


class BindingValidationError(AutoConfigError):
    """A Stage A parameter binding cannot be executed safely."""


class SemanticMappingError(AutoConfigError):
    """A circuit-type simulation role cannot be resolved unambiguously."""
