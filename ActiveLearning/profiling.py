"""Real-SPICE parameter-space profiling for benchmark target construction."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
from scipy.stats import qmc, spearmanr

from ActiveLearning.InitializeD0.evaluator import evaluate_current_mirror
from ActiveLearning.InitializeD0.validation import (
    check_current_mirror_functionality,
    validate_parameters,
)
from ActiveLearning.onboarding import build_active_learning_circuit_config


_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _ROOT.parent
_DEFAULT_OUTPUT_ROOT = _ROOT / "results" / "profiling"
_PERCENTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
_TARGET_LEVELS = {
    "easy": {
        "minimum": 0.15,
        "maximum": 0.25,
        "center": 0.20,
        "minimum_resistance_quantile": 0.40,
    },
    "medium": {
        "minimum": 0.05,
        "maximum": 0.10,
        "center": 0.075,
        "minimum_resistance_quantile": 0.50,
    },
    "hard": {
        "minimum": 0.01,
        "maximum": 0.03,
        "center": 0.02,
        "minimum_resistance_quantile": 0.60,
    },
}

_OPAMP_METRICS = (
    "low_frequency_open_loop_gain_db",
    "unity_gain_frequency_hz",
    "phase_margin_deg",
)
_OPAMP_FAILURE_TAXONOMY = (
    "parameter_invalid",
    "native_op_failed",
    "gross_rail_violation",
    "input_polarity_unresolved",
    "unsupported_measurement_mode",
    "feedback_decoration_failed",
    "feedback_testbench_invalid",
    "decorated_op_failed",
    "dc_preservation_invalid",
    "ac_simulation_failed",
    "return_ratio_invalid",
    "low_frequency_gain_unavailable",
    "no_valid_unity_crossing",
    "phase_margin_unavailable",
    "simulation_exception",
)


@dataclass(frozen=True)
class ProfilingHandler:
    """Circuit-specific profiling behavior behind the shared sampler/artifacts."""

    circuit_type: str
    metric_names: tuple[str, ...]
    schema_version: str


_PROFILING_HANDLERS = {
    "CurrentMirror": ProfilingHandler(
        circuit_type="CurrentMirror",
        metric_names=("current_error", "output_resistance"),
        schema_version="spice_profiling_v1",
    ),
    "OpAmp": ProfilingHandler(
        circuit_type="OpAmp",
        metric_names=_OPAMP_METRICS,
        schema_version="opamp_spice_profiling_v1",
    ),
}


def resolve_profiling_handler(circuit_type: str) -> ProfilingHandler:
    """Resolve the registered profiling contract for one runtime circuit type."""

    try:
        return _PROFILING_HANDLERS[str(circuit_type)]
    except KeyError as error:
        raise ValueError(f"Unsupported profiling circuit type: {circuit_type}") from error


def registered_profiling_circuit_types() -> tuple[str, ...]:
    return tuple(_PROFILING_HANDLERS)


def _parameter_space(runtime: Any) -> list[dict[str, Any]]:
    """Return ordered AutoConfig parameter metadata used by the profiler."""

    space = []
    for parameter in runtime.runtime_parameters:
        lower = float(parameter.lower)
        upper = float(parameter.upper)
        if parameter.variable_type != "continuous":
            raise NotImplementedError(
                f"Profiling supports continuous variables only: {parameter.name}"
            )
        if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
            raise ValueError(f"{parameter.name}: invalid profiling bounds")
        space.append(
            {
                "name": parameter.name,
                "nominal": float(parameter.nominal),
                "lower": lower,
                "upper": upper,
                "scale": parameter.scale,
                "transform": parameter.transform,
                "fixed_sign": parameter.fixed_sign,
                "unit": parameter.unit,
            }
        )
    if not space:
        raise ValueError("AutoConfig parameter space is empty")
    return space


def _unit_to_physical(value: float, parameter: Mapping[str, Any]) -> float:
    lower = float(parameter["lower"])
    upper = float(parameter["upper"])
    transform = str(parameter.get("transform") or "").casefold()
    scale = str(parameter.get("scale") or "linear").casefold()
    fixed_sign = str(parameter.get("fixed_sign") or "").casefold()

    if transform == "log_magnitude" or scale in {"log-magnitude", "log_magnitude"}:
        if not (upper < 0.0 or lower > 0.0):
            raise ValueError(
                f"{parameter['name']}: log-magnitude range must not cross zero"
            )
        magnitude_lower = min(abs(lower), abs(upper))
        magnitude_upper = max(abs(lower), abs(upper))
        if magnitude_lower <= 0.0:
            raise ValueError(f"{parameter['name']}: log-magnitude requires nonzero bounds")
        magnitude = math.exp(
            math.log(magnitude_lower)
            + float(value) * (math.log(magnitude_upper) - math.log(magnitude_lower))
        )
        negative = fixed_sign == "negative" or upper < 0.0
        physical = -magnitude if negative else magnitude
    elif scale == "log":
        if lower <= 0.0:
            raise ValueError(f"{parameter['name']}: log scale requires positive bounds")
        physical = math.exp(
            math.log(lower) + float(value) * (math.log(upper) - math.log(lower))
        )
    elif scale in {"linear", "none", ""}:
        physical = lower + float(value) * (upper - lower)
    else:
        raise NotImplementedError(
            f"{parameter['name']}: unsupported profiling scale {parameter['scale']!r}"
        )
    return float(min(upper, max(lower, physical)))


def generate_parameter_samples(
    runtime: Any,
    *,
    samples: int,
    sampler: str,
    seed: int,
) -> list[dict[str, Any]]:
    """Generate deterministic physical samples from AutoConfig metadata."""

    if samples < 1:
        raise ValueError("samples must be at least 1")
    space = _parameter_space(runtime)
    dimension = len(space)
    sampler_name = sampler.casefold()
    if sampler_name == "sobol":
        engine = qmc.Sobol(d=dimension, scramble=True, seed=int(seed))
        power = int(math.ceil(math.log2(samples)))
        normalized_matrix = engine.random_base2(power)[:samples]
    elif sampler_name == "random":
        normalized_matrix = np.random.default_rng(seed).random((samples, dimension))
    else:
        raise ValueError("sampler must be 'sobol' or 'random'")

    result = []
    for index, vector in enumerate(normalized_matrix):
        normalized = {
            parameter["name"]: float(value)
            for parameter, value in zip(space, vector)
        }
        params = {
            parameter["name"]: _unit_to_physical(value, parameter)
            for parameter, value in zip(space, vector)
        }
        result.append(
            {
                "sample_id": f"profile_{index:04d}",
                "params": params,
                "normalized_params": normalized,
            }
        )
    return result


def _functional_config(adapter: Any) -> dict[str, Any]:
    nominal = float(adapter.vout_nominal)
    return {
        "compliance_vout_start": max(0.05, nominal - 1.0),
        "compliance_vout_stop": nominal + 1.0,
        "compliance_points": 11,
        "reference_scales": (0.8, 1.0, 1.2),
    }


def _evaluate_point(
    sample: Mapping[str, Any],
    runtime: Any,
    evaluator: Callable[..., tuple[float | None, float | None, bool]],
    functional_validator: Callable[..., Mapping[str, Any]],
    functional_config: Mapping[str, Any],
) -> dict[str, Any]:
    params = dict(sample["params"])
    record = {
        "sample_id": str(sample["sample_id"]),
        "params": params,
        "normalized_params": dict(sample.get("normalized_params") or {}),
        "parameter_valid": False,
        "simulation_valid": False,
        "functional_valid": None,
        "functional_failure_reasons": [],
        "current_error": None,
        "output_resistance": None,
        "compliance_relative_variation": None,
        "tracking_max_error": None,
        "compliance_quality_smoke_passed": None,
        "tracking_quality_smoke_passed": None,
        "compliance_output_branch_alive": None,
        "reference_responsive": None,
        "failure_reason": None,
    }
    validation = validate_parameters(params, runtime.param_template)
    record["parameter_valid"] = bool(validation["parameter_valid"])
    if not record["parameter_valid"]:
        record["failure_reason"] = "; ".join(validation["violations"])
        return record

    current_error, output_resistance, simulation_valid = evaluator(
        params,
        adapter=runtime.adapter,
    )
    record["simulation_valid"] = bool(simulation_valid)
    if not simulation_valid:
        record["failure_reason"] = "CurrentMirror evaluator returned simulation_valid=False"
        return record

    record["current_error"] = float(current_error)
    record["output_resistance"] = float(output_resistance)
    functional = functional_validator(
        params,
        adapter=runtime.adapter,
        **dict(functional_config),
    )
    record["functional_valid"] = bool(functional["functional_feasible"])
    record["functional_failure_reasons"] = list(
        functional.get("failure_reasons") or []
    )
    record["compliance_relative_variation"] = functional["compliance"][
        "relative_variation"
    ]
    record["tracking_max_error"] = functional["reference_tracking"][
        "max_tracking_error"
    ]
    record["compliance_quality_smoke_passed"] = functional["compliance"].get(
        "quality_smoke_passed",
        functional["compliance"].get("passed"),
    )
    record["tracking_quality_smoke_passed"] = functional[
        "reference_tracking"
    ].get(
        "quality_smoke_passed",
        functional["reference_tracking"].get("passed"),
    )
    record["compliance_output_branch_alive"] = functional["compliance"].get(
        "output_branch_alive"
    )
    record["reference_responsive"] = functional["reference_tracking"].get(
        "responsive"
    )
    return record


def _record_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return {}


def _invoke_opamp_evaluator(evaluator: Any, params: Mapping[str, float]) -> Any:
    method = getattr(evaluator, "evaluate", None)
    return method(params) if callable(method) else evaluator(params)


def _invoke_opamp_validator(
    validator: Any,
    params: Mapping[str, float],
    evaluation: Any,
) -> Mapping[str, Any]:
    method = getattr(validator, "validate", None)
    if callable(method):
        return method(params, evaluation, target_specs=None)
    return validator(params, evaluation, target_specs=None)


def _empty_opamp_record(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": str(sample["sample_id"]),
        "params": dict(sample["params"]),
        "normalized_params": dict(sample.get("normalized_params") or {}),
        "parameter_valid": False,
        "simulation_valid": False,
        "functional_valid": False,
        "functional_status": "invalid",
        "low_frequency_open_loop_gain_db": None,
        "unity_gain_frequency_hz": None,
        "phase_margin_deg": None,
        "failure_reasons": [],
        "native_op_valid": False,
        "operating_region_valid": False,
        "feedback_testbench_valid": False,
        "decorated_op_valid": False,
        "dc_preservation_valid": False,
        "ac_simulation_valid": False,
        "polarity_status": "unavailable",
        "return_ratio_valid": False,
        "low_frequency_gain_available": False,
        "unity_crossing_available": False,
        "phase_margin_available": False,
        "unity_crossing_count": 0,
        "output_voltage": None,
        "rail_margin_low": None,
        "rail_margin_high": None,
        "feedback_dc_offset_v": None,
        "max_dc_voltage_difference": None,
        "low_frequency_plateau_spread_db": None,
        "measurement_status": "unavailable",
        "evaluation_wall_time_seconds": 0.0,
    }


def _evaluate_opamp_point(
    sample: Mapping[str, Any],
    runtime: Any,
    evaluator: Any,
    functional_validator: Any,
) -> dict[str, Any]:
    """Evaluate one OpAmp point once, then classify the existing result."""

    record = _empty_opamp_record(sample)
    params = record["params"]
    parameter_result = validate_parameters(params, runtime.param_template)
    record["parameter_valid"] = bool(parameter_result["parameter_valid"])
    if not record["parameter_valid"]:
        record["failure_reasons"] = ["parameter_invalid"]
        record["failure_reason"] = "; ".join(parameter_result["violations"])
        return record

    started = perf_counter()
    try:
        evaluation = _invoke_opamp_evaluator(evaluator, params)
        validation = dict(
            _invoke_opamp_validator(
                functional_validator,
                params,
                evaluation,
            )
        )
    except Exception as error:  # keep a long profile alive on one unexpected point
        record["evaluation_wall_time_seconds"] = perf_counter() - started
        record["failure_reasons"] = ["simulation_exception"]
        record["failure_reason"] = f"{type(error).__name__}: {error}"
        return record
    record["evaluation_wall_time_seconds"] = perf_counter() - started

    evaluation_payload = _record_mapping(evaluation)
    validation_status = _record_mapping(validation.get("status"))
    metrics = _record_mapping(validation.get("metrics"))
    diagnostics = _record_mapping(validation.get("diagnostics"))
    functional_checks = _record_mapping(validation.get("functional_checks"))
    record["parameter_valid"] = bool(validation_status.get("parameter_valid", True))
    record["simulation_valid"] = bool(
        validation_status.get(
            "simulation_valid", evaluation_payload.get("simulation_valid", False)
        )
    )
    functional_status = str(validation_status.get("functional_status", "not_evaluable"))
    if functional_status not in {"valid", "invalid", "not_evaluable"}:
        functional_status = "not_evaluable"
    record["functional_status"] = functional_status
    record["functional_valid"] = bool(
        validation_status.get("functional_valid", functional_status == "valid")
    )
    for metric_name in _OPAMP_METRICS:
        value = metrics.get(metric_name)
        record[metric_name] = (
            float(value)
            if isinstance(value, (int, float, np.number))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            else None
        )
    record["failure_reasons"] = [
        str(reason) for reason in validation.get("failure_reasons") or []
    ]
    record["failure_reason"] = (
        None if not record["failure_reasons"] else "; ".join(record["failure_reasons"])
    )
    record["measurement_status"] = str(
        diagnostics.get(
            "evaluator_measurement_status",
            evaluation_payload.get("measurement_status", "unavailable"),
        )
    )
    for name in (
        "native_op_valid",
        "decorated_op_valid",
        "dc_preservation_valid",
        "ac_simulation_valid",
        "return_ratio_valid",
    ):
        record[name] = bool(diagnostics.get(name, False))
    for name in (
        "operating_region_valid",
        "feedback_testbench_valid",
        "low_frequency_gain_available",
        "unity_crossing_available",
        "phase_margin_available",
    ):
        record[name] = bool(functional_checks.get(name, False))
    for name in (
        "output_voltage",
        "rail_margin_low",
        "rail_margin_high",
        "feedback_dc_offset_v",
        "max_dc_voltage_difference",
    ):
        value = diagnostics.get(name)
        record[name] = (
            float(value)
            if isinstance(value, (int, float, np.number))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            else None
        )
    record["polarity_status"] = str(diagnostics.get("polarity_status", "unavailable"))
    crossing_count = diagnostics.get("unity_crossing_count", 0)
    record["unity_crossing_count"] = (
        int(crossing_count)
        if isinstance(crossing_count, (int, np.integer))
        and not isinstance(crossing_count, bool)
        else 0
    )
    plateau = diagnostics.get("low_frequency_plateau_spread_db")
    record["low_frequency_plateau_spread_db"] = (
        float(plateau)
        if isinstance(plateau, (int, float, np.number))
        and not isinstance(plateau, bool)
        and math.isfinite(float(plateau))
        else None
    )
    return record


def run_spice_profile(
    runtime: Any,
    sampled_points: Sequence[Mapping[str, Any]],
    *,
    evaluator: Any | None = None,
    functional_validator: Any | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evaluate sampled points and a separate nominal reference point."""

    handler = resolve_profiling_handler(runtime.circuit_type)
    if handler.circuit_type == "OpAmp":
        opamp_evaluator = evaluator or runtime.create_evaluator()
        opamp_validator = functional_validator or runtime.create_functional_validator()
        records = []
        total = len(sampled_points)
        for index, sample in enumerate(sampled_points, start=1):
            records.append(
                _evaluate_opamp_point(
                    sample,
                    runtime,
                    opamp_evaluator,
                    opamp_validator,
                )
            )
            if progress is not None:
                progress(index, total)
        nominal_params = {
            parameter.name: float(parameter.nominal)
            for parameter in runtime.runtime_parameters
        }
        nominal = _evaluate_opamp_point(
            {
                "sample_id": "nominal_reference",
                "params": nominal_params,
                "normalized_params": {},
            },
            runtime,
            opamp_evaluator,
            opamp_validator,
        )
        return records, nominal

    current_mirror_evaluator = evaluator or evaluate_current_mirror
    current_mirror_validator = functional_validator or check_current_mirror_functionality
    config = _functional_config(runtime.adapter)
    records = []
    total = len(sampled_points)
    for index, sample in enumerate(sampled_points, start=1):
        records.append(
            _evaluate_point(
                sample,
                runtime,
                current_mirror_evaluator,
                current_mirror_validator,
                config,
            )
        )
        if progress is not None:
            progress(index, total)

    nominal_params = {
        parameter.name: float(parameter.nominal)
        for parameter in runtime.runtime_parameters
    }
    nominal = _evaluate_point(
        {
            "sample_id": "nominal_reference",
            "params": nominal_params,
            "normalized_params": {},
        },
        runtime,
        current_mirror_evaluator,
        current_mirror_validator,
        config,
    )
    return records, nominal


def _distribution(values: Sequence[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=float)
    result = {"min": float(np.min(array))}
    for quantile in _PERCENTILES:
        result[f"p{int(quantile * 100):02d}"] = float(np.quantile(array, quantile))
    result["max"] = float(np.max(array))
    return result


def _pareto_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result = []
    for index, candidate in enumerate(records):
        error = float(candidate["current_error"])
        resistance = float(candidate["output_resistance"])
        dominated = any(
            other_index != index
            and float(other["current_error"]) <= error
            and float(other["output_resistance"]) >= resistance
            and (
                float(other["current_error"]) < error
                or float(other["output_resistance"]) > resistance
            )
            for other_index, other in enumerate(records)
        )
        if not dominated:
            result.append(candidate)
    return sorted(result, key=lambda item: float(item["current_error"]))


def _joint_target_grid(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    errors = np.asarray([record["current_error"] for record in records], dtype=float)
    resistances = np.asarray(
        [record["output_resistance"] for record in records], dtype=float
    )
    quantiles = np.arange(0.01, 1.0, 0.01)
    error_thresholds = np.quantile(errors, quantiles)
    resistance_thresholds = np.quantile(resistances, quantiles)
    candidates: dict[tuple[float, float], dict[str, Any]] = {}
    for error_quantile, error_target in zip(quantiles, error_thresholds):
        for resistance_quantile, resistance_target in zip(
            quantiles, resistance_thresholds
        ):
            mask = (errors <= error_target) & (resistances >= resistance_target)
            count = int(np.count_nonzero(mask))
            if count == 0:
                continue
            key = (float(error_target), float(resistance_target))
            candidate = {
                "current_error_max": key[0],
                "output_resistance_min": key[1],
                "joint_feasible_count": count,
                "joint_feasible_rate": float(count / len(records)),
                "current_error_quantile": float(error_quantile),
                "output_resistance_quantile": float(resistance_quantile),
                "support_sample_ids": [
                    records[index]["sample_id"]
                    for index in np.flatnonzero(mask)[:10]
                ],
            }
            previous = candidates.get(key)
            if previous is None or abs(
                error_quantile - (1.0 - resistance_quantile)
            ) < abs(
                previous["current_error_quantile"]
                - (1.0 - previous["output_resistance_quantile"])
            ):
                candidates[key] = candidate
    return list(candidates.values())


def _select_target_levels(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    selected = {}
    previous: Mapping[str, Any] | None = None
    for level, limits in _TARGET_LEVELS.items():
        eligible = [
            candidate
            for candidate in candidates
            if limits["minimum"]
            <= float(candidate["joint_feasible_rate"])
            <= limits["maximum"]
            and float(candidate["output_resistance_quantile"])
            >= limits["minimum_resistance_quantile"]
            and (
                previous is None
                or (
                    float(candidate["current_error_max"])
                    <= float(previous["current_error_max"])
                    and float(candidate["output_resistance_min"])
                    >= float(previous["output_resistance_min"])
                )
            )
        ]
        if not eligible:
            selected[level] = None
            continue
        # Within the requested empirical-rate band, prefer the strictest
        # current-accuracy threshold while keeping Rout progressively above a
        # nontrivial distribution quantile.  This prevents a high-Rout target
        # from making current_error effectively unconstrained.
        selected[level] = dict(
            min(
                eligible,
                key=lambda candidate: (
                    float(candidate["current_error_max"]),
                    abs(float(candidate["joint_feasible_rate"]) - limits["center"]),
                    -float(candidate["output_resistance_min"]),
                ),
            )
        )
        previous = selected[level]
    return selected


def _analyze_current_mirror_profile(
    records: Sequence[Mapping[str, Any]],
    nominal_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Compute valid-only distributions, Pareto points, and joint targets."""

    simulation_valid = [record for record in records if record["simulation_valid"]]
    valid = [
        record
        for record in simulation_valid
        if record["functional_valid"] is True
    ]
    if not valid:
        return {
            "analysis_status": "no_functional_valid_samples",
            "total_samples": len(records),
            "simulation_valid_samples": len(simulation_valid),
            "simulation_invalid_samples": len(records) - len(simulation_valid),
            "simulation_valid_rate": float(len(simulation_valid) / len(records)),
            "functional_valid_samples": 0,
            "functional_invalid_samples": len(simulation_valid),
            "functional_valid_rate": 0.0,
            "functional_valid_rate_given_simulation_valid": 0.0,
            "valid_samples": 0,
            "invalid_samples": len(records),
            "valid_rate": 0.0,
            "current_error": None,
            "output_resistance": None,
            "joint_tradeoff": {
                "spearman_correlation": None,
                "p_value": None,
                "interpretation": "unavailable_without_functional_valid_samples",
            },
            "pareto_point_count": 0,
            "representative_pareto_points": [],
            "target_candidates": {"easy": None, "medium": None, "hard": None},
            "target_pool_filter": (
                "simulation_valid == True AND functional_valid == True"
            ),
            "functional_failure_reason_counts": {
                reason: sum(
                    reason in record.get("functional_failure_reasons", [])
                    for record in simulation_valid
                )
                for reason in sorted(
                    {
                        reason
                        for record in simulation_valid
                        for reason in record.get("functional_failure_reasons", [])
                    }
                )
            },
            "target_selection_policy": {
                "primary": "empirical joint feasible-rate bands",
                "difficulty_is_nested": True,
                "minimum_output_resistance_quantiles": {
                    level: limits["minimum_resistance_quantile"]
                    for level, limits in _TARGET_LEVELS.items()
                },
                "tie_break": "strictest current_error threshold",
            },
            "recommended_level": None,
            "nominal_reference": dict(nominal_record),
        }
    errors = [float(record["current_error"]) for record in valid]
    resistances = [float(record["output_resistance"]) for record in valid]
    pareto = _pareto_records(valid)
    candidates = _joint_target_grid(valid)
    targets = _select_target_levels(candidates)
    if np.ptp(errors) == 0.0 or np.ptp(resistances) == 0.0:
        rho, p_value = math.nan, math.nan
    else:
        rho, p_value = spearmanr(errors, resistances)
    correlation = None if not math.isfinite(float(rho)) else float(rho)
    if correlation is None:
        interpretation = "undefined"
    elif correlation >= 0.25:
        interpretation = "tradeoff"
    elif correlation <= -0.25:
        interpretation = "objectives_aligned"
    else:
        interpretation = "weak_or_no_monotonic_relationship"

    representative_pareto = pareto
    if len(pareto) > 10:
        indices = np.linspace(0, len(pareto) - 1, 10, dtype=int)
        representative_pareto = [pareto[index] for index in indices]
    return {
        "total_samples": len(records),
        "simulation_valid_samples": len(simulation_valid),
        "simulation_invalid_samples": len(records) - len(simulation_valid),
        "simulation_valid_rate": float(len(simulation_valid) / len(records)),
        "functional_valid_samples": len(valid),
        "functional_invalid_samples": len(simulation_valid) - len(valid),
        "functional_valid_rate": float(len(valid) / len(records)),
        "functional_valid_rate_given_simulation_valid": float(
            len(valid) / len(simulation_valid)
        ),
        # Compatibility aliases now describe the target-construction pool.
        "valid_samples": len(valid),
        "invalid_samples": len(records) - len(valid),
        "valid_rate": float(len(valid) / len(records)),
        "current_error": _distribution(errors),
        "output_resistance": _distribution(resistances),
        "joint_tradeoff": {
            "spearman_correlation": correlation,
            "p_value": (
                None if not math.isfinite(float(p_value)) else float(p_value)
            ),
            "interpretation": interpretation,
        },
        "pareto_point_count": len(pareto),
        "representative_pareto_points": [
            {
                "sample_id": record["sample_id"],
                "current_error": record["current_error"],
                "output_resistance": record["output_resistance"],
            }
            for record in representative_pareto
        ],
        "target_candidates": targets,
        "target_pool_filter": "simulation_valid == True AND functional_valid == True",
        "functional_failure_reason_counts": {
            reason: sum(
                reason in record.get("functional_failure_reasons", [])
                for record in simulation_valid
            )
            for reason in sorted(
                {
                    reason
                    for record in simulation_valid
                    for reason in record.get("functional_failure_reasons", [])
                }
            )
        },
        "target_selection_policy": {
            "primary": "empirical joint feasible-rate bands",
            "difficulty_is_nested": True,
            "minimum_output_resistance_quantiles": {
                level: limits["minimum_resistance_quantile"]
                for level, limits in _TARGET_LEVELS.items()
            },
            "tie_break": "strictest current_error threshold",
        },
        "recommended_level": "medium" if targets["medium"] is not None else None,
        "nominal_reference": dict(nominal_record),
    }


def _canonical_opamp_reason(reason: str) -> str:
    aliases = {
        "output_outside_supply_rails": "gross_rail_violation",
        "polarity_unresolved": "input_polarity_unresolved",
    }
    return aliases.get(str(reason), str(reason))


def _opamp_failure_counts(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    discovered = {
        _canonical_opamp_reason(reason)
        for record in records
        for reason in record.get("failure_reasons") or []
    }
    keys = sorted(set(_OPAMP_FAILURE_TAXONOMY) | discovered)
    counts = {name: 0 for name in keys}
    for record in records:
        per_record = {
            _canonical_opamp_reason(reason)
            for reason in record.get("failure_reasons") or []
        }
        for reason in per_record:
            counts[reason] += 1
    return counts


def _maximize_pareto_records(
    records: Sequence[Mapping[str, Any]],
    metric_names: Sequence[str],
) -> list[Mapping[str, Any]]:
    values = np.asarray(
        [[float(record[name]) for name in metric_names] for record in records],
        dtype=float,
    )
    result = []
    for index, candidate in enumerate(values):
        dominated = np.any(
            np.all(values >= candidate, axis=1)
            & np.any(values > candidate, axis=1)
        )
        if not dominated:
            result.append(records[index])
    return result


def _compact_opamp_point(
    record: Mapping[str, Any] | None,
    *,
    include_params: bool = False,
) -> dict[str, Any] | None:
    if record is None:
        return None
    result = {"sample_id": record["sample_id"]}
    result.update({name: record.get(name) for name in _OPAMP_METRICS})
    if include_params:
        result["params"] = dict(record.get("params") or {})
    return result


def _metric_matrix(records: Sequence[Mapping[str, Any]]) -> np.ndarray:
    matrix = np.asarray(
        [[float(record[name]) for name in _OPAMP_METRICS] for record in records],
        dtype=float,
    )
    matrix[:, 1] = np.log10(matrix[:, 1])
    return matrix


def _normalize_columns(matrix: np.ndarray) -> np.ndarray:
    lower = np.min(matrix, axis=0)
    spread = np.ptp(matrix, axis=0)
    return np.divide(
        matrix - lower,
        spread,
        out=np.full_like(matrix, 0.5),
        where=spread > 0.0,
    )


def _correlation_result(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    if len(x) < 2 or np.ptp(x) == 0.0 or np.ptp(y) == 0.0:
        pearson = None
        spearman = None
        p_value = None
    else:
        pearson_value = float(np.corrcoef(x, y)[0, 1])
        spearman_value, p_value_raw = spearmanr(x, y)
        pearson = pearson_value if math.isfinite(pearson_value) else None
        spearman = (
            float(spearman_value) if math.isfinite(float(spearman_value)) else None
        )
        p_value = float(p_value_raw) if math.isfinite(float(p_value_raw)) else None
    if spearman is None:
        interpretation = "insufficient_or_constant_data"
    elif spearman <= -0.70:
        interpretation = "strong_tradeoff"
    elif spearman <= -0.40:
        interpretation = "moderate_tradeoff"
    elif spearman >= 0.70:
        interpretation = "strong_positive_association"
    elif spearman >= 0.40:
        interpretation = "moderate_positive_association"
    else:
        interpretation = "weak_or_no_clear_tradeoff"
    return {
        "pearson_correlation": pearson,
        "spearman_correlation": spearman,
        "spearman_p_value": p_value,
        "interpretation": interpretation,
        "interpretation_rule": (
            "Spearman rho: <=-0.70 strong tradeoff; <=-0.40 moderate tradeoff; "
            "abs(rho)<0.40 weak/no clear tradeoff; positive thresholds denote "
            "association, not causation"
        ),
    }


def _opamp_correlations(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            name: _correlation_result(np.asarray([]), np.asarray([]))
            for name in ("aol_vs_ugf", "aol_vs_pm", "ugf_vs_pm")
        }
    matrix = _metric_matrix(records)
    pairs = {
        "aol_vs_ugf": (matrix[:, 0], matrix[:, 1], "log10_hz"),
        "aol_vs_pm": (matrix[:, 0], matrix[:, 2], "identity"),
        "ugf_vs_pm": (matrix[:, 1], matrix[:, 2], "log10_hz"),
    }
    result = {}
    for name, (first, second, ugf_transform) in pairs.items():
        result[name] = {
            **_correlation_result(first, second),
            "ugf_transform": ugf_transform,
            "population": "functional_valid",
        }
    return result


def _candidate_threshold_support(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not records:
        return []
    arrays = {
        name: np.asarray([float(record[name]) for record in records], dtype=float)
        for name in _OPAMP_METRICS
    }
    quantiles = {
        "low_frequency_open_loop_gain_db": (0.50, 0.75, 0.90),
        "unity_gain_frequency_hz": (0.50, 0.75, 0.90),
        "phase_margin_deg": (0.25, 0.50, 0.75),
    }
    thresholds = {
        name: {quantile: float(np.quantile(arrays[name], quantile)) for quantile in levels}
        for name, levels in quantiles.items()
    }
    result = []
    for aol_quantile in quantiles[_OPAMP_METRICS[0]]:
        for ugf_quantile in quantiles[_OPAMP_METRICS[1]]:
            for pm_quantile in quantiles[_OPAMP_METRICS[2]]:
                aol = thresholds[_OPAMP_METRICS[0]][aol_quantile]
                ugf = thresholds[_OPAMP_METRICS[1]][ugf_quantile]
                pm = thresholds[_OPAMP_METRICS[2]][pm_quantile]
                mask = (
                    (arrays[_OPAMP_METRICS[0]] >= aol)
                    & (arrays[_OPAMP_METRICS[1]] >= ugf)
                    & (arrays[_OPAMP_METRICS[2]] >= pm)
                )
                support_indices = np.flatnonzero(mask)
                result.append(
                    {
                        "aol_quantile": f"p{int(aol_quantile * 100)}",
                        "aol_threshold_db": aol,
                        "ugf_quantile": f"p{int(ugf_quantile * 100)}",
                        "ugf_threshold_hz": ugf,
                        "pm_quantile": f"p{int(pm_quantile * 100)}",
                        "pm_threshold_deg": pm,
                        "joint_support_count": int(len(support_indices)),
                        "joint_support_rate": float(len(support_indices) / len(records)),
                        "support_sample_ids": [
                            records[index]["sample_id"] for index in support_indices[:10]
                        ],
                    }
                )
    return result


def _crossing_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"exactly_one": 0, "multiple": 0, "none": 0}
    for record in records:
        value = int(record.get("unity_crossing_count") or 0)
        if value == 1:
            counts["exactly_one"] += 1
        elif value > 1:
            counts["multiple"] += 1
        else:
            counts["none"] += 1
    return counts


def _plateau_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [
        float(record["low_frequency_plateau_spread_db"])
        for record in records
        if record.get("low_frequency_plateau_spread_db") is not None
    ]
    return {
        "plateau_valid_count": len(values),
        "plateau_unavailable_count": len(records) - len(values),
        "spread_db": _distribution(values) if values else None,
    }


def _opamp_representatives(
    records: Sequence[Mapping[str, Any]],
    nominal_record: Mapping[str, Any],
    pareto: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not records:
        return {
            "highest_aol": None,
            "highest_ugf": None,
            "highest_pm": None,
            "balanced_pareto": None,
            "closest_to_nominal": None,
            "balanced_rule": "unavailable_without_functional_valid_samples",
            "distance_rule": "unavailable_without_functional_valid_samples",
        }
    highest = {
        "highest_aol": max(records, key=lambda item: float(item[_OPAMP_METRICS[0]])),
        "highest_ugf": max(records, key=lambda item: float(item[_OPAMP_METRICS[1]])),
        "highest_pm": max(records, key=lambda item: float(item[_OPAMP_METRICS[2]])),
    }
    pareto_matrix = _normalize_columns(_metric_matrix(pareto))
    balance_scores = np.min(pareto_matrix, axis=1)
    balanced = pareto[int(np.argmax(balance_scores))]

    nominal_metrics_available = all(
        isinstance(nominal_record.get(name), (int, float, np.number))
        and not isinstance(nominal_record.get(name), bool)
        and math.isfinite(float(nominal_record[name]))
        for name in _OPAMP_METRICS
    )
    closest = None
    if nominal_metrics_available:
        sample_matrix = _metric_matrix(records)
        combined = np.vstack(
            [
                sample_matrix,
                np.asarray(
                    [[float(nominal_record[name]) for name in _OPAMP_METRICS]],
                    dtype=float,
                ),
            ]
        )
        combined[-1, 1] = math.log10(combined[-1, 1])
        normalized = _normalize_columns(combined)
        distances = np.linalg.norm(normalized[:-1] - normalized[-1], axis=1)
        closest = records[int(np.argmin(distances))]
    return {
        **{name: _compact_opamp_point(record, include_params=True) for name, record in highest.items()},
        "balanced_pareto": _compact_opamp_point(balanced, include_params=True),
        "closest_to_nominal": _compact_opamp_point(closest, include_params=True),
        "balanced_rule": (
            "maximize the minimum min-max-normalized objective over the Pareto set; "
            "UGF is transformed with log10 before normalization"
        ),
        "distance_rule": (
            "minimum Euclidean distance to nominal in dataset min-max-normalized "
            "[Aol_dB, log10(UGF_Hz), PM_deg] space; unavailable if nominal metrics are missing"
        ),
    }


def _focused_profiling_decision(
    total: int,
    functional_count: int,
    pareto_count: int,
    high_quantile_support: int,
) -> tuple[str, list[str]]:
    reasons = []
    if functional_count < max(50, math.ceil(0.20 * total)):
        reasons.append("functional-valid coverage is below 50 points or 20%")
    if pareto_count < 4:
        reasons.append("the three-objective Pareto set has fewer than 4 points")
    if high_quantile_support <= 2:
        reasons.append("joint P75/P75/P75 support has at most 2 points")
    if reasons:
        return "needed", reasons

    uncertain = []
    if functional_count < max(120, math.ceil(0.40 * total)):
        uncertain.append("functional-valid coverage is below 120 points or 40%")
    if pareto_count < 8:
        uncertain.append("the three-objective Pareto set has fewer than 8 points")
    if high_quantile_support < 8:
        uncertain.append("joint P75/P75/P75 support has fewer than 8 points")
    if uncertain:
        return "uncertain", uncertain
    return "not_needed", [
        "functional-valid, Pareto, and joint P75/P75/P75 coverage meet the declared global-profile sufficiency rule"
    ]


def _analyze_opamp_profile(
    records: Sequence[Mapping[str, Any]],
    nominal_record: Mapping[str, Any],
    *,
    total_wall_time_seconds: float | None = None,
) -> dict[str, Any]:
    total = len(records)
    parameter_valid = [record for record in records if record.get("parameter_valid")]
    simulation_valid = [record for record in records if record.get("simulation_valid")]
    functional_valid = [
        record
        for record in records
        if record.get("functional_valid") is True
        and all(record.get(name) is not None for name in _OPAMP_METRICS)
    ]
    invalid = [record for record in records if record.get("functional_status") == "invalid"]
    not_evaluable = [
        record for record in records if record.get("functional_status") == "not_evaluable"
    ]
    wall_time = (
        float(total_wall_time_seconds)
        if total_wall_time_seconds is not None
        else float(sum(float(record.get("evaluation_wall_time_seconds") or 0.0) for record in records))
    )
    distributions = {
        name: (
            _distribution([float(record[name]) for record in functional_valid])
            if functional_valid
            else None
        )
        for name in _OPAMP_METRICS
    }
    nominal_percentiles = {}
    for name in _OPAMP_METRICS:
        nominal_value = nominal_record.get(name)
        if functional_valid and nominal_value is not None:
            values = np.asarray([float(record[name]) for record in functional_valid])
            nominal_percentiles[name] = float(
                100.0 * np.count_nonzero(values <= float(nominal_value)) / len(values)
            )
        else:
            nominal_percentiles[name] = None

    pareto = _maximize_pareto_records(functional_valid, _OPAMP_METRICS) if functional_valid else []
    representatives = _opamp_representatives(functional_valid, nominal_record, pareto)
    support = _candidate_threshold_support(functional_valid)
    high_support = next(
        (
            int(item["joint_support_count"])
            for item in support
            if item["aol_quantile"] == "p75"
            and item["ugf_quantile"] == "p75"
            and item["pm_quantile"] == "p75"
        ),
        0,
    )
    focused, focused_reasons = _focused_profiling_decision(
        total, len(functional_valid), len(pareto), high_support
    )
    anchors = []
    if focused != "not_needed":
        candidates = [
            ("nominal", nominal_record),
            ("highest_aol_pareto", max(pareto, key=lambda item: float(item[_OPAMP_METRICS[0]])) if pareto else None),
            ("highest_ugf_pareto", max(pareto, key=lambda item: float(item[_OPAMP_METRICS[1]])) if pareto else None),
            ("balanced_pareto", next((item for item in pareto if representatives["balanced_pareto"] and item["sample_id"] == representatives["balanced_pareto"]["sample_id"]), None)),
        ]
        seen = set()
        for role, record in candidates:
            if record is None or record["sample_id"] in seen:
                continue
            seen.add(record["sample_id"])
            anchors.append({"role": role, **(_compact_opamp_point(record, include_params=True) or {})})

    pm_values = np.asarray(
        [float(record["phase_margin_deg"]) for record in functional_valid], dtype=float
    )
    pm_bins = {
        "less_than_0_deg": int(np.count_nonzero(pm_values < 0.0)),
        "0_to_less_than_30_deg": int(np.count_nonzero((pm_values >= 0.0) & (pm_values < 30.0))),
        "30_to_less_than_60_deg": int(np.count_nonzero((pm_values >= 30.0) & (pm_values < 60.0))),
        "60_to_90_deg_inclusive": int(np.count_nonzero((pm_values >= 60.0) & (pm_values <= 90.0))),
        "greater_than_90_deg": int(np.count_nonzero(pm_values > 90.0)),
    }
    return {
        "analysis_status": "complete" if functional_valid else "no_functional_valid_samples",
        "total_samples": total,
        "parameter_valid_count": len(parameter_valid),
        "parameter_valid_rate": float(len(parameter_valid) / total) if total else 0.0,
        "simulation_valid_count": len(simulation_valid),
        "simulation_valid_samples": len(simulation_valid),
        "simulation_invalid_samples": total - len(simulation_valid),
        "simulation_valid_rate": float(len(simulation_valid) / total) if total else 0.0,
        "functional_valid_count": len(functional_valid),
        "functional_valid_samples": len(functional_valid),
        "functional_valid_rate": float(len(functional_valid) / total) if total else 0.0,
        "invalid_count": len(invalid),
        "not_evaluable_count": len(not_evaluable),
        "functional_status_counts": {
            "valid": len(functional_valid),
            "invalid": len(invalid),
            "not_evaluable": len(not_evaluable),
        },
        "failure_reason_counts": _opamp_failure_counts(records),
        "metric_distributions": distributions,
        **distributions,
        "nominal_reference": dict(nominal_record),
        "nominal_empirical_percentile": nominal_percentiles,
        "nominal_percentile_definition": (
            "100 * count(functional-valid sampled metric <= nominal metric) / functional-valid count"
        ),
        "correlations": _opamp_correlations(functional_valid),
        "correlation_note": "Descriptive association only; no causal interpretation.",
        "pareto_objectives": [f"maximize {name}" for name in _OPAMP_METRICS],
        "pareto_point_count": len(pareto),
        "pareto_points": [
            _compact_opamp_point(record, include_params=True) for record in pareto
        ],
        "representative_points": representatives,
        "phase_margin_bins": pm_bins,
        "unity_crossing_diagnostics": {
            "functional_valid": _crossing_counts(functional_valid),
            "parameter_valid": _crossing_counts(parameter_valid),
        },
        "low_frequency_plateau_diagnostics": {
            "functional_valid": _plateau_counts(functional_valid),
            "parameter_valid": _plateau_counts(parameter_valid),
        },
        "candidate_threshold_support": support,
        "candidate_threshold_note": (
            "All thresholds are empirical profiling quantiles; no engineering target is selected."
        ),
        "focused_profiling_needed": focused,
        "focused_profiling_decision_reasons": focused_reasons,
        "focused_profiling_decision_rule": {
            "needed": "functional-valid < max(50,20%), Pareto <4, or joint P75/P75/P75 <=2",
            "uncertain": "functional-valid < max(120,40%), Pareto <8, or joint P75/P75/P75 <8",
            "not_needed": "none of the needed/uncertain conditions apply",
        },
        "focused_anchor_candidates": anchors,
        "total_profiling_wall_time_seconds": wall_time,
        "average_seconds_per_requested_sample": (wall_time / total if total else None),
        "performance_target_selected": False,
    }


def analyze_profile(
    records: Sequence[Mapping[str, Any]],
    nominal_record: Mapping[str, Any],
    *,
    circuit_type: str = "CurrentMirror",
    total_wall_time_seconds: float | None = None,
) -> dict[str, Any]:
    """Dispatch shared profiling records to circuit-specific summary logic."""

    handler = resolve_profiling_handler(circuit_type)
    if handler.circuit_type == "OpAmp":
        return _analyze_opamp_profile(
            records,
            nominal_record,
            total_wall_time_seconds=total_wall_time_seconds,
        )
    return _analyze_current_mirror_profile(records, nominal_record)


def _write_current_mirror_csv(
    path: Path,
    records: Sequence[Mapping[str, Any]],
    parameter_names: Sequence[str],
) -> None:
    fieldnames = [
        "sample_id",
        "parameter_valid",
        "simulation_valid",
        "functional_valid",
        "functional_failure_reasons",
        "current_error",
        "output_resistance",
        "compliance_relative_variation",
        "tracking_max_error",
        "compliance_quality_smoke_passed",
        "tracking_quality_smoke_passed",
        "compliance_output_branch_alive",
        "reference_responsive",
        "failure_reason",
        *(f"param_{name}" for name in parameter_names),
        *(f"normalized_{name}" for name in parameter_names),
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {name: record.get(name) for name in fieldnames}
            row["functional_failure_reasons"] = ";".join(
                record.get("functional_failure_reasons") or []
            )
            for name in parameter_names:
                row[f"param_{name}"] = record["params"].get(name)
                row[f"normalized_{name}"] = record["normalized_params"].get(name)
            writer.writerow(row)


def _write_opamp_csv(
    path: Path,
    records: Sequence[Mapping[str, Any]],
    parameter_names: Sequence[str],
) -> None:
    fields = [
        "sample_id",
        "parameter_valid",
        "simulation_valid",
        "functional_valid",
        "functional_status",
        *_OPAMP_METRICS,
        "failure_reasons",
        "native_op_valid",
        "decorated_op_valid",
        "dc_preservation_valid",
        "ac_simulation_valid",
        "polarity_status",
        "return_ratio_valid",
        "unity_crossing_count",
        "low_frequency_plateau_spread_db",
        "measurement_status",
        "evaluation_wall_time_seconds",
        "failure_reason",
        *(f"param_{name}" for name in parameter_names),
        *(f"normalized_{name}" for name in parameter_names),
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = {name: record.get(name) for name in fields}
            row["failure_reasons"] = ";".join(record.get("failure_reasons") or [])
            for name in parameter_names:
                row[f"param_{name}"] = record["params"].get(name)
                row[f"normalized_{name}"] = record["normalized_params"].get(name)
            writer.writerow(row)


def save_profile_artifacts(
    runtime: Any,
    records: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Any],
    *,
    sampler: str,
    seed: int,
    functional_validator_note: str | None = None,
    output_root: str | Path = _DEFAULT_OUTPUT_ROOT,
) -> tuple[Path, Path]:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    stem = f"{runtime.circuit_id}_profiling_{run_id}"
    csv_path = root / f"{stem}.csv"
    summary_path = root / f"{stem}_summary.json"
    space = _parameter_space(runtime)
    parameter_names = [parameter["name"] for parameter in space]
    handler = resolve_profiling_handler(runtime.circuit_type)
    if handler.circuit_type == "OpAmp":
        _write_opamp_csv(csv_path, records, parameter_names)
    else:
        _write_current_mirror_csv(csv_path, records, parameter_names)
    payload = {
        "schema_version": handler.schema_version,
        "run_id": run_id,
        "circuit_id": runtime.circuit_id,
        "circuit_type": runtime.circuit_type,
        "parameter_dimension": len(space),
        "sampler": sampler,
        "seed": int(seed),
        "requested_samples": len(records),
        "sample_generation": {
            "source": "Stage A/B AutoConfig runtime_parameters",
            "requested_samples_preserved": True,
            "sobol_policy": (
                "generate the smallest power-of-two Sobol block covering the request, then retain the first requested samples"
                if str(sampler).casefold() == "sobol"
                else "seeded NumPy random sampling"
            ),
        },
        "parameter_space": space,
        "testbench_contract": dict(runtime.testbench_config),
        "included_in_d0": False,
        "included_in_dt": False,
        "included_in_retrieval": False,
        "included_in_llm_context": False,
        **dict(analysis),
        "artifacts": {"csv": str(csv_path.resolve())},
    }
    if functional_validator_note:
        payload["functional_validator_note"] = str(functional_validator_note)
    summary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return csv_path.resolve(), summary_path.resolve()


def _print_current_mirror_summary(
    summary: Mapping[str, Any], csv_path: Path, json_path: Path
) -> None:
    print("SPICE Profiling")
    print(f"circuit_id: {summary['circuit_id']}")
    print(f"parameters: {len(summary['parameter_space'])}")
    print(f"sampler: {summary['sampler']}")
    print(f"requested_samples: {summary['requested_samples']}")
    print(f"simulation_valid: {summary['simulation_valid_samples']}")
    print(f"simulation_invalid: {summary['simulation_invalid_samples']}")
    print(f"simulation_valid_rate: {summary['simulation_valid_rate']:.2%}")
    print(f"functional_valid: {summary['functional_valid_samples']}")
    print(f"functional_invalid: {summary['functional_invalid_samples']}")
    print(f"functional_valid_rate: {summary['functional_valid_rate']:.2%}")
    print(f"target_pool: {summary['valid_samples']}")
    for metric in ("current_error", "output_resistance"):
        distribution = summary[metric]
        if distribution is None:
            print(f"{metric}: unavailable (no functional-valid samples)")
            continue
        print(f"{metric}:")
        print(f"  best: {distribution['min'] if metric == 'current_error' else distribution['max']:.12g}")
        print(f"  p10: {distribution['p10']:.12g}")
        print(f"  median: {distribution['p50']:.12g}")
        print(f"  p90: {distribution['p90']:.12g}")
    print(f"Pareto points: {summary['pareto_point_count']}")
    print("Candidate targets:")
    for level in ("easy", "medium", "hard"):
        target = summary["target_candidates"][level]
        print(f"  {level.title()}:")
        if target is None:
            print("    unavailable from empirical samples")
            continue
        print(f"    current_error <= {target['current_error_max']:.12g}")
        print(f"    output_resistance >= {target['output_resistance_min']:.12g}")
        print(
            "    joint feasible: "
            f"{target['joint_feasible_count']} / {summary['valid_samples']} "
            f"({target['joint_feasible_rate']:.2%})"
        )
    recommended = summary["target_candidates"].get("medium")
    print("Recommended benchmark:")
    if recommended is None:
        print("  no empirically supported Medium target")
    else:
        print(f"  current_error <= {recommended['current_error_max']:.12g}")
        print(f"  output_resistance >= {recommended['output_resistance_min']:.12g}")
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")


def _print_opamp_summary(
    summary: Mapping[str, Any], csv_path: Path, json_path: Path
) -> None:
    print("SPICE Profiling")
    print(f"circuit_id: {summary['circuit_id']}")
    print(f"parameters: {summary['parameter_dimension']}")
    print(f"sampler: {summary['sampler']}")
    print(f"requested_samples: {summary['requested_samples']}")
    print(f"parameter_valid: {summary['parameter_valid_count']}")
    print(f"simulation_valid: {summary['simulation_valid_count']}")
    print(f"functional_valid: {summary['functional_valid_count']}")
    print(f"invalid: {summary['invalid_count']}")
    print(f"not_evaluable: {summary['not_evaluable_count']}")
    for metric in _OPAMP_METRICS:
        distribution = summary["metric_distributions"][metric]
        print(f"{metric}:")
        if distribution is None:
            print("  unavailable (no functional-valid samples)")
            continue
        print(
            "  "
            + ", ".join(
                f"{name}={distribution[name]:.12g}"
                for name in ("min", "p10", "p25", "p50", "p75", "p90", "p95", "max")
            )
        )
    print(f"Pareto points: {summary['pareto_point_count']}")
    print(f"focused_profiling_needed: {summary['focused_profiling_needed']}")
    print(f"wall_time_seconds: {summary['total_profiling_wall_time_seconds']:.6f}")
    average = summary.get("average_seconds_per_requested_sample")
    if average is not None:
        print(f"average_seconds_per_sample: {average:.6f}")
    print("No final performance target was selected.")
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")


def _print_summary(summary: Mapping[str, Any], csv_path: Path, json_path: Path) -> None:
    handler = resolve_profiling_handler(str(summary["circuit_type"]))
    if handler.circuit_type == "OpAmp":
        _print_opamp_summary(summary, csv_path, json_path)
    else:
        _print_current_mirror_summary(summary, csv_path, json_path)


def main(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Profile an AutoConfig parameter space with real SPICE"
    )
    parser.add_argument("--circuit-type", required=True)
    parser.add_argument("--circuit-id", required=True)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--sampler", choices=("sobol", "random"), default="sobol")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--functional-validator-note")
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_ROOT))
    args = parser.parse_args(argv)

    runtime = build_active_learning_circuit_config(
        args.circuit_type,
        args.circuit_id,
        repository_root=_REPOSITORY_ROOT,
    )
    samples = generate_parameter_samples(
        runtime,
        samples=args.samples,
        sampler=args.sampler,
        seed=args.seed,
    )
    report_every = max(1, args.samples // 10)

    def progress(completed: int, total: int) -> None:
        if completed == total or completed % report_every == 0:
            print(f"profiling progress: {completed}/{total}", flush=True)

    profiling_started = perf_counter()
    records, nominal = run_spice_profile(runtime, samples, progress=progress)
    profiling_wall_time = perf_counter() - profiling_started
    analysis = analyze_profile(
        records,
        nominal,
        circuit_type=runtime.circuit_type,
        total_wall_time_seconds=profiling_wall_time,
    )
    csv_path, json_path = save_profile_artifacts(
        runtime,
        records,
        analysis,
        sampler=args.sampler,
        seed=args.seed,
        functional_validator_note=args.functional_validator_note,
        output_root=args.output_dir,
    )
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    _print_summary(summary, csv_path, json_path)
    return summary


if __name__ == "__main__":
    main()
