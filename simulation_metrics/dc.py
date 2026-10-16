"""DC transfer sweep and operating-region metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

import numpy as np


ExpectedPolarity = Literal["inverting", "noninverting"] | None
CheckState = bool | None


@dataclass(frozen=True)
class DCSweepRegion:
    kind: str
    start_input_v: float
    stop_input_v: float
    point_count: int
    median_gain_v_per_v: float


@dataclass
class DCSweepResult:
    analysis: Any
    input_voltage_v: np.ndarray
    output_voltage_v: np.ndarray
    local_gain_v_per_v: np.ndarray
    normal_regions: tuple[DCSweepRegion, ...]
    limiting_regions: tuple[DCSweepRegion, ...]
    recommended_bias_v: float | None
    observed_polarity: str | None
    direction_matches_expected: CheckState
    has_normal_amplification_region: bool
    has_cutoff_or_saturation_region: bool


def _array(value: Any) -> np.ndarray:
    return np.asarray(value, dtype=float).reshape(-1)


def _analysis_lookup(analysis: Any, name: str) -> tuple[str, Any] | None:
    nodes = getattr(analysis, "nodes", {})
    if isinstance(nodes, Mapping):
        target = name.casefold()
        for key, value in nodes.items():
            if str(key).casefold() == target:
                return str(key), value
    try:
        return name, analysis[name]
    except (KeyError, IndexError, TypeError):
        return None


def _contiguous_regions(
    mask: np.ndarray,
    input_voltage: np.ndarray,
    local_gain: np.ndarray,
    kind: str,
) -> tuple[DCSweepRegion, ...]:
    indices = np.flatnonzero(mask)
    if not len(indices):
        return ()
    groups = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
    return tuple(
        DCSweepRegion(
            kind=kind,
            start_input_v=float(input_voltage[group[0]]),
            stop_input_v=float(input_voltage[group[-1]]),
            point_count=int(len(group)),
            median_gain_v_per_v=float(np.median(local_gain[group])),
        )
        for group in groups
        if len(group)
    )


def _recommended_bias(
    normal_mask: np.ndarray,
    input_voltage: np.ndarray,
    output_voltage: np.ndarray,
    vdd_v: float,
) -> float | None:
    candidates = np.flatnonzero(normal_mask)
    if not len(candidates):
        return None
    selected = candidates[int(np.argmin(np.abs(output_voltage[candidates] - 0.5 * vdd_v)))]
    return float(input_voltage[selected])


def run_dc_sweep(
    circuit: Any,
    *,
    sweep_source: str = "VVIN1",
    input_node: str = "VIN1",
    output_node: str = "VOUT1",
    start_v: float = 0.0,
    stop_v: float = 3.3,
    step_v: float = 0.01,
    vdd_v: float = 3.3,
    expected_polarity: ExpectedPolarity = None,
    minimum_abs_gain_v_per_v: float = 0.1,
    flat_gain_threshold_v_per_v: float = 0.02,
    rail_margin_v: float | None = None,
    temperature_c: float = 25.0,
    nominal_temperature_c: float = 25.0,
) -> DCSweepResult:
    """Sweep an input source and identify useful and limiting DC regions."""

    if vdd_v <= 0:
        raise ValueError("vdd_v must be positive")
    if step_v <= 0 or stop_v <= start_v:
        raise ValueError("require step_v > 0 and stop_v > start_v")
    if minimum_abs_gain_v_per_v < 0 or flat_gain_threshold_v_per_v < 0:
        raise ValueError("gain thresholds must be non-negative")
    if expected_polarity not in (None, "inverting", "noninverting"):
        raise ValueError("expected_polarity must be inverting, noninverting, or None")
    rail_margin = 0.05 * vdd_v if rail_margin_v is None else float(rail_margin_v)

    simulator = circuit.simulator(
        temperature=temperature_c,
        nominal_temperature=nominal_temperature_c,
    )
    analysis = simulator.dc(**{sweep_source: slice(start_v, stop_v, step_v)})
    sweep = getattr(analysis, "sweep", None)
    input_match = _analysis_lookup(analysis, input_node)
    input_voltage = _array(sweep if sweep is not None else input_match[1]) if (
        sweep is not None or input_match is not None
    ) else None
    if input_voltage is None:
        raise KeyError(f"DC sweep input {input_node!r} is unavailable")
    output_match = _analysis_lookup(analysis, output_node)
    if output_match is None:
        raise KeyError(f"DC sweep output {output_node!r} is unavailable")
    output_voltage = _array(output_match[1])
    if len(input_voltage) != len(output_voltage) or len(input_voltage) < 3:
        raise ValueError("DC sweep requires at least three aligned input/output points")
    if np.any(np.diff(input_voltage) <= 0):
        raise ValueError("DC sweep input values must be strictly increasing")

    local_gain = np.gradient(output_voltage, input_voltage)
    away = (output_voltage > rail_margin) & (output_voltage < vdd_v - rail_margin)
    active = np.abs(local_gain) >= minimum_abs_gain_v_per_v
    if expected_polarity == "inverting":
        direction = local_gain < 0
    elif expected_polarity == "noninverting":
        direction = local_gain > 0
    else:
        direction = np.ones_like(local_gain, dtype=bool)
    normal = away & active & direction
    wrong = away & active & ~direction
    low = output_voltage <= rail_margin
    high = output_voltage >= vdd_v - rail_margin
    flat = away & (np.abs(local_gain) <= flat_gain_threshold_v_per_v)

    normal_regions = _contiguous_regions(normal, input_voltage, local_gain, "normal_amplification")
    limiting_regions = (
        *_contiguous_regions(low, input_voltage, local_gain, "low_rail"),
        *_contiguous_regions(high, input_voltage, local_gain, "high_rail"),
        *_contiguous_regions(flat, input_voltage, local_gain, "flat_cutoff_or_saturation"),
        *_contiguous_regions(wrong, input_voltage, local_gain, "wrong_polarity"),
    )
    samples = local_gain[normal]
    observed = None if not len(samples) else (
        "noninverting" if float(np.median(samples)) >= 0 else "inverting"
    )
    direction_matches = None if expected_polarity is None or observed is None else observed == expected_polarity
    return DCSweepResult(
        analysis=analysis,
        input_voltage_v=input_voltage,
        output_voltage_v=output_voltage,
        local_gain_v_per_v=local_gain,
        normal_regions=normal_regions,
        limiting_regions=limiting_regions,
        recommended_bias_v=_recommended_bias(normal, input_voltage, output_voltage, vdd_v),
        observed_polarity=observed,
        direction_matches_expected=direction_matches,
        has_normal_amplification_region=bool(normal_regions),
        has_cutoff_or_saturation_region=bool(limiting_regions),
    )
