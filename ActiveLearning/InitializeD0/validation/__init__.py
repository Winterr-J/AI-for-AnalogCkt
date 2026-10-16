"""Validation utilities for Active Learning D0 initialization."""

from .current_mirror_functional_validator import check_current_mirror_functionality
from .excitation_perturbation_policy import (
    ExcitationPerturbationPolicy,
    resolve_excitation_perturbation_policy,
)
from .parameter_validator import validate_parameters
from .spec_validator import check_meets_spec

__all__ = [
    "check_current_mirror_functionality",
    "check_meets_spec",
    "check_opamp_functionality",
    "ExcitationPerturbationPolicy",
    "OpAmpFunctionalValidator",
    "resolve_excitation_perturbation_policy",
    "validate_parameters",
]


def __getattr__(name):
    """Load OpAmp validation only when requested by the OpAmp provider."""

    if name in {"OpAmpFunctionalValidator", "check_opamp_functionality"}:
        from . import opamp_functional_validator

        return getattr(opamp_functional_validator, name)
    raise AttributeError(name)
