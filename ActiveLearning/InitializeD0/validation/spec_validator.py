"""Generic target-specification checks for measured performance metrics."""

from __future__ import annotations

import math
import operator
from collections.abc import Mapping
from typing import Any


SUPPORTED_OPERATORS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
}


def _target_value(metric_name: str, spec: Any) -> tuple[Any, float]:
    if not isinstance(spec, Mapping):
        raise TypeError(f"target spec for {metric_name!r} must be a mapping")
    missing = [key for key in ("operator", "value") if key not in spec]
    if missing:
        raise ValueError(
            f"target spec for {metric_name!r} is missing: {', '.join(missing)}"
        )

    operator_name = spec["operator"]
    if not isinstance(operator_name, str) or operator_name not in SUPPORTED_OPERATORS:
        raise ValueError(f"unsupported target operator: {operator_name!r}")
    try:
        target = float(spec["value"])
    except (TypeError, ValueError) as error:
        raise TypeError(f"target value for {metric_name!r} must be numeric") from error
    if not math.isfinite(target):
        raise ValueError(f"target value for {metric_name!r} must be finite")
    return SUPPORTED_OPERATORS[operator_name], target


def check_meets_spec(
    metrics: Mapping[str, float | None],
    target_specs: Mapping[str, Any] | None,
) -> dict[str, bool | None]:
    """Return whether every defined target is met, or ``None`` if none exist."""

    if target_specs is None:
        return {"meets_spec": None}
    if not isinstance(target_specs, Mapping):
        raise TypeError("target_specs must be a mapping or None")
    if not target_specs:
        return {"meets_spec": None}
    if not isinstance(metrics, Mapping):
        raise TypeError("metrics must be a mapping")

    for metric_name, spec in target_specs.items():
        compare, target = _target_value(str(metric_name), spec)
        if metric_name not in metrics or metrics[metric_name] is None:
            return {"meets_spec": False}
        try:
            measured = float(metrics[metric_name])
        except (TypeError, ValueError):
            return {"meets_spec": False}
        if not math.isfinite(measured) or not compare(measured, target):
            return {"meets_spec": False}

    return {"meets_spec": True}
