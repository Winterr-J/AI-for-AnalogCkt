"""Gold-independent Stage A optimization-eligibility policy."""

from .eligibility import (
    OptimizationEligibilityResult,
    evaluate_optimization_eligibility,
)

__all__ = [
    "OptimizationEligibilityResult",
    "evaluate_optimization_eligibility",
]
