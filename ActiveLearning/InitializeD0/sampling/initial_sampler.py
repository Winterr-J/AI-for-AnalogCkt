"""Circuit-agnostic V1 initial sampling in normalized parameter space."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np


def _extract_space(parameter_template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract continuous optimization variables in their declared order."""

    variable_names = parameter_template["optimization_space"]["variables"]
    parameters_by_name = {
        parameter["name"]: parameter for parameter in parameter_template["parameters"]
    }

    space = []
    for name in variable_names:
        parameter = parameters_by_name[name]
        if parameter["variable_type"] != "continuous":
            raise NotImplementedError("Only continuous variables are supported.")

        nominal = float(parameter["nominal_value"])
        lower = float(parameter["admissible_range"]["lower"])
        upper = float(parameter["admissible_range"]["upper"])
        if lower >= upper:
            raise ValueError(f"{name}: admissible range requires lower < upper")
        if not lower <= nominal <= upper:
            raise ValueError(f"{name}: nominal value is outside the admissible range")

        space.append(
            {
                "name": name,
                "nominal": nominal,
                "lower": lower,
                "upper": upper,
            }
        )
    return space


def normalize_parameters(
    params: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
) -> dict[str, float]:
    """Map real parameters to normalized coordinates."""

    return {
        parameter["name"]: float(
            (params[parameter["name"]] - parameter["lower"])
            / (parameter["upper"] - parameter["lower"])
        )
        for parameter in _extract_space(parameter_template)
    }


def denormalize_parameters(
    normalized_params: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
) -> dict[str, float]:
    """Map normalized coordinates in [0, 1] to real parameters."""

    params = {}
    for parameter in _extract_space(parameter_template):
        name = parameter["name"]
        normalized = float(normalized_params[name])
        if not 0.0 <= normalized <= 1.0:
            raise ValueError(f"{name}: normalized value must be within [0, 1]")
        params[name] = float(
            parameter["lower"]
            + normalized * (parameter["upper"] - parameter["lower"])
        )
    return params


def _global_normalized_sample(
    space: list[dict[str, Any]],
    rng: np.random.Generator,
) -> dict[str, float]:
    return {
        parameter["name"]: float(value)
        for parameter, value in zip(space, rng.random(len(space)))
    }


def sample_global_normalized(
    parameter_template: Mapping[str, Any],
    n_samples: int,
    rng: np.random.Generator,
) -> list[dict[str, float]]:
    """Sample normalized points uniformly over the full parameter space."""

    if n_samples < 0:
        raise ValueError("n_samples must be non-negative")
    space = _extract_space(parameter_template)
    return [_global_normalized_sample(space, rng) for _ in range(n_samples)]


def sample_local_normalized(
    center: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
    n_samples: int,
    radius: float,
    rng: np.random.Generator,
) -> list[dict[str, float]]:
    """Sample a clipped uniform box around one normalized-space center."""

    if n_samples < 0:
        raise ValueError("n_samples must be non-negative")
    if not 0.0 < radius <= 1.0:
        raise ValueError("radius must satisfy 0 < radius <= 1")

    space = _extract_space(parameter_template)
    center_vector = np.asarray(
        [center[parameter["name"]] for parameter in space],
        dtype=float,
    )
    if not np.all(np.isfinite(center_vector)) or np.any(
        (center_vector < 0.0) | (center_vector > 1.0)
    ):
        raise ValueError("normalized center must be finite and within [0, 1]")

    local_lower = np.maximum(0.0, center_vector - radius)
    local_upper = np.minimum(1.0, center_vector + radius)
    return [
        {
            parameter["name"]: float(value)
            for parameter, value in zip(
                space,
                rng.uniform(local_lower, local_upper),
            )
        }
        for _ in range(n_samples)
    ]


def generate_v1_global_samples(
    parameter_template: Mapping[str, Any],
    n_samples: int = 20,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Generate one nominal anchor followed by global uniform samples."""

    if n_samples < 1:
        raise ValueError("n_samples must be at least 1")

    space = _extract_space(parameter_template)
    anchor_params = {
        parameter["name"]: parameter["nominal"] for parameter in space
    }
    samples = [
        {
            "sample_id": "d0_v1_000",
            "sampling_source": "anchor",
            "params": anchor_params,
            "normalized_params": normalize_parameters(
                anchor_params,
                parameter_template,
            ),
        }
    ]

    rng = np.random.default_rng(seed)
    normalized_samples = sample_global_normalized(
        parameter_template,
        n_samples - 1,
        rng,
    )
    for index, normalized_params in enumerate(normalized_samples, start=1):
        samples.append(
            {
                "sample_id": f"d0_v1_{index:03d}",
                "sampling_source": "global",
                "params": denormalize_parameters(
                    normalized_params,
                    parameter_template,
                ),
                "normalized_params": normalized_params,
            }
        )
    return samples


def generate_v2_local_global_samples(
    parameter_template: Mapping[str, Any],
    n_local: int = 10,
    n_global: int = 9,
    local_radius: float = 0.05,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Generate one anchor, then local and global normalized-space samples."""

    if n_local < 0 or n_global < 0:
        raise ValueError("n_local and n_global must be non-negative")
    if not 0.0 < local_radius <= 1.0:
        raise ValueError("local_radius must satisfy 0 < local_radius <= 1")

    space = _extract_space(parameter_template)
    anchor_params = {
        parameter["name"]: parameter["nominal"] for parameter in space
    }
    anchor_normalized = normalize_parameters(anchor_params, parameter_template)
    samples = [
        {
            "sample_id": "d0_v2_000",
            "sampling_source": "anchor",
            "params": anchor_params,
            "normalized_params": anchor_normalized,
        }
    ]

    rng = np.random.default_rng(seed)

    local_samples = sample_local_normalized(
        anchor_normalized,
        parameter_template,
        n_local,
        local_radius,
        rng,
    )
    for index, normalized_params in enumerate(local_samples, start=1):
        samples.append(
            {
                "sample_id": f"d0_v2_{index:03d}",
                "sampling_source": "local",
                "params": denormalize_parameters(
                    normalized_params,
                    parameter_template,
                ),
                "normalized_params": normalized_params,
            }
        )

    global_samples = sample_global_normalized(
        parameter_template,
        n_global,
        rng,
    )
    for normalized_params in global_samples:
        index = len(samples)
        samples.append(
            {
                "sample_id": f"d0_v2_{index:03d}",
                "sampling_source": "global",
                "params": denormalize_parameters(
                    normalized_params,
                    parameter_template,
                ),
                "normalized_params": normalized_params,
            }
        )
    return samples


def _sample_physical_local_params(
    space: list[dict[str, Any]],
    local_factor: float,
    rng: np.random.Generator,
    center_params: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    params = {}
    for parameter in space:
        name = parameter["name"]
        center = (
            parameter["nominal"]
            if center_params is None
            else float(center_params[name])
        )
        lower = parameter["lower"]
        upper = parameter["upper"]
        if not np.isfinite(center) or not lower <= center <= upper:
            raise ValueError(f"{name}: local center is outside the admissible range")

        if center > 0.0 and lower >= 0.0:
            local_lower = max(lower, center / local_factor)
            local_upper = min(upper, center * local_factor)
            value = float(
                np.exp(rng.uniform(np.log(local_lower), np.log(local_upper)))
            )
        elif center < 0.0 and upper <= 0.0:
            magnitude_lower = max(abs(upper), abs(center) / local_factor)
            magnitude_upper = min(abs(lower), abs(center) * local_factor)
            magnitude = float(
                np.exp(
                    rng.uniform(
                        np.log(magnitude_lower),
                        np.log(magnitude_upper),
                    )
                )
            )
            value = -magnitude
        else:
            half_width = abs(center) * (local_factor - 1.0)
            if half_width == 0.0:
                half_width = (
                    (upper - lower) * (local_factor - 1.0) / (2.0 * local_factor)
                )
            local_lower = max(lower, center - half_width)
            local_upper = min(upper, center + half_width)
            value = float(rng.uniform(local_lower, local_upper))

        params[name] = value
    return params


def sample_physical_local(
    center_params: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
    n_samples: int,
    local_factor: float,
    rng: np.random.Generator,
) -> list[dict[str, float]]:
    """Sample physical-local points around an arbitrary parameter center."""

    if n_samples < 0:
        raise ValueError("n_samples must be non-negative")
    if not np.isfinite(local_factor) or local_factor <= 1.0:
        raise ValueError("local_factor must be finite and greater than 1")
    space = _extract_space(parameter_template)
    return [
        _sample_physical_local_params(
            space,
            local_factor,
            rng,
            center_params=center_params,
        )
        for _ in range(n_samples)
    ]


def generate_v3_physical_local_global_samples(
    parameter_template: Mapping[str, Any],
    n_local: int = 10,
    n_global: int = 9,
    local_factor: float = 1.25,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Generate one nominal anchor, physical-local samples, and global samples."""

    if n_local < 0 or n_global < 0:
        raise ValueError("n_local and n_global must be non-negative")
    if not np.isfinite(local_factor) or local_factor <= 1.0:
        raise ValueError("local_factor must be finite and greater than 1")

    space = _extract_space(parameter_template)
    anchor_params = {
        parameter["name"]: parameter["nominal"] for parameter in space
    }
    samples = [
        {
            "sample_id": "d0_v3_000",
            "sampling_source": "nominal",
            "params": anchor_params,
            "normalized_params": normalize_parameters(
                anchor_params,
                parameter_template,
            ),
        }
    ]

    rng = np.random.default_rng(seed)
    for _ in range(n_local):
        params = _sample_physical_local_params(space, local_factor, rng)
        index = len(samples)
        samples.append(
            {
                "sample_id": f"d0_v3_{index:03d}",
                "sampling_source": "local",
                "params": params,
                "normalized_params": normalize_parameters(
                    params,
                    parameter_template,
                ),
            }
        )

    global_samples = sample_global_normalized(
        parameter_template,
        n_global,
        rng,
    )
    for normalized_params in global_samples:
        index = len(samples)
        samples.append(
            {
                "sample_id": f"d0_v3_{index:03d}",
                "sampling_source": "global",
                "params": denormalize_parameters(
                    normalized_params,
                    parameter_template,
                ),
                "normalized_params": normalized_params,
            }
        )
    return samples
