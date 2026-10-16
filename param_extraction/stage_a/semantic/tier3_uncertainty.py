"""Tier-3 semantic finalization: explicit abstention, never category inference."""

from __future__ import annotations

from dataclasses import dataclass

from .semantic_result import SemanticPrediction


@dataclass(frozen=True)
class FinalSemanticResult:
    semantic_category: str | None
    semantic_status: str
    rule_tier: str
    rule_id: str
    confidence: str
    evidence: tuple[str, ...]


def finalize_semantic_prediction(
    prediction: SemanticPrediction,
    *,
    validity_status: str,
) -> FinalSemanticResult:
    """Finalize one selective prediction while preserving all resolved results."""

    if prediction.semantic_status == "RESOLVED":
        assert prediction.confidence is not None
        return FinalSemanticResult(
            semantic_category=prediction.predicted_semantic_category,
            semantic_status="RESOLVED",
            rule_tier=prediction.rule_tier,
            rule_id=prediction.rule_id,
            confidence=prediction.confidence,
            evidence=prediction.evidence,
        )
    if validity_status != "VALID":
        return FinalSemanticResult(
            semantic_category=None,
            semantic_status="NOT_APPLICABLE",
            rule_tier="TIER_1",
            rule_id="T1_NONVALID_TARGET_DEFERRED",
            confidence="LOW",
            evidence=(
                *prediction.evidence,
                f"previous_rule_id={prediction.rule_id}",
                f"validity_status={validity_status}",
            ),
        )

    ambiguity = _ambiguity_family(prediction.evidence)
    return FinalSemanticResult(
        semantic_category=None,
        semantic_status="UNCERTAIN",
        rule_tier="TIER_3",
        rule_id="T3_SEMANTIC_ROLE_UNCERTAIN",
        confidence="LOW",
        evidence=(
            *prediction.evidence,
            "previous_status=UNRESOLVED",
            f"previous_rule_id={prediction.rule_id}",
            f"last_attempted_tier={_last_attempted_tier(prediction.rule_id)}",
            f"ambiguity_family={ambiguity}",
            "reason=SEMANTIC_ROLE_UNCERTAIN",
        ),
    )


def _last_attempted_tier(rule_id: str) -> str:
    if rule_id.startswith("T2_"):
        return "TIER_2"
    return "TIER_1"


def _ambiguity_family(evidence: tuple[str, ...]) -> str:
    joined = "\n".join(evidence)
    if "DIFFERENT_ROLE_SOURCE_TO_SOURCE" in joined:
        return "SOURCE_TO_SOURCE_AMBIGUITY"
    if "GROUND_ONLY_RLC_PROXIMITY_WITHOUT_EXACT_MEMBERSHIP" in joined:
        return "PASSIVE_LOOP_MEMBERSHIP_UNCERTAIN"
    if "SOURCE_SHUNT_ACTIVE_CONDUCTION" in joined:
        return "SOURCE_IMPEDANCE_VS_DEGENERATION"
    if "source_motif=" in joined or "interface" in joined.lower():
        return "SOURCE_IMPEDANCE_VS_DEGENERATION"
    if any(token in joined for token in ("passive_kind=", "passive_topology=")):
        return "INTERNAL_PASSIVE_ROLE_UNCLEAR"
    return "OTHER_SEMANTIC_AMBIGUITY"
