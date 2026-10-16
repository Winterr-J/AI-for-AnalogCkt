"""Generic V1 functional feasibility checks for current mirrors."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from ActiveLearning.InitializeD0.evaluator.current_mirror_evaluator import (
    CurrentMeasurement,
)
from .excitation_perturbation_policy import (
    ExcitationPerturbationPolicy,
    resolve_excitation_perturbation_policy,
)
from ActiveLearning.errors import ReferenceStimulusUnavailableError
from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice


__all__ = ["check_current_mirror_functionality"]

_LOGGER = logging.getLogger(__name__)
_CURRENT_EPSILON_A = 1.0e-18

# Diagnostic quality-smoke defaults.  They do not define basic functionality.
_PROVISIONAL_COMPLIANCE_QUALITY_TOLERANCE = 0.05
_PROVISIONAL_TRACKING_QUALITY_TOLERANCE = 0.05

# A functional branch may lose a small number of edge-of-sweep points, but it
# must carry meaningful current through most of the requested operating range.
_MIN_COMPLIANCE_ALIVE_FRACTION = 0.80
_RESPONSE_NUMERICAL_RELATIVE_TOLERANCE = 1.0e-6
_STIMULUS_APPLICATION_RELATIVE_TOLERANCE = 1.0e-6


def _validate_settings(
    compliance_vout_start: Any,
    compliance_vout_stop: Any,
    compliance_points: Any,
    compliance_tolerance: Any,
    reference_scales: Sequence[float],
    tracking_tolerance: Any,
) -> tuple[float, float, int, float, tuple[float, ...], float]:
    start = float(compliance_vout_start)
    stop = float(compliance_vout_stop)
    if not stop > start:
        raise ValueError("compliance_vout_stop must be greater than compliance_vout_start")
    if isinstance(compliance_points, bool) or not isinstance(
        compliance_points, (int, np.integer)
    ):
        raise TypeError("compliance_points must be an integer")
    if compliance_points < 2:
        raise ValueError("compliance_points must be at least 2")

    compliance_limit = float(compliance_tolerance)
    tracking_limit = float(tracking_tolerance)
    if not compliance_limit >= 0.0:
        raise ValueError("compliance_tolerance must be non-negative")
    if not tracking_limit >= 0.0:
        raise ValueError("tracking_tolerance must be non-negative")

    scales = tuple(float(scale) for scale in reference_scales)
    if not scales:
        raise ValueError("reference_scales must not be empty")
    if any(not scale > 0.0 for scale in scales):
        raise ValueError("reference_scales must contain only positive values")
    return (
        start,
        stop,
        int(compliance_points),
        compliance_limit,
        scales,
        tracking_limit,
    )


def _measurement_vector(analysis: Any, measurement: CurrentMeasurement) -> np.ndarray:
    mapping = getattr(analysis, measurement.section)
    target = measurement.name.casefold()
    try:
        item = next(
            value for key, value in mapping.items() if str(key).casefold() == target
        )
    except StopIteration as error:
        raise KeyError(f"required simulator current {measurement.name!r} is unavailable") from error
    values = np.asarray(item, dtype=float).reshape(-1)
    if not np.all(np.isfinite(values)):
        raise ValueError(f"required simulator current {measurement.name!r} is not finite")
    return values


def _measure_op_currents(
    circuit: Any,
    adapter: Any,
    temperature: float,
) -> tuple[float, float]:
    simulator = circuit.simulator(
        temperature=temperature,
        nominal_temperature=temperature,
    )
    internal_names = tuple(
        dict.fromkeys(
            measurement.name
            for measurement in (
                adapter.op_reference_current,
                adapter.op_output_current,
            )
            if measurement.section == "internal_parameters"
        )
    )
    if internal_names:
        simulator.save_internal_parameters(*internal_names)
    analysis = simulator.operating_point()
    currents = []
    for measurement in (adapter.op_reference_current, adapter.op_output_current):
        values = _measurement_vector(analysis, measurement)
        if values.size != 1:
            raise ValueError(f"required simulator current {measurement.name!r} is not scalar")
        currents.append(float(values[0]))
    return currents[0], currents[1]


def _meaningful_current(value: float) -> bool:
    return math.isfinite(value) and abs(value) > _CURRENT_EPSILON_A


def _run_basic_nominal_test(
    params: Mapping[str, Any],
    adapter: Any,
    nominal_vout: float,
    mirror_ratio: float,
    temperature: float,
) -> dict[str, Any]:
    circuit = adapter.build_circuit(params, nominal_vout)
    iref_signed, iout_signed = _measure_op_currents(circuit, adapter, temperature)
    reference_valid = _meaningful_current(iref_signed)
    output_valid = _meaningful_current(iout_signed)
    observed_ratio = None
    if reference_valid and output_valid:
        observed_ratio = float(abs(iout_signed) / (mirror_ratio * abs(iref_signed)))
        if not math.isfinite(observed_ratio) or observed_ratio <= 0.0:
            output_valid = False
            observed_ratio = None
    return {
        "reference_current_valid": reference_valid,
        "output_current_valid": output_valid,
        "reference_current": float(iref_signed),
        "output_current": float(iout_signed),
        "observed_ratio_to_expected": observed_ratio,
    }


def _run_compliance_test(
    params: Mapping[str, Any],
    adapter: Any,
    start: float,
    stop: float,
    points: int,
    tolerance: float,
    nominal_vout: float,
    temperature: float,
) -> dict[str, Any]:
    circuit = adapter.build_circuit(params, nominal_vout)
    _iref, nominal_iout_signed = _measure_op_currents(circuit, adapter, temperature)
    nominal_iout = abs(nominal_iout_signed)
    if nominal_iout <= _CURRENT_EPSILON_A:
        raise ValueError("compliance test has near-zero nominal IOUT")

    step = (stop - start) / (points - 1)
    analysis = circuit.simulator(
        temperature=temperature,
        nominal_temperature=temperature,
    ).dc(**{adapter.dc_sweep_source: slice(start, stop, step)})
    vout = np.asarray(analysis.sweep, dtype=float).reshape(-1)
    iout = _measurement_vector(analysis, adapter.dc_output_current)
    if len(vout) != points or len(iout) != points:
        raise ValueError(f"compliance test expected {points} aligned DC points")
    if not np.all(np.isfinite(vout)) or np.any(np.diff(vout) <= 0.0):
        raise ValueError("compliance VOUT data is invalid")

    iout = np.abs(iout)
    alive = iout > _CURRENT_EPSILON_A
    alive_fraction = float(np.count_nonzero(alive) / len(iout))
    output_branch_alive = alive_fraction >= _MIN_COMPLIANCE_ALIVE_FRACTION
    relative_variation = float(np.max(np.abs(iout - nominal_iout) / nominal_iout))
    if not math.isfinite(relative_variation):
        raise ValueError("compliance variation is not finite")
    return {
        # ``passed`` is retained as a compatibility alias for the diagnostic
        # quality smoke.  It is deliberately not a functional gate.
        "passed": relative_variation <= tolerance,
        "quality_smoke_passed": relative_variation <= tolerance,
        "relative_variation": relative_variation,
        "output_branch_alive": output_branch_alive,
        "alive_fraction": alive_fraction,
    }


def _responsive_increasing(values: Sequence[float]) -> bool:
    array = np.asarray(values, dtype=float)
    if array.size < 2 or not np.all(np.isfinite(array)):
        return False
    tolerance = max(
        _CURRENT_EPSILON_A,
        float(np.max(np.abs(array))) * _RESPONSE_NUMERICAL_RELATIVE_TOLERANCE,
    )
    return bool(
        array[-1] - array[0] > tolerance
        and np.all(np.diff(array) >= -tolerance)
    )


def _has_meaningful_response(values: Sequence[float]) -> bool:
    """Detect a response above the existing numerical-noise tolerance."""

    array = np.asarray(values, dtype=float)
    if array.size < 2 or not np.all(np.isfinite(array)):
        return False
    tolerance = max(
        _CURRENT_EPSILON_A,
        float(np.max(np.abs(array))) * _RESPONSE_NUMERICAL_RELATIVE_TOLERANCE,
    )
    return bool(float(np.max(array) - np.min(array)) > tolerance)


def _finalize_tracking_result(
    measurements: Sequence[Mapping[str, Any]],
    errors: Sequence[float],
    tolerance: float,
    *,
    operating_region_valid: bool,
    operating_region_reason: str | None,
) -> dict[str, Any]:
    ordered = sorted(measurements, key=lambda item: item["scale"])
    measured_reference = [item["measured_reference"] for item in ordered]
    measured_output = [item["measured_output"] for item in ordered]
    currents_available = all(
        value is not None
        for value in (*measured_reference, *measured_output)
    )
    reference_responsive = bool(
        operating_region_valid
        and currents_available
        and _responsive_increasing(measured_reference)
    )
    output_responsive = bool(
        operating_region_valid
        and currents_available
        and _responsive_increasing(measured_output)
    )

    requested_excitation_values = [item["requested_excitation"] for item in ordered]
    if all(value is not None for value in requested_excitation_values):
        requested_responsive = _responsive_increasing(requested_excitation_values)
        applied_values = [item["applied_excitation"] for item in ordered]
        if all(value is not None for value in applied_values):
            stimulus_matches = all(
                math.isclose(
                    item["applied_excitation"],
                    item["requested_excitation"],
                    rel_tol=_STIMULUS_APPLICATION_RELATIVE_TOLERANCE,
                    abs_tol=_CURRENT_EPSILON_A,
                )
                for item in ordered
            )
        else:
            direct_reference_values = [
                item["requested_reference"] for item in ordered
            ]
            stimulus_matches = all(
                value is not None
                and item["measured_reference"] is not None
                and math.isclose(
                    item["measured_reference"],
                    value,
                    rel_tol=_STIMULUS_APPLICATION_RELATIVE_TOLERANCE,
                    abs_tol=_CURRENT_EPSILON_A,
                )
                for item, value in zip(ordered, direct_reference_values)
            )
        direct_reference_matches = all(
            item["requested_reference"] is None
            or (
                item["measured_reference"] is not None
                and math.isclose(
                    item["measured_reference"],
                    item["requested_reference"],
                    rel_tol=_STIMULUS_APPLICATION_RELATIVE_TOLERANCE,
                    abs_tol=_CURRENT_EPSILON_A,
                )
            )
            for item in ordered
        )
        reference_stimulus_applied = (
            (requested_responsive or not operating_region_valid)
            and stimulus_matches
            and direct_reference_matches
        )
    else:
        # Compatibility for legacy adapters that only expose parameter scaling.
        reference_stimulus_applied = reference_responsive

    reference_response_valid = (
        operating_region_valid
        and reference_stimulus_applied
        and reference_responsive
        and output_responsive
    )
    max_error = max(errors) if errors else None
    quality_smoke_passed = bool(
        max_error is not None and max_error <= tolerance
    )
    return {
        # ``passed`` is retained as a compatibility alias for the diagnostic
        # quality smoke.  Functional response is reported separately.
        "passed": quality_smoke_passed,
        "quality_smoke_passed": quality_smoke_passed,
        "max_tracking_error": max_error,
        "responsive": reference_response_valid,
        "reference_stimulus_applied": reference_stimulus_applied,
        "reference_response_valid": reference_response_valid,
        "reference_responsive": reference_responsive,
        "output_responsive": output_responsive,
        "operating_region_valid": operating_region_valid,
        "operating_region_reason": operating_region_reason,
        "measurements": [
            {
                **item,
                # Compatibility aliases for existing diagnostics.
                "reference_current_magnitude": item["measured_reference"],
                "output_current_magnitude": item["measured_output"],
            }
            for item in ordered
        ],
    }


def _run_reference_tracking_test(
    params: Mapping[str, Any],
    adapter: Any,
    reference_scales: tuple[float, ...],
    tolerance: float,
    nominal_vout: float,
    mirror_ratio: float,
    temperature: float,
) -> dict[str, Any]:
    errors = []
    measurements = []
    for scale in reference_scales:
        requested_reference = None
        requested_excitation = None
        applied_excitation = None
        excitation_element = None
        nominal_excitation = None
        if hasattr(adapter, "prepare_reference_stimulus"):
            prepared = adapter.prepare_reference_stimulus(params, scale)
            scaled_params = prepared["params"]
            stimulus_overrides = prepared.get("stimulus_overrides")
            raw_reference = prepared.get("requested_reference")
            requested_reference = (
                None if raw_reference is None else float(raw_reference)
            )
            requested_excitation = float(
                prepared.get("requested_excitation", requested_reference)
            )
            excitation_element = prepared.get("excitation_element")
            nominal_excitation = prepared.get("nominal_excitation")
            if not math.isfinite(requested_excitation) or requested_excitation <= 0.0:
                raise ValueError(
                    f"requested excitation is invalid at scale {scale!r}"
                )
            circuit = adapter.build_circuit(
                scaled_params,
                nominal_vout,
                stimulus_overrides=stimulus_overrides,
            )
            if hasattr(adapter, "read_applied_stimulus"):
                applied_excitation = float(
                    adapter.read_applied_stimulus(circuit, prepared)
                )
        else:
            scaled_params = adapter.scale_reference(params, scale)
            circuit = adapter.build_circuit(scaled_params, nominal_vout)
        try:
            iref_signed, iout_signed = _measure_op_currents(
                circuit, adapter, temperature
            )
        except Exception as error:
            measurements.append(
                {
                    "scale": float(scale),
                    "excitation_element": excitation_element,
                    "nominal_excitation": nominal_excitation,
                    "requested_excitation": requested_excitation,
                    "applied_excitation": applied_excitation,
                    "requested_reference": requested_reference,
                    "measured_reference": None,
                    "measured_output": None,
                }
            )
            return _finalize_tracking_result(
                measurements,
                errors,
                tolerance,
                operating_region_valid=False,
                operating_region_reason=(
                    f"scale {scale:g}: operating point or current measurement "
                    f"unavailable ({error})"
                ),
            )
        expected_iout = mirror_ratio * abs(iref_signed)
        if expected_iout > _CURRENT_EPSILON_A and _meaningful_current(iout_signed):
            tracking_error = float(
                abs(abs(iout_signed) - expected_iout) / expected_iout
            )
            if math.isfinite(tracking_error):
                errors.append(tracking_error)
        measurements.append(
            {
                "scale": float(scale),
                "excitation_element": excitation_element,
                "nominal_excitation": nominal_excitation,
                "requested_excitation": requested_excitation,
                "applied_excitation": applied_excitation,
                "requested_reference": requested_reference,
                "measured_reference": float(abs(iref_signed)),
                "measured_output": float(abs(iout_signed)),
            }
        )

    ordered = sorted(measurements, key=lambda item: item["scale"])
    nominal_point = min(ordered, key=lambda item: abs(item["scale"] - 1.0))
    nominal_reference = float(nominal_point["measured_reference"])
    nominal_output = float(nominal_point["measured_output"])
    reference_floor = max(
        _CURRENT_EPSILON_A,
        abs(nominal_reference) * _RESPONSE_NUMERICAL_RELATIVE_TOLERANCE,
    )
    output_floor = max(
        _CURRENT_EPSILON_A,
        abs(nominal_output) * _RESPONSE_NUMERICAL_RELATIVE_TOLERANCE,
    )
    invalid_scales = [
        item["scale"]
        for item in ordered
        if abs(item["measured_reference"]) <= reference_floor
        or abs(item["measured_output"]) <= output_floor
    ]
    if invalid_scales:
        return _finalize_tracking_result(
            ordered,
            errors,
            tolerance,
            operating_region_valid=False,
            operating_region_reason=(
                "reference/output branch is at or below the numerical operating-"
                "region floor at scale(s): "
                + ", ".join(f"{scale:g}" for scale in invalid_scales)
            ),
        )
    return _finalize_tracking_result(
        ordered,
        errors,
        tolerance,
        operating_region_valid=True,
        operating_region_reason=None,
    )


def _run_reference_tracking_with_policy(
    params: Mapping[str, Any],
    adapter: Any,
    policy: ExcitationPerturbationPolicy,
    tolerance: float,
    nominal_vout: float,
    mirror_ratio: float,
    temperature: float,
) -> dict[str, Any]:
    primary = _run_reference_tracking_test(
        params,
        adapter,
        policy.primary_scales,
        tolerance,
        nominal_vout,
        mirror_ratio,
        temperature,
    )
    policy_metadata = {
        "excitation_quantity": policy.excitation_quantity,
        "primary_scales": list(policy.primary_scales),
        "fallback_scales": (
            None
            if policy.fallback_scales is None
            else list(policy.fallback_scales)
        ),
    }
    primary.update(
        {
            "perturbation_policy": policy_metadata,
            "fallback_used": False,
            "fallback_reason": None,
            "primary_measurements": primary["measurements"],
        }
    )
    primary_reference = [
        item["measured_reference"] for item in primary["measurements"]
    ]
    should_fallback = bool(
        policy.fallback_scales is not None
        and primary["operating_region_valid"]
        and primary["reference_stimulus_applied"]
        and all(value is not None for value in primary_reference)
        and not _has_meaningful_response(primary_reference)
    )
    if not should_fallback:
        return primary

    fallback = _run_reference_tracking_test(
        params,
        adapter,
        policy.fallback_scales,
        tolerance,
        nominal_vout,
        mirror_ratio,
        temperature,
    )
    fallback.update(
        {
            "perturbation_policy": policy_metadata,
            "fallback_used": True,
            "fallback_reason": (
                "primary_reference_response_below_numerical_tolerance"
            ),
            "primary_measurements": primary["measurements"],
        }
    )
    return fallback


def check_current_mirror_functionality(
    params: Mapping[str, Any],
    adapter: Any,
    compliance_vout_start: float,
    compliance_vout_stop: float,
    compliance_points: int = 11,
    compliance_tolerance: float = _PROVISIONAL_COMPLIANCE_QUALITY_TOLERANCE,
    reference_scales: Sequence[float] = (0.8, 1.0, 1.2),
    tracking_tolerance: float = _PROVISIONAL_TRACKING_QUALITY_TOLERANCE,
) -> dict[str, Any]:
    """Check output compliance and reference tracking for validated parameters."""

    start, stop, points, compliance_limit, scales, tracking_limit = _validate_settings(
        compliance_vout_start,
        compliance_vout_stop,
        compliance_points,
        compliance_tolerance,
        reference_scales,
        tracking_tolerance,
    )
    policy = resolve_excitation_perturbation_policy(
        getattr(adapter, "reference_excitation_quantity", None),
        current_scales=scales,
    )
    nominal_vout = float(adapter.vout_nominal)
    mirror_ratio = float(adapter.mirror_ratio)
    temperature = float(adapter.temperature)
    if not math.isfinite(nominal_vout) or not math.isfinite(temperature):
        raise ValueError("adapter nominal voltage and temperature must be finite")
    if not math.isfinite(mirror_ratio) or mirror_ratio <= 0.0:
        raise ValueError("adapter mirror_ratio must be positive and finite")

    failed_nominal = {
        "reference_current_valid": False,
        "output_current_valid": False,
        "reference_current": None,
        "output_current": None,
        "observed_ratio_to_expected": None,
    }
    failed_compliance = {
        "passed": False,
        "quality_smoke_passed": False,
        "relative_variation": None,
        "output_branch_alive": False,
        "alive_fraction": None,
    }
    failed_tracking = {
        "passed": False,
        "quality_smoke_passed": False,
        "max_tracking_error": None,
        "responsive": False,
        "reference_stimulus_applied": False,
        "reference_response_valid": False,
        "reference_responsive": False,
        "output_responsive": False,
        "operating_region_valid": False,
        "operating_region_reason": None,
        "perturbation_policy": {
            "excitation_quantity": policy.excitation_quantity,
            "primary_scales": list(policy.primary_scales),
            "fallback_scales": (
                None
                if policy.fallback_scales is None
                else list(policy.fallback_scales)
            ),
        },
        "fallback_used": False,
        "fallback_reason": None,
        "primary_measurements": [],
        "measurements": [],
    }
    try:
        configure_ngspice()
    except Exception:
        _LOGGER.debug("ngspice initialization failed", exc_info=True)
        return {
            "functional_feasible": False,
            "functional_status": "not_evaluable",
            "failure_reasons": ["simulation_setup_failed"],
            "basic_behavior": {
                "reference_current_valid": False,
                "output_current_valid": False,
                "mirror_response_valid": False,
                "compliance_behavior_valid": False,
            },
            "nominal": failed_nominal,
            "compliance": failed_compliance,
            "reference_tracking": failed_tracking,
        }

    failure_reasons = []
    try:
        nominal = _run_basic_nominal_test(
            params,
            adapter,
            nominal_vout,
            mirror_ratio,
            temperature,
        )
    except Exception:
        _LOGGER.debug("nominal current test failed", exc_info=True)
        nominal = failed_nominal
        failure_reasons.append("nominal_current_data_invalid")
    if not nominal["reference_current_valid"]:
        failure_reasons.append("reference_current_missing")
    if not nominal["output_current_valid"]:
        failure_reasons.append("output_current_missing")

    try:
        compliance = _run_compliance_test(
            params,
            adapter,
            start,
            stop,
            points,
            compliance_limit,
            nominal_vout,
            temperature,
        )
    except Exception:
        _LOGGER.debug("compliance test failed", exc_info=True)
        compliance = failed_compliance
        failure_reasons.append("compliance_data_invalid")

    tracking_data_valid = True
    tracking_evaluable = True
    try:
        tracking = _run_reference_tracking_with_policy(
            params,
            adapter,
            policy,
            tracking_limit,
            nominal_vout,
            mirror_ratio,
            temperature,
        )
    except ReferenceStimulusUnavailableError:
        _LOGGER.debug("reference stimulus is unavailable", exc_info=True)
        tracking = failed_tracking
        tracking_data_valid = False
        tracking_evaluable = False
        failure_reasons.append("reference_stimulus_unavailable")
    except Exception:
        _LOGGER.debug("reference tracking test failed", exc_info=True)
        tracking = failed_tracking
        tracking_data_valid = False
        failure_reasons.append("reference_response_data_invalid")

    if tracking_data_valid:
        if not tracking["reference_stimulus_applied"]:
            failure_reasons.append("reference_stimulus_not_applied")
        elif not tracking["operating_region_valid"]:
            failure_reasons.append("excitation_operating_region_invalid")
            tracking_evaluable = False
        elif not tracking["reference_responsive"]:
            failure_reasons.append("reference_response_data_invalid")
            tracking_evaluable = False
        elif not tracking["reference_response_valid"]:
            failure_reasons.append("reference_response_invalid")
    if not compliance["output_branch_alive"]:
        failure_reasons.append("output_branch_collapsed")

    basic_behavior = {
        "reference_current_valid": nominal["reference_current_valid"],
        "output_current_valid": nominal["output_current_valid"],
        "mirror_response_valid": tracking["responsive"],
        "compliance_behavior_valid": compliance["output_branch_alive"],
    }
    functional_feasible = all(basic_behavior.values())

    return {
        "functional_feasible": functional_feasible,
        "functional_status": (
            "valid"
            if functional_feasible
            else "not_evaluable"
            if not tracking_evaluable
            else "invalid"
        ),
        "failure_reasons": list(dict.fromkeys(failure_reasons)),
        "basic_behavior": basic_behavior,
        "nominal": nominal,
        "compliance": compliance,
        "reference_tracking": tracking,
    }
