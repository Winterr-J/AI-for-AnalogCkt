"""Technology-specific range provider contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.models import (
    CircuitContext,
    OperatingContext,
    RangeDecision,
    SearchVariable,
    TargetRangeRequest,
    TechnologyContext,
)


class RangeProvider(ABC):
    """Construct target-local bounds without changing generic topology facts."""

    @abstractmethod
    def construct_target_range(
        self,
        target: TargetRangeRequest,
        circuit_context: CircuitContext,
        operating_context: OperatingContext,
        technology_context: TechnologyContext,
    ) -> RangeDecision:
        raise NotImplementedError

    def construct_range(
        self,
        variable: SearchVariable,
        circuit_context: CircuitContext,
        operating_context: OperatingContext,
        technology_context: TechnologyContext,
    ) -> RangeDecision:
        """Compatibility wrapper for a root considered as one physical target.

        Full Stage B construction uses ``construct_target_range`` for every
        member and dependent target before propagation/intersection.
        """

        request = TargetRangeRequest(
            parameter_id=variable.members[0] if variable.members else variable.variable_id,
            parameter_name=variable.variable_id,
            device_id=None,
            device_type=(
                "mos"
                if (variable.parameter_kind or "").strip().lower()
                in {"width", "w", "length", "l"}
                else None
            ),
            parameter_kind=variable.parameter_kind or "unknown",
            semantic_category=variable.semantic_category,
            domain_type=variable.domain_type,
            unit=variable.unit,
            nominal_value=variable.nominal_value,
        )
        return self.construct_target_range(
            request,
            circuit_context,
            operating_context,
            technology_context,
        )
