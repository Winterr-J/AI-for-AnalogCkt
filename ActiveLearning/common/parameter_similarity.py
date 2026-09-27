"""Parameter-template-aware feature vectors for similarity calculations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def parameter_to_similarity_vector(
    params: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
    *,
    normalized_params: Mapping[str, Any] | None = None,
) -> list[float]:
    """Map physical parameters to stable similarity coordinates.

    Strictly positive continuous ranges use log10 coordinates. Strictly
    negative ranges use log10 magnitude coordinates, without changing the
    physical signed value. Ranges containing zero retain linear normalization.
    Non-continuous variables retain their existing normalized representation.
    """

    definitions = {
        definition["name"]: definition
        for definition in parameter_template["parameters"]
    }
    vector = []
    for name in parameter_template["optimization_space"]["variables"]:
        definition = definitions[name]
        if definition["variable_type"] != "continuous":
            if normalized_params is None or name not in normalized_params:
                raise ValueError(
                    f"{name}: normalized value is required for non-continuous similarity"
                )
            coordinate = float(normalized_params[name])
        else:
            admissible_range = definition["admissible_range"]
            lower = float(admissible_range["lower"])
            upper = float(admissible_range["upper"])
            value = float(params[name])
            if not all(math.isfinite(item) for item in (lower, upper, value)):
                raise ValueError(f"{name}: similarity inputs must be finite")
            if lower >= upper:
                raise ValueError(f"{name}: admissible range requires lower < upper")

            if lower > 0.0 and upper > 0.0:
                if value <= 0.0:
                    raise ValueError(f"{name}: positive log range requires value > 0")
                coordinate = (
                    (math.log10(value) - math.log10(lower))
                    / (math.log10(upper) - math.log10(lower))
                )
            elif lower < 0.0 and upper < 0.0:
                if value >= 0.0:
                    raise ValueError(f"{name}: negative log range requires value < 0")
                magnitude_low = min(abs(lower), abs(upper))
                magnitude_high = max(abs(lower), abs(upper))
                coordinate = (
                    (math.log10(abs(value)) - math.log10(magnitude_low))
                    / (math.log10(magnitude_high) - math.log10(magnitude_low))
                )
            else:
                coordinate = (value - lower) / (upper - lower)

        if not math.isfinite(coordinate):
            raise ValueError(f"{name}: similarity coordinate must be finite")
        vector.append(float(coordinate))
    return vector
