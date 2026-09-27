"""Select target-relevant, diverse historical samples for inverse design."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from ActiveLearning.common import parameter_to_similarity_vector, select_mmr


_EPSILON = 1.0e-12
_SUPPORTED_OPERATORS = {"<", "<=", ">", ">=", "=="}


def _parse_targets(
    target_specs: Mapping[str, Any],
) -> list[tuple[str, str, float]]:
    targets = []
    for metric_name, spec in target_specs.items():
        if not isinstance(spec, Mapping):
            raise TypeError(f"target spec for {metric_name!r} must be a mapping")
        missing = [key for key in ("operator", "value") if key not in spec]
        if missing:
            raise ValueError(
                f"target spec for {metric_name!r} is missing: {', '.join(missing)}"
            )

        operator_name = spec["operator"]
        if (
            not isinstance(operator_name, str)
            or operator_name not in _SUPPORTED_OPERATORS
        ):
            raise ValueError(f"unsupported target operator: {operator_name!r}")
        try:
            target = float(spec["value"])
        except (TypeError, ValueError) as error:
            raise TypeError(
                f"target value for {metric_name!r} must be numeric"
            ) from error
        if not math.isfinite(target):
            raise ValueError(f"target value for {metric_name!r} must be finite")
        targets.append((str(metric_name), operator_name, target))
    return targets


def _usable_metric_values(
    record: Mapping[str, Any],
    targets: Sequence[tuple[str, str, float]],
) -> dict[str, float] | None:
    status = record.get("status")
    metrics = record.get("metrics")
    if not isinstance(status, Mapping) or not isinstance(metrics, Mapping):
        return None
    if status.get("parameter_valid") is not True:
        return None
    if status.get("simulation_valid") is not True:
        return None

    values = {}
    for metric_name, _, _ in targets:
        if metric_name not in metrics or metrics[metric_name] is None:
            return None
        try:
            measured = float(metrics[metric_name])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(measured):
            return None
        values[metric_name] = measured
    return values


def _target_relevance(
    metric_values: Mapping[str, float],
    targets: Sequence[tuple[str, str, float]],
) -> float:
    violations = []
    for metric_name, operator_name, target in targets:
        measured = metric_values[metric_name]
        denominator = max(abs(target), _EPSILON)
        if operator_name in {"<", "<="}:
            violation = max(0.0, (measured - target) / denominator)
        elif operator_name in {">", ">="}:
            violation = max(0.0, (target - measured) / denominator)
        else:
            violation = abs(measured - target) / denominator
        violations.append(violation)

    mean_violation = sum(violations) / len(violations)
    return 1.0 / (1.0 + mean_violation)


def _joint_target_violation(
    metric_values: Mapping[str, float],
    targets: Sequence[tuple[str, str, float]],
) -> float:
    violations = []
    for metric_name, operator_name, target in targets:
        measured = metric_values[metric_name]
        denominator = max(abs(target), _EPSILON)
        if operator_name in {"<", "<="}:
            violation = max(0.0, (measured - target) / denominator)
        elif operator_name in {">", ">="}:
            violation = max(0.0, (target - measured) / denominator)
        else:
            violation = abs(measured - target) / denominator
        violations.append(violation)
    return sum(violations) / len(violations)


def select_best_known_anchor(
    records: Sequence[Mapping[str, Any]],
    target_specs: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the first measured record with minimum joint target violation."""

    targets = _parse_targets(target_specs)
    best_record = None
    best_metrics = None
    best_violation = math.inf
    for record in records:
        metric_values = _usable_metric_values(record, targets)
        if metric_values is None:
            continue
        violation = _joint_target_violation(metric_values, targets)
        if violation < best_violation:
            best_record = record
            best_metrics = metric_values
            best_violation = violation

    if best_record is None:
        raise ValueError("No usable measured sample for inverse-design anchor")
    return {
        "sample_id": best_record.get("sample_id", best_record.get("candidate_id")),
        "params": dict(best_record["params"]),
        "measured_metrics": dict(best_metrics),
        "joint_violation": float(best_violation),
    }


def _feature_vectors(
    usable_records: Sequence[Mapping[str, Any]],
    parameter_template: Mapping[str, Any] | None = None,
) -> list[list[float]]:
    if parameter_template is not None:
        return [
            parameter_to_similarity_vector(
                record["params"],
                parameter_template,
                normalized_params=record.get("normalized_params"),
            )
            for record in usable_records
        ]

    first = usable_records[0].get("normalized_params")
    if not isinstance(first, Mapping):
        raise ValueError("usable records must contain normalized_params")
    parameter_order = list(first)
    expected_names = set(parameter_order)

    feature_vectors = []
    for record in usable_records:
        normalized = record.get("normalized_params")
        if not isinstance(normalized, Mapping):
            raise ValueError("usable records must contain normalized_params")
        if set(normalized) != expected_names:
            raise ValueError(
                "normalized_params keys must be consistent across usable records"
            )
        feature_vectors.append([normalized[name] for name in parameter_order])
    return feature_vectors


def select_inverse_design_context(
    records: Sequence[Mapping[str, Any]],
    target_specs: Mapping[str, Any] | None,
    k: int = 5,
    lambda_weight: float = 0.7,
    *,
    parameter_template: Mapping[str, Any] | None = None,
    return_diagnostics: bool = False,
):
    """Return usable records selected by target-aware generic MMR."""

    if target_specs is None or target_specs == {}:
        raise ValueError("target_specs are required for inverse design")
    if not isinstance(target_specs, Mapping):
        raise TypeError("target_specs must be a mapping")

    targets = _parse_targets(target_specs)
    usable_records = []
    usable_metrics = []
    for record in records:
        metric_values = _usable_metric_values(record, targets)
        if metric_values is not None:
            usable_records.append(record)
            usable_metrics.append(metric_values)

    if not usable_records:
        raise ValueError("No usable historical samples for inverse design context")

    relevance_scores = [
        _target_relevance(metric_values, targets)
        for metric_values in usable_metrics
    ]
    feature_vectors = _feature_vectors(usable_records, parameter_template)
    indices = select_mmr(
        relevance_scores,
        feature_vectors,
        k=k,
        lambda_weight=lambda_weight,
    )
    selected_context = [usable_records[index] for index in indices]
    if not return_diagnostics:
        return selected_context

    diagnostics = []
    for index in indices:
        record = usable_records[index]
        diagnostic = {
            "params": record["params"],
            "metrics": record["metrics"],
            "relevance": relevance_scores[index],
        }
        for identifier in ("sample_id", "record_id"):
            if identifier in record:
                diagnostic[identifier] = record[identifier]
        diagnostics.append(diagnostic)
    return selected_context, diagnostics
