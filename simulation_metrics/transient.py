"""Transient simulation and steady-state functional metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

import numpy as np


ExpectedPolarity = Literal["inverting", "noninverting"] | None


@dataclass
class TransientAnalysisResult:
    analysis: Any
    time_s: np.ndarray
    input_voltage_v: np.ndarray
    output_voltage_v: np.ndarray
    signed_gain_v_per_v: float
    input_amplitude_v: float
    output_amplitude_v: float
    expected_polarity: ExpectedPolarity
    polarity_matches_expected: bool | None


def _array(value: Any) -> np.ndarray:
    return np.asarray(value, dtype=float).reshape(-1)


def _node(analysis: Any, name: str) -> Any | None:
    nodes = getattr(analysis, "nodes", {})
    if isinstance(nodes, Mapping):
        target = name.casefold()
        for key, value in nodes.items():
            if str(key).casefold() == target:
                return value
    try:
        return analysis[name]
    except (KeyError, IndexError, TypeError):
        return None


def run_transient_analysis(
    circuit: Any,
    *,
    step_time_s: float,
    end_time_s: float,
    input_node: str = "VIN1",
    output_node: str = "VOUT1",
    expected_polarity: ExpectedPolarity = None,
    settling_fraction: float = 0.2,
    temperature_c: float = 25.0,
    nominal_temperature_c: float = 25.0,
) -> TransientAnalysisResult:
    """Run TRAN and measure steady-state signed gain and signal amplitudes."""

    if step_time_s <= 0 or end_time_s <= step_time_s:
        raise ValueError("require 0 < step_time_s < end_time_s")
    if not 0 <= settling_fraction < 1:
        raise ValueError("settling_fraction must be in [0, 1)")
    if expected_polarity not in (None, "inverting", "noninverting"):
        raise ValueError("expected_polarity must be inverting, noninverting, or None")

    analysis = circuit.simulator(
        temperature=temperature_c,
        nominal_temperature=nominal_temperature_c,
    ).transient(step_time=step_time_s, end_time=end_time_s)
    input_value = _node(analysis, input_node)
    output_value = _node(analysis, output_node)
    if input_value is None or output_value is None:
        raise KeyError(f"TRAN input/output {input_node!r}/{output_node!r} is unavailable")
    input_voltage = _array(input_value)
    output_voltage = _array(output_value)
    time_value = getattr(analysis, "time", np.arange(len(input_voltage), dtype=float))
    time_s = _array(time_value)
    if len(input_voltage) != len(output_voltage) or len(time_s) != len(input_voltage):
        raise ValueError("TRAN time, input and output vectors are not aligned")

    skip = int(len(input_voltage) * settling_fraction)
    vin = input_voltage[skip:] - np.mean(input_voltage[skip:])
    vout = output_voltage[skip:] - np.mean(output_voltage[skip:])
    denominator = float(np.dot(vin, vin))
    signed_gain = float(np.dot(vin, vout) / denominator) if denominator else 0.0
    input_amplitude = 0.5 * float(np.ptp(input_voltage[skip:]))
    output_amplitude = 0.5 * float(np.ptp(output_voltage[skip:]))
    if expected_polarity is None:
        polarity_matches = None
    elif expected_polarity == "inverting":
        polarity_matches = signed_gain < 0
    else:
        polarity_matches = signed_gain > 0
    return TransientAnalysisResult(
        analysis=analysis,
        time_s=time_s,
        input_voltage_v=input_voltage,
        output_voltage_v=output_voltage,
        signed_gain_v_per_v=signed_gain,
        input_amplitude_v=input_amplitude,
        output_amplitude_v=output_amplitude,
        expected_polarity=expected_polarity,
        polarity_matches_expected=polarity_matches,
    )
