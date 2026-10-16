"""Initial conservative Tier-2B passive role templates."""

from __future__ import annotations

from dataclasses import dataclass

from .passive_role_features import PassiveRoleFeatures


@dataclass(frozen=True)
class Tier2PassiveDecision:
    semantic_category: str
    rule_id: str
    confidence: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Tier2PassiveEvaluation:
    decision: Tier2PassiveDecision | None
    abstention_reason: str | None = None


_RF_CONTEXTS = frozenset({"LNA", "Mixer", "Oscillator / PLL", "PA / output stage"})
_CLOCKED_CONTEXTS = frozenset({"ADC / DAC", "Comparator", "Switched-cap / sampler"})


def apply_tier2_passive_rule(
    features: PassiveRoleFeatures | None,
) -> Tier2PassiveDecision | None:
    return evaluate_tier2_passive_rule(features).decision


def evaluate_tier2_passive_rule(
    features: PassiveRoleFeatures | None,
) -> Tier2PassiveEvaluation:
    if features is None or features.binding_status != "BOUND":
        return Tier2PassiveEvaluation(None)
    motifs = set(features.motifs)
    roles = set(features.all_source_roles)
    a, b = features.endpoint_a, features.endpoint_b

    # Strong internal motifs take precedence over interface adjacency.
    if (
        features.circuit_type == "Power converter"
        and features.passive_kind in {"L", "C"}
        and "POWER_SWITCHING_PATH" in motifs
        and (
            "RLC_NETWORK" in motifs
            or "BETWEEN_INTERNAL_NODES" in motifs
            or (features.passive_kind == "L" and features.ground_relation == "BOTH_NON_GROUND")
        )
    ):
        return _resolved(
            _decision("device_parameter", "T2P_POWER_STAGE_STORAGE", "HIGH", features)
        )

    if (
        features.circuit_type in _RF_CONTEXTS
        and features.passive_kind in {"R", "C", "L"}
        and (
            "LC_RESONANT_NETWORK" in motifs
            or (
                "RLC_NETWORK" in motifs
                and ("BETWEEN_INTERNAL_NODES" in motifs or bool(roles & {"RF", "LO"}))
            )
            or ("COUPLING_PATH" in motifs and bool(roles & {"RF", "LO"}))
        )
    ):
        if features.source_relation == "DIFFERENT_ROLE_SOURCE_TO_SOURCE":
            return _abstain("DIFFERENT_ROLE_SOURCE_TO_SOURCE")
        if _ground_only_rlc_without_exact_membership(features, motifs):
            return _abstain(
                "GROUND_ONLY_RLC_PROXIMITY_WITHOUT_EXACT_MEMBERSHIP"
            )
        return _resolved(
            _decision("device_parameter", "T2P_INTERNAL_RLC_NETWORK", "HIGH", features)
        )

    if (
        features.passive_kind in {"R", "C"}
        and "REFERENCE_BIAS_NETWORK" in motifs
        and "BIAS_TO_INTERNAL" in motifs
        and any(item.internal_relation for item in (a, b) if not item.grounded)
        and features.ground_relation == "BOTH_NON_GROUND"
    ):
        if features.source_relation == "DIFFERENT_ROLE_SOURCE_TO_SOURCE":
            return _abstain("DIFFERENT_ROLE_SOURCE_TO_SOURCE")
        return _resolved(
            _decision(
                "device_parameter",
                "T2P_INTERNAL_BIAS_REFERENCE_NETWORK",
                "HIGH",
                features,
            )
        )

    if (
        features.passive_kind in {"R", "C"}
        and "FEEDBACK_LIKE_PATH" in motifs
        and "BETWEEN_INTERNAL_NODES" in motifs
        and not roles & {"INPUT", "COMMON_MODE", "OUTPUT"}
    ):
        return _resolved(
            _decision(
                "device_parameter",
                "T2P_INTERNAL_FEEDBACK_COMPENSATION",
                "HIGH",
                features,
            )
        )

    if (
        features.circuit_type in _CLOCKED_CONTEXTS
        and features.passive_kind == "C"
        and "CLOCKED_STORAGE_NETWORK" in motifs
    ):
        return _resolved(
            _decision(
                "device_parameter", "T2P_CLOCKED_INTERNAL_STORAGE", "HIGH", features
            )
        )

    if (
        features.passive_kind == "R"
        and "DEGENERATION_LIKE_PATH" in motifs
        and not roles & {"INPUT", "COMMON_MODE", "OUTPUT", "RF", "LO"}
        and (
            features.ground_relation == "SHUNT_TO_GROUND"
            or features.supply_relation
            or bool(roles & {"BIAS"})
            or "BETWEEN_INTERNAL_NODES" in motifs
        )
    ):
        return _resolved(
            _decision(
                "device_parameter",
                "T2P_INTERNAL_DEGENERATION_BIAS_RESISTOR",
                "HIGH",
                features,
            )
        )

    # External conditions require an explicit runtime source role and a
    # narrow interface-only topology.
    if (
        features.passive_kind == "R"
        and bool(roles & {"INPUT", "COMMON_MODE"})
        and "SOURCE_SERIES" in motifs
        and not motifs & {
            "FEEDBACK_LIKE_PATH",
            "RLC_NETWORK",
            "REFERENCE_BIAS_NETWORK",
            "LC_RESONANT_NETWORK",
        }
        and any(item.active_control_count for item in (a, b))
    ):
        return _resolved(
            _decision(
                "operating_condition",
                "T2P_EXTERNAL_SOURCE_IMPEDANCE",
                "HIGH",
                features,
            )
        )

    if (
        features.passive_kind in {"R", "C"}
        and "OUTPUT" in roles
        and "OUTPUT_SHUNT" in motifs
        and not motifs & {
            "FEEDBACK_LIKE_PATH",
            "RLC_NETWORK",
            "POWER_SWITCHING_PATH",
            "CLOCKED_STORAGE_NETWORK",
        }
    ):
        return _resolved(
            _decision(
                "operating_condition", "T2P_EXTERNAL_OUTPUT_LOAD", "HIGH", features
            )
        )

    if (
        features.passive_kind == "R"
        and bool(roles & {"INPUT", "COMMON_MODE"})
        and "SOURCE_SHUNT" in motifs
        and not motifs & {"RLC_NETWORK", "REFERENCE_BIAS_NETWORK"}
    ):
        if (
            features.source_relation == "SOURCE_SHUNT_TO_GROUND"
            and features.source_shunt_has_conduction_continuation
        ):
            return _abstain("SOURCE_SHUNT_ACTIVE_CONDUCTION")
        return _resolved(
            _decision(
                "operating_condition",
                "T2P_EXTERNAL_INTERFACE_ENVIRONMENT",
                "HIGH",
                features,
            )
        )

    return Tier2PassiveEvaluation(None)


def _ground_only_rlc_without_exact_membership(
    features: PassiveRoleFeatures,
    motifs: set[str],
) -> bool:
    ground_only_evidence = features.rlc_proximity == "GROUND_ONLY" or (
        features.shared_ground_only and "LC_RESONANT_NETWORK" in motifs
    )
    return bool(
        ground_only_evidence
        and not features.is_parallel_passive_member
        and not features.is_series_chain_member
        and not features.has_direct_inductor_neighbor
    )


def _resolved(decision: Tier2PassiveDecision) -> Tier2PassiveEvaluation:
    return Tier2PassiveEvaluation(decision=decision)


def _abstain(reason: str) -> Tier2PassiveEvaluation:
    return Tier2PassiveEvaluation(decision=None, abstention_reason=reason)


def _decision(
    semantic: str,
    rule_id: str,
    confidence: str,
    features: PassiveRoleFeatures,
) -> Tier2PassiveDecision:
    return Tier2PassiveDecision(
        semantic_category=semantic,
        rule_id=rule_id,
        confidence=confidence,
        evidence=features.evidence(),
    )
