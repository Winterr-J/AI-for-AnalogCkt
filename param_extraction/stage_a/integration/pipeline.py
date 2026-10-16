"""Compose existing Stage A results into frozen-schema runtime rows."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from ..ir import CircuitIR
from ..optimization import evaluate_optimization_eligibility
from ..semantic import SemanticPrediction, classify_semantics
from ..semantic.tier3_uncertainty import finalize_semantic_prediction
from ..validity import CircuitValidityResult, validate_candidates


@dataclass(frozen=True)
class StageAOutputRow:
    circuit_id: str
    circuit_type: str
    parameter_id: str
    parameter_name: str | None
    source_kind: str
    device_id: str | None
    device_type: str | None
    parameter_kind: str
    validity_status: str
    semantic_category: str | None
    binding_status: str
    optimization_eligible: bool
    rule_tier: str
    rule_id: str
    confidence: str
    review_status: str
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["evidence"] = list(self.evidence)
        return result


def build_stage_a_output_rows(
    circuit: CircuitIR,
    *,
    validity: CircuitValidityResult | None = None,
    semantic_predictions: tuple[SemanticPrediction, ...] | None = None,
) -> tuple[StageAOutputRow, ...]:
    """Build one final row per already parsed and binding-resolved target."""

    if any(target.binding is None for target in circuit.physical_targets):
        raise ValueError("Circuit IR must be binding-resolved before final integration")
    validity = validity or validate_candidates(circuit)
    if validity.circuit_id != circuit.circuit_id:
        raise ValueError("Validity result belongs to another circuit")
    predictions = semantic_predictions or classify_semantics(circuit, validity)
    target_ids = {target.parameter_id for target in circuit.physical_targets}
    validity_by_id = _unique_by_id(validity.candidates, "validity")
    semantic_by_id = _unique_by_id(predictions, "semantic")
    if set(validity_by_id) != target_ids or set(semantic_by_id) != target_ids:
        raise ValueError("Integration inputs do not cover the canonical target universe")

    rows: list[StageAOutputRow] = []
    for target in sorted(circuit.physical_targets, key=lambda item: item.parameter_id):
        validity_item = validity_by_id[target.parameter_id]
        prediction = semantic_by_id[target.parameter_id]
        final_semantic = finalize_semantic_prediction(
            prediction,
            validity_status=validity_item.validity_status,
        )
        binding = target.binding
        assert binding is not None
        optimization = evaluate_optimization_eligibility(
            validity_status=validity_item.validity_status,
            binding_status=binding.binding_status,
            semantic_category=final_semantic.semantic_category,
            semantic_status=final_semantic.semantic_status,
        )
        review_status = (
            "NEEDS_REVIEW"
            if final_semantic.semantic_status == "UNCERTAIN"
            else validity_item.review_status
            if validity_item.validity_status != "VALID"
            else "AUTO"
        )
        rows.append(
            StageAOutputRow(
                circuit_id=circuit.circuit_id,
                circuit_type=circuit.circuit_type,
                parameter_id=target.parameter_id,
                parameter_name=target.parameter_name,
                source_kind=target.source_kind,
                device_id=target.device_id,
                device_type=target.device_type,
                parameter_kind=target.parameter_kind,
                validity_status=validity_item.validity_status,
                semantic_category=final_semantic.semantic_category,
                binding_status=binding.binding_status,
                optimization_eligible=optimization.optimization_eligible,
                rule_tier=final_semantic.rule_tier,
                rule_id=final_semantic.rule_id,
                confidence=final_semantic.confidence,
                review_status=review_status,
                evidence=tuple(
                    dict.fromkeys(
                        (
                            *validity_item.evidence,
                            *binding.evidence,
                            *final_semantic.evidence,
                            f"optimization_reason={optimization.optimization_reason}",
                        )
                    )
                ),
            )
        )
    return tuple(rows)


def _unique_by_id(rows, label: str) -> dict[str, object]:
    counts = Counter(row.parameter_id for row in rows)
    duplicates = sorted(key for key, count in counts.items() if count != 1)
    if duplicates:
        raise ValueError(f"Duplicate {label} parameter IDs: {duplicates}")
    return {row.parameter_id: row for row in rows}
