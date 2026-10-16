"""Frozen deterministic optimization-eligibility policy for Stage A v1."""

from __future__ import annotations

from dataclasses import dataclass


_DESIGN_SEMANTICS = frozenset({"device_parameter", "bias_parameter"})
_FIXED_REASON = {
    "operating_condition": "FIXED_OPERATING_CONDITION",
    "constraint_or_process": "FIXED_CONSTRAINT_OR_PROCESS",
    "topology_or_discrete": "FIXED_TOPOLOGY_OR_DISCRETE",
}


@dataclass(frozen=True)
class OptimizationEligibilityResult:
    optimization_eligible: bool
    optimization_reason: str


def evaluate_optimization_eligibility(
    *,
    validity_status: str,
    binding_status: str,
    semantic_category: str | None,
    semantic_status: str,
) -> OptimizationEligibilityResult:
    """Combine frozen Stage A decisions without re-inferring any of them."""

    if validity_status != "VALID":
        return OptimizationEligibilityResult(False, "INVALID_OR_OUT_OF_SCOPE")
    if semantic_status == "UNCERTAIN" or semantic_category is None:
        return OptimizationEligibilityResult(False, "SEMANTIC_ROLE_UNCERTAIN")
    if binding_status == "UNCERTAIN":
        return OptimizationEligibilityResult(False, "BINDING_UNCERTAIN")
    if semantic_category in _FIXED_REASON:
        return OptimizationEligibilityResult(False, _FIXED_REASON[semantic_category])
    if semantic_category in _DESIGN_SEMANTICS:
        if binding_status == "BOUND":
            return OptimizationEligibilityResult(
                True, "ELIGIBLE_BOUND_DESIGN_PARAMETER"
            )
        if binding_status == "DECLARED_UNBOUND":
            return OptimizationEligibilityResult(False, "DECLARED_UNBOUND")
        return OptimizationEligibilityResult(False, "NOT_RUNTIME_TUNABLE")
    raise ValueError(f"Unsupported semantic category: {semantic_category!r}")
