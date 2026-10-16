"""Initial Tier-2A source/interface role templates.

This module is frozen for the first Development baseline before Gold error
inspection.  It contains no circuit-ID or parameter-ID lookup.
"""

from __future__ import annotations

from dataclasses import dataclass

from .source_role_features import SourceRoleFeatures


@dataclass(frozen=True)
class Tier2SourceDecision:
    semantic_category: str
    rule_id: str
    confidence: str
    evidence: tuple[str, ...]


_RF_CONTEXTS = frozenset({"LNA", "Mixer", "PA / output stage", "Oscillator / PLL"})
_REFERENCE_CONTEXTS = frozenset(
    {
        "ADC / DAC",
        "Bandgap / voltage reference",
        "Comparator",
        "LDO / regulator",
        "Oscillator / PLL",
        "Power converter",
    }
)
_CONTROL_CONTEXTS = frozenset(
    {
        "ADC / DAC",
        "Comparator",
        "Filter",
        "LDO / regulator",
        "Oscillator / PLL",
        "Power converter",
        "Switched-cap / sampler",
    }
)
_CURRENT_REFERENCE_BIAS_CONTEXTS = frozenset(
    {
        "Amplifier",
        "Bandgap / voltage reference",
        "Current mirror / bias",
        "LNA",
        "Mixer",
        "Opamp / OTA",
        "Oscillator / PLL",
        "PA / output stage",
    }
)


def apply_tier2_source_rule(
    features: SourceRoleFeatures | None,
) -> Tier2SourceDecision | None:
    if features is None or features.binding_status != "BOUND":
        return None

    # Functional external/interface roles precede local gate/base bias shape.
    if (
        features.name_hint == "CLOCK"
        and features.grounded
        and (
            features.active_control_terminal_count > 0
            or features.passive_relay_control_count > 0
            or features.subcircuit_neighbor_count > 0
        )
    ):
        return _decision("operating_condition", "T2_CLOCK_SWITCH_CONTROL", "HIGH", features)

    if (
        features.name_hint in {"RF", "LO"}
        and features.circuit_type in _RF_CONTEXTS
        and features.grounded
        and features.has_interface_path
    ):
        rule = "T2_RF_INTERFACE" if features.name_hint == "RF" else "T2_LO_INTERFACE"
        return _decision("operating_condition", rule, "HIGH", features)

    if (
        features.name_hint in {"INPUT", "COMMON_MODE"}
        and features.grounded
        and features.has_interface_path
    ):
        rule = (
            "T2_COMMON_MODE_INPUT"
            if features.name_hint == "COMMON_MODE"
            else "T2_INPUT_INTERFACE"
        )
        return _decision("operating_condition", rule, "HIGH", features)

    if (
        features.name_hint == "OUTPUT"
        and features.grounded
        and (
            features.active_conduction_terminal_count > 0
            or features.connects_passive_network
            or features.subcircuit_neighbor_count > 0
        )
    ):
        return _decision("operating_condition", "T2_OUTPUT_COMPLIANCE", "HIGH", features)

    if (
        features.name_hint == "REFERENCE"
        and features.source_type == "voltage"
        and features.circuit_type in _REFERENCE_CONTEXTS
        and features.grounded
        and features.has_interface_path
    ):
        return _decision("operating_condition", "T2_FUNCTIONAL_REFERENCE", "MEDIUM", features)

    if (
        features.name_hint == "CONTROL"
        and features.circuit_type in _CONTROL_CONTEXTS
        and features.grounded
        and features.has_interface_path
    ):
        return _decision("operating_condition", "T2_FUNCTIONAL_CONTROL", "MEDIUM", features)

    if (
        features.source_type == "current"
        and features.grounded
        and features.has_active_path
        and (
            features.name_hint == "BIAS"
            or (
                features.name_hint == "REFERENCE"
                and features.circuit_type in _CURRENT_REFERENCE_BIAS_CONTEXTS
            )
        )
    ):
        return _decision("bias_parameter", "T2_INTERNAL_BIAS_CURRENT", "MEDIUM", features)

    if (
        features.source_type == "voltage"
        and features.name_hint == "BIAS"
        and features.grounded
        and features.has_active_path
    ):
        return _decision("bias_parameter", "T2_INTERNAL_BIAS_VOLTAGE", "HIGH", features)

    return None


def _decision(
    semantic: str,
    rule_id: str,
    confidence: str,
    features: SourceRoleFeatures,
) -> Tier2SourceDecision:
    return Tier2SourceDecision(
        semantic_category=semantic,
        rule_id=rule_id,
        confidence=confidence,
        evidence=features.evidence(),
    )
