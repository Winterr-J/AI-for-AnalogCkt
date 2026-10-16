"""Candidate scope and validity decisions for Stage A Milestone 3."""

from .candidate_validator import (
    CandidateValidityResult,
    CircuitValidityResult,
    CandidateValidator,
    validate_candidates,
)

__all__ = [
    "CandidateValidityResult",
    "CandidateValidator",
    "CircuitValidityResult",
    "validate_candidates",
]
