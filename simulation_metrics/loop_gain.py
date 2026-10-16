"""Numerical extraction for a caller-supplied formal complex loop gain."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class UnityCrossing:
    frequency_hz: float
    direction: str
    index: int


@dataclass(frozen=True)
class LoopGainResult:
    frequency_hz: np.ndarray
    return_ratio: np.ndarray
    magnitude: np.ndarray
    gain_db: np.ndarray
    phase_deg: np.ndarray
    valid_mask: np.ndarray
    return_ratio_valid: bool
    low_frequency_gain_v_per_v: float | None
    low_frequency_gain_db: float | None
    low_frequency_plateau_spread_db: float | None
    unity_crossings: tuple[UnityCrossing, ...]
    selected_crossing: UnityCrossing | None
    phase_at_unity_deg: float | None
    phase_margin_deg: float | None
    failure_reasons: tuple[str, ...]


def _vectors(
    frequency_hz: Any,
    return_ratio: Any,
) -> tuple[np.ndarray, np.ndarray]:
    frequency = np.asarray(frequency_hz, dtype=float).reshape(-1)
    ratio = np.asarray(return_ratio, dtype=complex).reshape(-1)
    if frequency.size != ratio.size or frequency.size < 2:
        raise ValueError("frequency and return_ratio must have equal length >= 2")
    if np.any(~np.isfinite(frequency)) or np.any(frequency <= 0.0):
        raise ValueError("frequency must contain finite positive values")
    if np.any(np.diff(frequency) <= 0.0):
        raise ValueError("frequency must be strictly increasing")
    return frequency, ratio


def _crossings(
    frequency: np.ndarray,
    gain_db: np.ndarray,
    valid: np.ndarray,
) -> tuple[UnityCrossing, ...]:
    found: list[UnityCrossing] = []
    for index in range(len(gain_db) - 1):
        if not (valid[index] and valid[index + 1]):
            continue
        left = float(gain_db[index])
        right = float(gain_db[index + 1])
        if left == right:
            continue
        direction: str | None = None
        if left >= 0.0 > right:
            direction = "descending"
        elif left <= 0.0 < right:
            direction = "ascending"
        if direction is None:
            continue
        fraction = -left / (right - left)
        log_frequency = math.log10(float(frequency[index])) + fraction * (
            math.log10(float(frequency[index + 1]))
            - math.log10(float(frequency[index]))
        )
        found.append(
            UnityCrossing(
                frequency_hz=10.0**log_frequency,
                direction=direction,
                index=index,
            )
        )
    return tuple(found)


def interpolate_log_frequency(
    frequency_hz: np.ndarray,
    values: np.ndarray,
    target_hz: float,
) -> float:
    """Interpolate a real trace on a logarithmic frequency axis."""

    return float(
        np.interp(
            math.log10(float(target_hz)),
            np.log10(np.asarray(frequency_hz, dtype=float)),
            np.asarray(values, dtype=float),
        )
    )


def analyze_loop_gain(
    frequency_hz: Any,
    return_ratio: Any,
    *,
    low_frequency_point_count: int = 10,
    plateau_tolerance_db: float = 0.5,
    denominator_epsilon: float = 1.0e-24,
) -> LoopGainResult:
    """Extract plateau gain, descending unity crossing, and formal PM.

    ``return_ratio`` must already be the formal complex T(jw).  This function
    intentionally knows nothing about circuit inputs or closed-loop transfer.
    """

    if isinstance(low_frequency_point_count, bool) or low_frequency_point_count <= 0:
        raise ValueError("low_frequency_point_count must be positive")
    if plateau_tolerance_db <= 0.0 or denominator_epsilon <= 0.0:
        raise ValueError("plateau_tolerance_db and denominator_epsilon must be positive")
    frequency, ratio = _vectors(frequency_hz, return_ratio)
    magnitude = np.abs(ratio)
    valid = (
        np.isfinite(ratio.real)
        & np.isfinite(ratio.imag)
        & np.isfinite(magnitude)
        & (magnitude > denominator_epsilon)
    )
    gain_db = np.full(magnitude.shape, np.nan, dtype=float)
    gain_db[valid] = 20.0 * np.log10(magnitude[valid])

    raw_phase = np.unwrap(np.angle(ratio)) * 180.0 / math.pi
    valid_indices = np.flatnonzero(valid)
    low_indices = valid_indices[: min(low_frequency_point_count, len(valid_indices))]
    failure_reasons: list[str] = []
    low_gain_db: float | None = None
    low_gain: float | None = None
    plateau_spread: float | None = None
    if len(low_indices) < min(3, low_frequency_point_count):
        failure_reasons.append("low_frequency_gain_unavailable")
        normalized_phase = raw_phase
    else:
        low_values = gain_db[low_indices]
        plateau_spread = float(np.max(low_values) - np.min(low_values))
        low_phase = float(np.median(raw_phase[low_indices]))
        normalized_phase = raw_phase - 360.0 * round(low_phase / 360.0)
        if plateau_spread > plateau_tolerance_db:
            failure_reasons.append("low_frequency_gain_unavailable")
        else:
            low_gain_db = float(np.median(low_values))
            low_gain = float(10.0 ** (low_gain_db / 20.0))

    crossings = _crossings(frequency, gain_db, valid)
    selected = next(
        (crossing for crossing in crossings if crossing.direction == "descending"),
        None,
    )
    phase_at_unity: float | None = None
    phase_margin: float | None = None
    if selected is None:
        failure_reasons.append("no_valid_unity_crossing")
    else:
        phase_at_unity = interpolate_log_frequency(
            frequency,
            normalized_phase,
            selected.frequency_hz,
        )
        if math.isfinite(phase_at_unity):
            phase_margin = 180.0 + phase_at_unity
        else:
            phase_at_unity = None
            failure_reasons.append("phase_margin_unavailable")

    return LoopGainResult(
        frequency_hz=frequency,
        return_ratio=ratio,
        magnitude=magnitude,
        gain_db=gain_db,
        phase_deg=normalized_phase,
        valid_mask=valid,
        return_ratio_valid=bool(np.all(valid)),
        low_frequency_gain_v_per_v=low_gain,
        low_frequency_gain_db=low_gain_db,
        low_frequency_plateau_spread_db=plateau_spread,
        unity_crossings=crossings,
        selected_crossing=selected,
        phase_at_unity_deg=phase_at_unity,
        phase_margin_deg=phase_margin,
        failure_reasons=tuple(dict.fromkeys(failure_reasons)),
    )
