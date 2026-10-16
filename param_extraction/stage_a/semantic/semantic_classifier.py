"""Gold-independent runtime semantic classifier."""

from __future__ import annotations

from collections import Counter
from typing import Mapping

from ..ir import CircuitIR, PhysicalTargetIR
from ..validity import CandidateValidityResult, CircuitValidityResult
from .semantic_result import SemanticPrediction
from .passive_role_features import extract_passive_role_features
from .source_role_features import extract_source_role_features
from .tier1_rules import apply_tier1_rule
from .tier2_passive_rules import evaluate_tier2_passive_rule
from .tier2_source_rules import apply_tier2_source_rule


class SemanticClassifier:
    """Generate exactly one selective Tier-1 result per canonical target."""

    def classify(
        self,
        circuit: CircuitIR,
        validity: CircuitValidityResult | None = None,
    ) -> tuple[SemanticPrediction, ...]:
        validity_by_id: Mapping[str, CandidateValidityResult] = {}
        if validity is not None:
            if validity.circuit_id != circuit.circuit_id:
                raise ValueError("Validity result belongs to another circuit")
            counts = Counter(item.parameter_id for item in validity.candidates)
            if any(count != 1 for count in counts.values()):
                raise ValueError("Validity result contains duplicate parameter IDs")
            validity_by_id = {item.parameter_id: item for item in validity.candidates}
            if set(validity_by_id) != {item.parameter_id for item in circuit.physical_targets}:
                raise ValueError("Validity result does not cover the canonical target universe")

        preliminary = tuple(
            self.classify_target(circuit, target, validity_by_id.get(target.parameter_id))
            for target in sorted(circuit.physical_targets, key=lambda item: item.parameter_id)
        )
        prediction_by_id = {item.parameter_id: item for item in preliminary}
        final: list[SemanticPrediction] = []
        for target in sorted(circuit.physical_targets, key=lambda item: item.parameter_id):
            prediction = prediction_by_id[target.parameter_id]
            validity_item = validity_by_id.get(target.parameter_id)
            eligible_passive = (
                prediction.semantic_status == "UNRESOLVED"
                and target.parameter_kind in {"resistance", "capacitance", "inductance"}
                and (validity_item is None or validity_item.validity_status == "VALID")
            )
            if not eligible_passive:
                final.append(prediction)
                continue
            features = extract_passive_role_features(circuit, target, preliminary)
            outcome = evaluate_tier2_passive_rule(features)
            decision = outcome.decision
            if decision is None:
                evidence = features.evidence() if features is not None else prediction.evidence
                if outcome.abstention_reason is not None:
                    evidence = (
                        *evidence,
                        f"abstention_reason={outcome.abstention_reason}",
                    )
                final.append(
                    self._unresolved(
                        circuit,
                        target,
                        "T2_PASSIVE_ROLE_UNRESOLVED",
                        evidence,
                    )
                )
                continue
            final.append(
                self._base(
                    circuit,
                    target,
                    predicted_semantic_category=decision.semantic_category,
                    semantic_status="RESOLVED",
                    rule_tier="TIER_2",
                    rule_id=decision.rule_id,
                    confidence=decision.confidence,
                    evidence=decision.evidence,
                )
            )
        return tuple(final)

    def classify_target(
        self,
        circuit: CircuitIR,
        target: PhysicalTargetIR,
        validity: CandidateValidityResult | None = None,
    ) -> SemanticPrediction:
        if validity is not None and validity.validity_status != "VALID":
            return self._unresolved(
                circuit,
                target,
                "T1_NONVALID_TARGET_DEFERRED",
                (
                    f"validity_status={validity.validity_status}",
                    f"validity_rule_id={validity.validity_rule_id}",
                ),
            )
        decision = apply_tier1_rule(circuit, target)
        if decision is not None:
            return self._base(
                circuit,
                target,
                predicted_semantic_category=decision.semantic_category,
                semantic_status="RESOLVED",
                rule_tier="TIER_1",
                rule_id=decision.rule_id,
                confidence="HIGH",
                evidence=decision.evidence,
            )

        source_features = extract_source_role_features(circuit, target)
        source_decision = apply_tier2_source_rule(source_features)
        if source_decision is not None:
            return self._base(
                circuit,
                target,
                predicted_semantic_category=source_decision.semantic_category,
                semantic_status="RESOLVED",
                rule_tier="TIER_2",
                rule_id=source_decision.rule_id,
                confidence=source_decision.confidence,
                evidence=source_decision.evidence,
            )

        evidence = (
            source_features.evidence()
            if source_features is not None
            else (
                f"source_kind={target.source_kind}",
                f"device_type={target.device_type}",
                f"parameter_kind={target.parameter_kind}",
                f"canonical_target={target.parameter_id}",
            )
        )
        return self._unresolved(
            circuit,
            target,
            "T2_SOURCE_ROLE_UNRESOLVED" if source_features is not None else "T1_NO_DETERMINISTIC_RULE",
            evidence,
        )

    @staticmethod
    def _unresolved(
        circuit: CircuitIR,
        target: PhysicalTargetIR,
        rule_id: str,
        evidence: tuple[str, ...],
    ) -> SemanticPrediction:
        return SemanticClassifier._base(
            circuit,
            target,
            predicted_semantic_category=None,
            semantic_status="UNRESOLVED",
            rule_tier="UNRESOLVED",
            rule_id=rule_id,
            confidence=None,
            evidence=evidence,
        )

    @staticmethod
    def _base(
        circuit: CircuitIR,
        target: PhysicalTargetIR,
        *,
        predicted_semantic_category: str | None,
        semantic_status: str,
        rule_tier: str,
        rule_id: str,
        confidence: str | None,
        evidence: tuple[str, ...],
    ) -> SemanticPrediction:
        return SemanticPrediction(
            circuit_id=circuit.circuit_id,
            circuit_type=circuit.circuit_type,
            parameter_id=target.parameter_id,
            parameter_name=target.parameter_name,
            source_kind=target.source_kind,
            device_id=target.device_id,
            device_type=target.device_type,
            parameter_kind=target.parameter_kind,
            predicted_semantic_category=predicted_semantic_category,
            semantic_status=semantic_status,
            rule_tier=rule_tier,
            rule_id=rule_id,
            confidence=confidence,
            evidence=evidence,
        )


def classify_semantics(
    circuit: CircuitIR,
    validity: CircuitValidityResult | None = None,
) -> tuple[SemanticPrediction, ...]:
    return SemanticClassifier().classify(circuit, validity)
