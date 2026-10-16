"""Internal result model for selective semantic classification."""

from __future__ import annotations

from dataclasses import dataclass


SEMANTIC_STATUSES = frozenset({"RESOLVED", "UNRESOLVED"})


@dataclass(frozen=True)
class SemanticPrediction:
    circuit_id: str
    circuit_type: str
    parameter_id: str
    parameter_name: str | None
    source_kind: str
    device_id: str | None
    device_type: str | None
    parameter_kind: str
    predicted_semantic_category: str | None
    semantic_status: str
    rule_tier: str
    rule_id: str
    confidence: str | None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.semantic_status not in SEMANTIC_STATUSES:
            raise ValueError(f"Unsupported semantic status: {self.semantic_status}")
        resolved = self.semantic_status == "RESOLVED"
        if resolved != (self.predicted_semantic_category is not None):
            raise ValueError("Resolved status and predicted semantic disagree")
        if resolved and self.rule_tier not in {"TIER_1", "TIER_2"}:
            raise ValueError("Resolved predictions must come from Tier 1 or Tier 2")
        if resolved and self.confidence not in {"HIGH", "MEDIUM"}:
            raise ValueError("Resolved predictions must have HIGH or MEDIUM confidence")
        if not resolved and (self.rule_tier != "UNRESOLVED" or self.confidence is not None):
            raise ValueError("Unresolved predictions must not claim a tier/confidence")
