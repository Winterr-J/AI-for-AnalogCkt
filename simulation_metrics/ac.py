"""Small-signal AC response and bandwidth metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np


@dataclass
class ACAnalysisResult:
    analysis: Any
    frequency_hz: np.ndarray
    transfer: np.ndarray
    gain_v_per_v: np.ndarray
    gain_db: np.ndarray
    phase_deg: np.ndarray | None
    low_frequency_gain_v_per_v: float
    low_frequency_gain_db: float
    bandwidth_3db_hz: float | None
    phase_at_bandwidth_deg: float | None


def _array(value: Any, *, complex_values: bool = False) -> np.ndarray:
    return np.asarray(value, dtype=complex if complex_values else float).reshape(-1)


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


def _first_log_crossing(
    frequency: np.ndarray,
    values: np.ndarray,
    target: float,
    start_index: int,
) -> float | None:
    for index in range(max(start_index, 0), len(values) - 1):
        y0, y1 = float(values[index]), float(values[index + 1])
        if y0 >= target and y1 < target:
            f0, f1 = float(frequency[index]), float(frequency[index + 1])
            if y1 == y0 or f0 <= 0 or f1 <= 0:
                return f0
            fraction = (target - y0) / (y1 - y0)
            return float(10 ** (np.log10(f0) + fraction * (np.log10(f1) - np.log10(f0))))
    return None


def run_ac_analysis(
    circuit: Any,
    *,
    output_node: str = "VOUT1",
    input_node: str | None = "VIN1",
    input_ac_amplitude_v: float = 1.0,
    start_frequency_hz: float = 1.0,
    stop_frequency_hz: float = 1e9,
    points_per_decade: int = 60,
    variation: str = "dec",
    low_frequency_point_count: int = 10,
    include_phase: bool = True,
    temperature_c: float = 25.0,
    nominal_temperature_c: float = 25.0,
) -> ACAnalysisResult:
    """Run AC analysis and calculate gain, phase and relative -3 dB bandwidth."""

    if start_frequency_hz <= 0 or stop_frequency_hz <= start_frequency_hz:
        raise ValueError("AC frequencies must satisfy 0 < start < stop")
    if points_per_decade <= 0 or low_frequency_point_count <= 0:
        raise ValueError("AC point counts must be positive")
    if input_ac_amplitude_v == 0:
        raise ValueError("input_ac_amplitude_v must be non-zero")

    analysis = circuit.simulator(
        temperature=temperature_c,
        nominal_temperature=nominal_temperature_c,
    ).ac(
        start_frequency=start_frequency_hz,
        stop_frequency=stop_frequency_hz,
        number_of_points=points_per_decade,
        variation=variation,
    )
    frequency = _array(analysis.frequency)
    output_value = _node(analysis, output_node)
    if output_value is None:
        raise KeyError(f"AC output {output_node!r} is unavailable")
    output = _array(output_value, complex_values=True)
    input_value = _node(analysis, input_node) if input_node is not None else None
    if input_value is not None:
        input_vector = _array(input_value, complex_values=True)
        if len(input_vector) != len(output) or np.any(np.abs(input_vector) < 1e-30):
            raise ValueError("AC input vector is invalid")
        transfer = output / input_vector
    else:
        transfer = output / complex(input_ac_amplitude_v)

    gain = np.abs(transfer)
    gain_db = 20.0 * np.log10(np.maximum(gain, 1e-30))
    sample_count = min(low_frequency_point_count, len(gain_db))
    low_gain_db = float(np.median(gain_db[:sample_count]))
    low_gain = float(10 ** (low_gain_db / 20.0))
    bandwidth = _first_log_crossing(
        frequency,
        gain_db,
        low_gain_db - 3.0,
        max(sample_count - 1, 0),
    )
    phase = np.unwrap(np.angle(transfer)) * 180.0 / np.pi if include_phase else None
    phase_at_bandwidth = None
    if phase is not None and bandwidth is not None:
        phase_at_bandwidth = float(np.interp(np.log10(bandwidth), np.log10(frequency), phase))
    return ACAnalysisResult(
        analysis=analysis,
        frequency_hz=frequency,
        transfer=transfer,
        gain_v_per_v=gain,
        gain_db=gain_db,
        phase_deg=phase,
        low_frequency_gain_v_per_v=low_gain,
        low_frequency_gain_db=low_gain_db,
        bandwidth_3db_hz=bandwidth,
        phase_at_bandwidth_deg=phase_at_bandwidth,
    )
