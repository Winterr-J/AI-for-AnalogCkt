"""Type-independent Stage A scope rules.

This module intentionally does not infer semantic roles from parameter names or
circuit types.  It only recognizes explicit IR representations of testbench and
simulation-control quantities that are outside the frozen Stage A v1 scope.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ir.circuit_ir import PhysicalTargetIR


@dataclass(frozen=True)
class ScopeDecision:
    validity_status: str
    validity_rule_id: str
    evidence: tuple[str, ...]


_WAVEFORM_PARAMETER_KINDS = frozenset(
    {
        "ac_amplitude",
        "ac_frequency",
        "sin_amplitude",
        "sin_frequency",
        "pulse_width",
        "pulse_period",
        "rise_time",
        "fall_time",
        "clock_timing",
        "clock_frequency",
        "waveform_parameter",
    }
)

_SIMULATION_CONTROL_KINDS = frozenset(
    {
        "tran_timestep",
        "tran_stop_time",
        "ac_points",
        "sweep_start",
        "sweep_stop",
        "sweep_points",
        "simulation_control",
    }
)


def classify_scope(target: PhysicalTargetIR) -> ScopeDecision | None:
    """Return an explicit out-of-scope decision, or ``None``.

    The Milestone 1 parser does not currently create these records.  Supporting
    them here makes the boundary deterministic for synthetic tests and future IR
    producers without expanding the parser's source canonical template.
    """

    kind = target.parameter_kind.strip().lower()
    if kind in _WAVEFORM_PARAMETER_KINDS:
        return ScopeDecision(
            validity_status="OUT_OF_SCOPE",
            validity_rule_id="OUT_OF_SCOPE_TESTBENCH_WAVEFORM",
            evidence=(f"explicit testbench waveform parameter kind: {kind}",),
        )
    if kind in _SIMULATION_CONTROL_KINDS:
        return ScopeDecision(
            validity_status="OUT_OF_SCOPE",
            validity_rule_id="OUT_OF_SCOPE_SIMULATION_CONTROL",
            evidence=(f"explicit simulation-control parameter kind: {kind}",),
        )
    return None
