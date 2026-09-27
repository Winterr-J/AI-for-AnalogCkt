"""Generic V1 SPICE performance evaluation for current mirrors."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np

from run.single_stage_amplifier.run_ideal_dataset_simulations import configure_ngspice


__all__ = ["evaluate_current_mirror"]

_LOGGER = logging.getLogger(__name__)

_CURRENT_EPSILON_A = 1.0e-18
_SLOPE_EPSILON_A_PER_V = 1.0e-18


@dataclass(frozen=True)
class CurrentMeasurement:
    """Location of a current vector in a PySpice analysis."""

    section: Literal["branches", "internal_parameters"]
    name: str


class CurrentMirrorAdapter(Protocol):
    """Minimum circuit-specific interface required by the generic evaluator."""

    name: str
    op_reference_current: CurrentMeasurement
    op_output_current: CurrentMeasurement
    dc_sweep_source: str
    dc_output_current: CurrentMeasurement
    mirror_ratio: float
    vout_nominal: float
    temperature: float

    def build_circuit(self, params: Mapping[str, float], vout_nominal: float) -> Any:
        """Build and fully configure a circuit instance for OP and DC."""


def _finite_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite real number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite real number")
    return result


def _adapter_name(adapter: CurrentMirrorAdapter) -> str:
    name = str(getattr(adapter, "name", "")).strip()
    return name or type(adapter).__name__


def _validate_simulation_settings(
    adapter: CurrentMirrorAdapter,
    vout_nominal: Any,
    rout_half_width: Any,
    rout_points: Any,
) -> tuple[float, float, int, float, float]:
    adapter_name = _adapter_name(adapter)

    adapter_attributes = (
        "op_reference_current",
        "op_output_current",
        "dc_sweep_source",
        "dc_output_current",
        "mirror_ratio",
        "vout_nominal",
        "temperature",
    )
    missing_attributes = [
        attribute for attribute in adapter_attributes if not hasattr(adapter, attribute)
    ]
    if missing_attributes or not callable(getattr(adapter, "build_circuit", None)):
        missing = [*missing_attributes]
        if not callable(getattr(adapter, "build_circuit", None)):
            missing.append("build_circuit")
        raise TypeError(
            f"{adapter_name} does not implement the CurrentMirrorAdapter interface: "
            + ", ".join(missing)
        )
    if not isinstance(adapter.op_reference_current, CurrentMeasurement) or not isinstance(
        adapter.op_output_current, CurrentMeasurement
    ) or not isinstance(adapter.dc_output_current, CurrentMeasurement):
        raise TypeError(f"{adapter_name} adapter current mappings must be CurrentMeasurement")

    nominal_source = adapter.vout_nominal if vout_nominal is None else vout_nominal
    nominal = _finite_float(nominal_source, "vout_nominal")
    half_width = _finite_float(rout_half_width, "rout_half_width")
    if half_width <= 0.0:
        raise ValueError("rout_half_width must be positive")
    if isinstance(rout_points, bool) or not isinstance(rout_points, (int, np.integer)):
        raise TypeError("rout_points must be an integer")
    point_count = int(rout_points)
    if point_count < 3:
        raise ValueError("rout_points must be at least 3 for local linear fitting")

    mirror_ratio = _finite_float(adapter.mirror_ratio, "adapter.mirror_ratio")
    if mirror_ratio <= 0.0:
        raise ValueError("adapter.mirror_ratio must be positive")
    temperature = _finite_float(adapter.temperature, "adapter.temperature")
    if not str(adapter.dc_sweep_source).strip():
        raise ValueError(f"{adapter_name} adapter has an empty dc_sweep_source")
    return nominal, half_width, point_count, mirror_ratio, temperature


def _build_circuit(
    adapter: CurrentMirrorAdapter,
    params: Mapping[str, float],
    vout_nominal: float,
) -> Any:
    try:
        return adapter.build_circuit(params, vout_nominal)
    except Exception as error:
        raise RuntimeError(
            f"{_adapter_name(adapter)} topology build failed: {error}"
        ) from error


def _lookup(mapping: Any, name: str) -> Any | None:
    target = name.casefold()
    for key, value in mapping.items():
        if str(key).casefold() == target:
            return value
    return None


def _measurement_vector(analysis: Any, measurement: CurrentMeasurement) -> np.ndarray:
    mapping = getattr(analysis, measurement.section, None)
    if mapping is None:
        raise KeyError(f"simulator analysis section {measurement.section!r} is unavailable")
    item = _lookup(mapping, measurement.name)
    if item is None:
        raise KeyError(f"required simulator current {measurement.name!r} is unavailable")
    return np.asarray(item, dtype=float).reshape(-1)


def _measurement_scalar(analysis: Any, measurement: CurrentMeasurement) -> float:
    values = _measurement_vector(analysis, measurement)
    if values.size != 1:
        raise ValueError(f"required simulator current {measurement.name!r} is not scalar")
    result = float(values[0])
    if not math.isfinite(result):
        raise ValueError(f"required simulator current {measurement.name!r} is not finite")
    return result


def _run_operating_point(
    circuit: Any,
    adapter: CurrentMirrorAdapter,
    mirror_ratio: float,
    temperature: float,
) -> tuple[float, float, float]:
    adapter_name = _adapter_name(adapter)
    try:
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
        iref_signed = _measurement_scalar(analysis, adapter.op_reference_current)
        iout_signed = _measurement_scalar(analysis, adapter.op_output_current)
    except Exception as error:
        raise RuntimeError(f"{adapter_name} OP simulation failed: {error}") from error

    iref = abs(iref_signed)
    iout = abs(iout_signed)
    expected = abs(mirror_ratio * iref)
    if expected <= _CURRENT_EPSILON_A:
        raise RuntimeError(
            f"{adapter_name} current_error calculation failed: "
            f"abs(k * IREF)={expected:.12g} A is too close to zero"
        )
    current_error = float(abs(iout - mirror_ratio * iref) / expected)
    if not math.isfinite(current_error):
        raise RuntimeError(
            f"{adapter_name} current_error calculation produced a non-finite value"
        )

    _LOGGER.debug(
        "%s OP: IREF_signed=%r A, IOUT_signed=%r A, mirror_ratio=%r, "
        "current_error=%r",
        adapter_name,
        iref_signed,
        iout_signed,
        mirror_ratio,
        current_error,
    )
    return iref_signed, iout_signed, current_error


def _run_output_resistance_sweep(
    circuit: Any,
    adapter: CurrentMirrorAdapter,
    vout_nominal: float,
    rout_half_width: float,
    rout_points: int,
    temperature: float,
) -> float:
    adapter_name = _adapter_name(adapter)
    start = vout_nominal - rout_half_width
    stop = vout_nominal + rout_half_width
    step = (stop - start) / (rout_points - 1)
    try:
        analysis = circuit.simulator(
            temperature=temperature,
            nominal_temperature=temperature,
        ).dc(**{adapter.dc_sweep_source: slice(start, stop, step)})
        vout = np.asarray(analysis.sweep, dtype=float).reshape(-1)
        iout_signed = _measurement_vector(analysis, adapter.dc_output_current)
    except Exception as error:
        raise RuntimeError(f"{adapter_name} DC simulation failed: {error}") from error

    if len(vout) != rout_points or len(iout_signed) != rout_points:
        raise RuntimeError(
            f"{adapter_name} output_resistance calculation failed: "
            f"expected {rout_points} aligned DC points, got "
            f"{len(vout)} VOUT and {len(iout_signed)} IOUT points"
        )
    if not np.all(np.isfinite(vout)) or not np.all(np.isfinite(iout_signed)):
        raise RuntimeError(
            f"{adapter_name} output_resistance calculation failed: "
            "DC VOUT/IOUT contains non-finite values"
        )
    if np.any(np.diff(vout) <= 0.0):
        raise RuntimeError(
            f"{adapter_name} output_resistance calculation failed: "
            "DC VOUT points are not strictly increasing"
        )

    try:
        slope, _intercept = np.polyfit(vout, iout_signed, 1)
    except Exception as error:
        raise RuntimeError(
            f"{adapter_name} output_resistance linear fit failed: {error}"
        ) from error
    slope = float(slope)
    if not math.isfinite(slope):
        raise RuntimeError(
            f"{adapter_name} output_resistance calculation failed: "
            "fitted dI/dV is not finite"
        )
    if abs(slope) <= _SLOPE_EPSILON_A_PER_V:
        raise RuntimeError(
            f"{adapter_name} output_resistance calculation failed: "
            f"abs(fitted dI/dV)={abs(slope):.12g} A/V is too close to zero"
        )

    output_resistance = float(abs(1.0 / slope))
    if not math.isfinite(output_resistance) or output_resistance <= 0.0:
        raise RuntimeError(
            f"{adapter_name} output_resistance calculation produced a non-positive "
            "or non-finite value"
        )

    _LOGGER.debug(
        "%s DC: source=%s, VOUT=[%r, %r] V (%d points), "
        "IOUT_signed=[%r, %r] A, dI/dV=%r A/V, Rout=%r ohm",
        adapter_name,
        adapter.dc_sweep_source,
        float(vout[0]),
        float(vout[-1]),
        len(vout),
        float(np.min(iout_signed)),
        float(np.max(iout_signed)),
        slope,
        output_resistance,
    )
    return output_resistance


def evaluate_current_mirror(
    params: Mapping[str, Any],
    adapter: CurrentMirrorAdapter,
    vout_nominal: float | None = None,
    rout_half_width: float = 0.1,
    rout_points: int = 11,
) -> tuple[float | None, float | None, bool]:
    """Evaluate current error and local output resistance for a current mirror.

    Circuit construction, parameter names, current mappings, mirror ratio,
    nominal output voltage, and simulation temperature are supplied by
    ``adapter``.  Simulation or extraction failures return
    ``(None, None, False)``; successful evaluation returns
    ``(current_error, output_resistance_ohm, True)``.
    """

    nominal, half_width, point_count, mirror_ratio, temperature = (
        _validate_simulation_settings(
            adapter,
            vout_nominal,
            rout_half_width,
            rout_points,
        )
    )
    adapter_name = _adapter_name(adapter)
    try:
        try:
            configure_ngspice()
        except Exception as error:
            raise RuntimeError(
                f"{adapter_name} ngspice initialization failed: {error}"
            ) from error

        circuit = _build_circuit(adapter, params, nominal)
        _iref_signed, _iout_signed, current_error = _run_operating_point(
            circuit,
            adapter,
            mirror_ratio,
            temperature,
        )
        output_resistance = _run_output_resistance_sweep(
            circuit,
            adapter,
            nominal,
            half_width,
            point_count,
            temperature,
        )
        return float(current_error), float(output_resistance), True
    except RuntimeError:
        _LOGGER.debug("%s evaluation failed", adapter_name, exc_info=True)
        return None, None, False
