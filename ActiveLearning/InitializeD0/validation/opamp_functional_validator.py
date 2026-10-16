"""Interpret an OpAmp evaluator result without running another simulation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from numbers import Real
from typing import Any

import numpy as np

from ActiveLearning.InitializeD0.evaluator.opamp_evaluator import (
    OpAmpEvaluationResult,
)
from ActiveLearning.onboarding.models import OpAmpSemanticMapping

from .parameter_validator import validate_parameters
from .spec_validator import check_meets_spec


__all__ = ["OpAmpFunctionalValidator", "check_opamp_functionality"]


_SUPPORTED_MEASUREMENT_MODES = {
    "existing_feedback_return_ratio",
    "synthesized_unity_feedback_return_ratio",
}
_GROUND_NAMES = {"0", "gnd", "vss"}
_CANONICAL_METRICS = (
    "low_frequency_open_loop_gain_db",
    "unity_gain_frequency_hz",
    "phase_margin_deg",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return {}


def _finite(value: Any) -> bool:
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _lookup(values: Mapping[str, Any], name: str | None) -> Any:
    if name is None:
        return None
    target = str(name).casefold()
    for key, value in values.items():
        if str(key).casefold() == target:
            return value
    if target in _GROUND_NAMES:
        return 0.0
    return None


def _complex_vector(value: Any) -> np.ndarray:
    if isinstance(value, Mapping) and "real" in value and "imag" in value:
        return np.asarray(value["real"], dtype=float) + 1j * np.asarray(
            value["imag"], dtype=float
        )
    return np.asarray(value, dtype=complex)


def _finite_ac_traces(traces: Mapping[str, Any]) -> bool | None:
    """Validate traces when present; absence means rely on evaluator diagnostics."""

    if not traces:
        return None
    if "frequency_hz" not in traces or "return_ratio" not in traces:
        return False
    try:
        frequency = np.asarray(traces["frequency_hz"], dtype=float).reshape(-1)
        ratio = _complex_vector(traces["return_ratio"]).reshape(-1)
    except (TypeError, ValueError):
        return False
    return bool(
        frequency.size >= 2
        and frequency.size == ratio.size
        and np.all(np.isfinite(frequency))
        and np.all(frequency > 0.0)
        and np.all(np.diff(frequency) > 0.0)
        and np.all(np.isfinite(ratio.real))
        and np.all(np.isfinite(ratio.imag))
    )


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


class OpAmpFunctionalValidator:
    """Classify one already-evaluated OpAmp point as valid/invalid/not-evaluable."""

    def __init__(
        self,
        *,
        parameter_template: Mapping[str, Any],
        semantic_mapping: OpAmpSemanticMapping,
        rail_tolerance_v: float = 1.0e-6,
    ) -> None:
        self.parameter_template = parameter_template
        self.semantic_mapping = semantic_mapping
        self.rail_tolerance_v = float(rail_tolerance_v)
        if not math.isfinite(self.rail_tolerance_v) or self.rail_tolerance_v < 0.0:
            raise ValueError("rail_tolerance_v must be finite and non-negative")

    def __call__(
        self,
        params: Mapping[str, Any],
        evaluation_result: OpAmpEvaluationResult | Mapping[str, Any],
        *,
        target_specs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.validate(params, evaluation_result, target_specs=target_specs)

    def validate(
        self,
        params: Mapping[str, Any],
        evaluation_result: OpAmpEvaluationResult | Mapping[str, Any],
        *,
        target_specs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        parameter_result = validate_parameters(params, self.parameter_template)
        if isinstance(evaluation_result, Mapping):
            payload = evaluation_result
            metrics = _record(payload.get("metrics"))
            evaluator_diagnostics = dict(_mapping(payload.get("diagnostics")))
            evaluator_simulation_valid = bool(payload.get("simulation_valid", False))
            measurement_status = str(payload.get("measurement_status", "unavailable"))
            evaluator_reasons = list(payload.get("failure_reasons") or [])
            traces = _mapping(payload.get("traces"))
        else:
            metrics = _record(evaluation_result.metrics)
            evaluator_diagnostics = dict(evaluation_result.diagnostics)
            evaluator_simulation_valid = bool(evaluation_result.simulation_valid)
            measurement_status = str(evaluation_result.measurement_status)
            evaluator_reasons = list(evaluation_result.failure_reasons)
            traces = _mapping(evaluation_result.traces)

        canonical_metrics = {name: metrics.get(name) for name in _CANONICAL_METRICS}
        nodes = _mapping(evaluator_diagnostics.get("native_node_voltages"))
        branches = _mapping(evaluator_diagnostics.get("native_branch_currents"))

        native_op_valid = bool(evaluator_diagnostics.get("native_op_valid"))
        output_node = self.semantic_mapping.outputs.positive_node
        positive_input = evaluator_diagnostics.get("positive_input")
        negative_input = evaluator_diagnostics.get("negative_input")
        required_nodes = [output_node, positive_input, negative_input]
        required_nodes.extend(
            bias.node for bias in self.semantic_mapping.bias_sources if bias.node
        )
        required_nodes = list(
            dict.fromkeys(str(node) for node in required_nodes if node is not None)
        )
        missing_or_non_finite_nodes = [
            node for node in required_nodes if not _finite(_lookup(nodes, node))
        ]

        bias_value_checks: dict[str, bool] = {}
        for bias in self.semantic_mapping.bias_sources:
            node_value = _lookup(nodes, bias.node) if bias.node else None
            branch_value = _lookup(branches, bias.element)
            available = node_value if node_value is not None else branch_value
            bias_value_checks[bias.element] = _finite(available)
        required_operating_values_finite = bool(
            not missing_or_non_finite_nodes and all(bias_value_checks.values())
        )

        positive_rail = _lookup(nodes, self.semantic_mapping.supplies.positive)
        negative_rail = _lookup(nodes, self.semantic_mapping.supplies.negative)
        output_voltage = _lookup(nodes, output_node)
        rails_available = all(
            _finite(value) for value in (positive_rail, negative_rail)
        )
        output_finite = _finite(output_voltage)
        rail_margin_low = None
        rail_margin_high = None
        gross_rail_violation = False
        output_pinned_to_rail = False
        if rails_available and output_finite:
            low_rail = min(float(negative_rail), float(positive_rail))
            high_rail = max(float(negative_rail), float(positive_rail))
            output_numeric = float(output_voltage)
            rail_margin_low = output_numeric - low_rail
            rail_margin_high = high_rail - output_numeric
            gross_rail_violation = bool(
                output_numeric < low_rail - self.rail_tolerance_v
                or output_numeric > high_rail + self.rail_tolerance_v
            )
            output_pinned_to_rail = bool(
                abs(rail_margin_low) <= self.rail_tolerance_v
                or abs(rail_margin_high) <= self.rail_tolerance_v
            )

        operating_region_valid = bool(
            native_op_valid
            and required_operating_values_finite
            and rails_available
            and output_finite
            and not gross_rail_violation
        )
        polarity_resolved = bool(
            evaluator_diagnostics.get("polarity_status") == "resolved"
            and isinstance(positive_input, str)
            and isinstance(negative_input, str)
        )
        measurement_mode = evaluator_diagnostics.get("measurement_mode")
        measurement_mode_supported = bool(
            measurement_mode in _SUPPORTED_MEASUREMENT_MODES
            and measurement_status != "unsupported_measurement_mode"
        )
        feedback_testbench_valid = bool(
            measurement_mode_supported
            and evaluator_diagnostics.get("unity_feedback_confirmed")
        )
        decorated_op_valid = bool(evaluator_diagnostics.get("decorated_op_valid"))
        dc_preservation_valid = bool(
            evaluator_diagnostics.get("dc_preservation_valid")
        )
        ac_trace_valid = _finite_ac_traces(traces)
        ac_simulation_valid = bool(
            evaluator_diagnostics.get("ac_simulation_valid")
            and ac_trace_valid is not False
        )
        return_ratio_valid = bool(
            evaluator_diagnostics.get("return_ratio_valid")
            and ac_trace_valid is not False
        )
        low_frequency_gain_available = _finite(
            canonical_metrics["low_frequency_open_loop_gain_db"]
        )
        crossing_count = evaluator_diagnostics.get("unity_crossing_count", 0)
        crossing_direction = evaluator_diagnostics.get("crossing_direction")
        unity_crossing_available = bool(
            _finite(canonical_metrics["unity_gain_frequency_hz"])
            and isinstance(crossing_count, int)
            and not isinstance(crossing_count, bool)
            and crossing_count >= 1
            and crossing_direction == "descending"
        )
        phase_margin_available = _finite(canonical_metrics["phase_margin_deg"])

        simulation_valid = bool(
            evaluator_simulation_valid
            and native_op_valid
            and decorated_op_valid
            and ac_simulation_valid
        )
        invalid = bool(
            not parameter_result["parameter_valid"]
            or not native_op_valid
            or not required_operating_values_finite
            or gross_rail_violation
            or not output_finite
        )
        measurement_checks = (
            rails_available,
            polarity_resolved,
            feedback_testbench_valid,
            decorated_op_valid,
            dc_preservation_valid,
            ac_simulation_valid,
            return_ratio_valid,
            low_frequency_gain_available,
            unity_crossing_available,
            phase_margin_available,
        )
        if invalid:
            functional_status = "invalid"
        elif not all(measurement_checks):
            functional_status = "not_evaluable"
        else:
            functional_status = "valid"
        functional_valid = functional_status == "valid"

        reasons = [str(reason) for reason in evaluator_reasons]
        if not parameter_result["parameter_valid"]:
            _append_reason(reasons, "parameter_invalid")
        if not native_op_valid:
            _append_reason(reasons, "native_op_failed")
        if native_op_valid and not required_operating_values_finite:
            _append_reason(reasons, "non_finite_native_operating_point")
        if not rails_available:
            _append_reason(reasons, "supply_rails_unavailable")
        if gross_rail_violation:
            _append_reason(reasons, "output_outside_supply_rails")
        if not polarity_resolved:
            _append_reason(reasons, "input_polarity_unresolved")
        if not measurement_mode_supported:
            _append_reason(reasons, "unsupported_measurement_mode")
        elif not feedback_testbench_valid:
            _append_reason(reasons, "feedback_testbench_invalid")
        if not decorated_op_valid:
            _append_reason(reasons, "decorated_op_failed")
        if not dc_preservation_valid:
            _append_reason(reasons, "dc_preservation_invalid")
        if not ac_simulation_valid:
            _append_reason(reasons, "ac_simulation_failed")
        if not return_ratio_valid:
            _append_reason(reasons, "return_ratio_invalid")
        if not low_frequency_gain_available:
            _append_reason(reasons, "low_frequency_gain_unavailable")
        if not unity_crossing_available:
            _append_reason(reasons, "no_valid_unity_crossing")
        if not phase_margin_available:
            _append_reason(reasons, "phase_margin_unavailable")

        meets_spec = check_meets_spec(canonical_metrics, target_specs)["meets_spec"]
        checks = {
            "native_op_valid": native_op_valid,
            "required_operating_values_finite": required_operating_values_finite,
            "operating_region_valid": operating_region_valid,
            "polarity_resolved": polarity_resolved,
            "measurement_mode_supported": measurement_mode_supported,
            "feedback_testbench_valid": feedback_testbench_valid,
            "decorated_op_valid": decorated_op_valid,
            "dc_preservation_valid": dc_preservation_valid,
            "ac_simulation_valid": ac_simulation_valid,
            "return_ratio_valid": return_ratio_valid,
            "low_frequency_gain_available": low_frequency_gain_available,
            "unity_crossing_available": unity_crossing_available,
            "phase_margin_available": phase_margin_available,
        }
        diagnostics = dict(evaluator_diagnostics)
        diagnostics.update(
            {
                "evaluator_simulation_valid": evaluator_simulation_valid,
                "evaluator_measurement_status": measurement_status,
                "evaluator_failure_reasons": evaluator_reasons,
                "parameter_violations": list(parameter_result["violations"]),
                "required_native_nodes": required_nodes,
                "missing_or_non_finite_native_nodes": missing_or_non_finite_nodes,
                "bias_value_finite": bias_value_checks,
                "output_voltage": output_voltage,
                "positive_rail": positive_rail,
                "negative_rail": negative_rail,
                "rail_margin_low": rail_margin_low,
                "rail_margin_high": rail_margin_high,
                "rail_tolerance_v": self.rail_tolerance_v,
                "output_pinned_to_rail": output_pinned_to_rail,
                "ac_trace_valid": ac_trace_valid,
            }
        )
        status = {
            "parameter_valid": bool(parameter_result["parameter_valid"]),
            "simulation_valid": simulation_valid,
            "functional_valid": functional_valid,
            "functional_status": functional_status,
            "meets_spec": meets_spec,
        }
        return {
            "status": status,
            "functional_checks": checks,
            "metrics": canonical_metrics,
            "failure_reasons": reasons,
            "diagnostics": diagnostics,
        }


def check_opamp_functionality(
    params: Mapping[str, Any],
    evaluation_result: OpAmpEvaluationResult | Mapping[str, Any],
    *,
    parameter_template: Mapping[str, Any],
    semantic_mapping: OpAmpSemanticMapping,
    target_specs: Mapping[str, Any] | None = None,
    rail_tolerance_v: float = 1.0e-6,
) -> dict[str, Any]:
    """Functional facade for callers that do not need a reusable validator."""

    return OpAmpFunctionalValidator(
        parameter_template=parameter_template,
        semantic_mapping=semantic_mapping,
        rail_tolerance_v=rail_tolerance_v,
    ).validate(params, evaluation_result, target_specs=target_specs)
