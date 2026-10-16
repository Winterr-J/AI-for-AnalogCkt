"""Finite closed-interval operations used by Stage B v1."""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..core.models import NumericRange
from ..core.types import RangeStatus


@dataclass(frozen=True)
class IntervalDecision:
    range: NumericRange
    status: RangeStatus
    reason: str


def validate_interval(value: NumericRange) -> IntervalDecision:
    if value.lower is None or value.upper is None:
        return IntervalDecision(value, RangeStatus.UNCERTAIN, "INCOMPLETE_RANGE_RESULT")
    lower = float(value.lower)
    upper = float(value.upper)
    if not math.isfinite(lower) or not math.isfinite(upper):
        return IntervalDecision(NumericRange(), RangeStatus.UNCERTAIN, "NONFINITE_RANGE_RESULT")
    if lower > upper:
        return IntervalDecision(value, RangeStatus.CONFLICT, "INVALID_RANGE_ORDER")
    if lower == upper:
        return IntervalDecision(value, RangeStatus.CONFLICT, "DEGENERATE_CONTINUOUS_RANGE")
    return IntervalDecision(value, RangeStatus.VALID, "VALID_INTERVAL")


def intersect_intervals(left: NumericRange, right: NumericRange) -> IntervalDecision:
    left_check = validate_interval(left)
    if left_check.status != RangeStatus.VALID:
        return left_check
    right_check = validate_interval(right)
    if right_check.status != RangeStatus.VALID:
        return right_check
    assert left.lower is not None and left.upper is not None
    assert right.lower is not None and right.upper is not None
    lower = max(float(left.lower), float(right.lower))
    upper = min(float(left.upper), float(right.upper))
    result = NumericRange(
        lower=lower,
        upper=upper,
        scale=left.scale or right.scale,
        transform=left.transform or right.transform,
        fixed_sign=left.fixed_sign or right.fixed_sign,
    )
    if lower > upper:
        return IntervalDecision(result, RangeStatus.CONFLICT, "EMPTY_RANGE_INTERSECTION")
    if lower == upper:
        return IntervalDecision(result, RangeStatus.CONFLICT, "DEGENERATE_CONTINUOUS_RANGE")
    if _metadata_conflicts(left, right):
        return IntervalDecision(result, RangeStatus.CONFLICT, "RANGE_METADATA_CONFLICT")
    return IntervalDecision(result, RangeStatus.VALID, "VALID_INTERVAL_INTERSECTION")


def ordered_range(
    first: float,
    second: float,
    *,
    scale: str | None,
    transform: str | None,
    fixed_sign: str | None,
) -> IntervalDecision:
    if not math.isfinite(first) or not math.isfinite(second):
        return IntervalDecision(NumericRange(), RangeStatus.UNCERTAIN, "NONFINITE_RANGE_RESULT")
    value = NumericRange(
        lower=min(first, second),
        upper=max(first, second),
        scale=scale,
        transform=transform,
        fixed_sign=fixed_sign,
    )
    return validate_interval(value)


def contains_zero(value: NumericRange) -> bool:
    return (
        value.lower is None
        or value.upper is None
        or float(value.lower) <= 0.0 <= float(value.upper)
    )


def _metadata_conflicts(left: NumericRange, right: NumericRange) -> bool:
    for first, second in (
        (left.scale, right.scale),
        (left.transform, right.transform),
        (left.fixed_sign, right.fixed_sign),
    ):
        if first is not None and second is not None and first != second:
            return True
    return False
