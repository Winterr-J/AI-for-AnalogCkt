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
from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice


__all__ = ["check_current_mirror_functionality"]

_LOGGER = logging.getLogger(__name__)
_CURRENT_EPSILON_A = 1.0e-18

# Configurable V1 smoke-test defaults, not final physical acceptance standards.
_PROVISIONAL_COMPLIANCE_TOLERANCE = 0.05
_PROVISIONAL_TRACKING_TOLERANCE = 0.05


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
    relative_variation = float(np.max(np.abs(iout - nominal_iout) / nominal_iout))
    if not math.isfinite(relative_variation):
        raise ValueError("compliance variation is not finite")
    return {
        "passed": relative_variation <= tolerance,
        "relative_variation": relative_variation,
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
    for scale in reference_scales:
        scaled_params = adapter.scale_reference(params, scale)
        circuit = adapter.build_circuit(scaled_params, nominal_vout)
        iref_signed, iout_signed = _measure_op_currents(circuit, adapter, temperature)
        expected_iout = mirror_ratio * abs(iref_signed)
        if expected_iout <= _CURRENT_EPSILON_A:
            raise ValueError(f"tracking test has near-zero expected IOUT at scale {scale!r}")
        tracking_error = float(
            abs(abs(iout_signed) - expected_iout) / expected_iout
        )
        if not math.isfinite(tracking_error):
            raise ValueError(f"tracking error is not finite at scale {scale!r}")
        errors.append(tracking_error)

    max_error = max(errors)
    return {
        "passed": max_error <= tolerance,
        "max_tracking_error": max_error,
    }


def check_current_mirror_functionality(
    params: Mapping[str, Any],
    adapter: Any,
    compliance_vout_start: float,
    compliance_vout_stop: float,
    compliance_points: int = 11,
    compliance_tolerance: float = _PROVISIONAL_COMPLIANCE_TOLERANCE,
    reference_scales: Sequence[float] = (0.8, 1.0, 1.2),
    tracking_tolerance: float = _PROVISIONAL_TRACKING_TOLERANCE,
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
    nominal_vout = float(adapter.vout_nominal)
    mirror_ratio = float(adapter.mirror_ratio)
    temperature = float(adapter.temperature)
    if not math.isfinite(nominal_vout) or not math.isfinite(temperature):
        raise ValueError("adapter nominal voltage and temperature must be finite")
    if not math.isfinite(mirror_ratio) or mirror_ratio <= 0.0:
        raise ValueError("adapter mirror_ratio must be positive and finite")

    failed_compliance = {"passed": False, "relative_variation": None}
    failed_tracking = {"passed": False, "max_tracking_error": None}
    try:
        configure_ngspice()
    except Exception:
        _LOGGER.debug("ngspice initialization failed", exc_info=True)
        return {
            "functional_feasible": False,
            "compliance": failed_compliance,
            "reference_tracking": failed_tracking,
        }

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

    try:
        tracking = _run_reference_tracking_test(
            params,
            adapter,
            scales,
            tracking_limit,
            nominal_vout,
            mirror_ratio,
            temperature,
        )
    except Exception:
        _LOGGER.debug("reference tracking test failed", exc_info=True)
        tracking = failed_tracking

    return {
        "functional_feasible": compliance["passed"] and tracking["passed"],
        "compliance": compliance,
        "reference_tracking": tracking,
    }
