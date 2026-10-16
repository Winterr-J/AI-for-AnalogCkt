"""Dimension-independent local geometry for focused SPICE profiling.

The radius is the maximum L2 displacement of the *whole* vector in the
existing log-aware normalized parameter space. Directions are isotropic
normalized Gaussians; radii are uniform on [0, r]. This intentionally covers
near, intermediate, and outer distances. It is not volume-uniform ball sampling.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np


SAMPLER_VERSION = "profiling_local_v2"
SAMPLER_METADATA = {
    "sampler_version": SAMPLER_VERSION,
    "sampling_space": "log_aware_normalized",
    "distance_metric": "l2",
    "direction_distribution": "normalized_gaussian",
    "radial_distribution": "uniform_radius",
    "radius_semantics": "maximum full-vector normalized log-aware L2 displacement",
    "boundary_handling": "reject_resample_then_directional_contraction",
    "boundary_fallback": "reflect outward components only if contraction would be zero",
}


def _safe_radius(anchor: np.ndarray, direction: np.ndarray) -> float:
    """Largest step along one direction staying inside the unit hypercube."""

    positive = direction > 0.0
    negative = direction < 0.0
    limits = []
    if np.any(positive):
        limits.append(float(np.min((1.0 - anchor[positive]) / direction[positive])))
    if np.any(negative):
        limits.append(float(np.min(anchor[negative] / -direction[negative])))
    return min(limits, default=float("inf"))


def sample_local_norm_bounded(
    anchor: Sequence[float],
    radius: float,
    n_samples: int,
    rng: np.random.Generator,
    *,
    max_rejection_attempts: int = 256,
    include_sample_details: bool = False,
) -> tuple[list[np.ndarray], dict[str, Any]]:
    """Draw deterministic unit-cube points with full-vector L2 distance <= radius.

    A candidate crossing a boundary is rejected. After bounded retries, its
    sampled direction is retained and its radius is contracted to the boundary.
    At an exact corner, an outward direction has zero safe length; only then
    outward components are reflected inward before directional contraction.
    No coordinate is independently clipped.
    """

    center = np.asarray(anchor, dtype=float).reshape(-1)
    if center.size == 0 or not np.all(np.isfinite(center)) or np.any((center < 0) | (center > 1)):
        raise ValueError("anchor must be a finite nonempty vector in [0,1]^d")
    if not np.isfinite(radius) or not 0.0 < radius <= 1.0:
        raise ValueError("radius must satisfy 0 < radius <= 1")
    if n_samples < 0:
        raise ValueError("n_samples must be non-negative")
    if max_rejection_attempts < 1:
        raise ValueError("max_rejection_attempts must be positive")

    points: list[np.ndarray] = []
    rejected = 0
    contracted = 0
    reflected = 0
    sample_details: list[dict[str, Any]] = []
    for _ in range(n_samples):
        last_direction = None
        last_rho = None
        for _attempt in range(max_rejection_attempts):
            raw = rng.normal(size=center.size)
            length = float(np.linalg.norm(raw))
            if not np.isfinite(length) or length == 0.0:
                rejected += 1
                continue
            direction = raw / length
            rho = float(rng.uniform(0.0, radius))
            candidate = center + rho * direction
            last_direction, last_rho = direction, rho
            if np.all((candidate >= 0.0) & (candidate <= 1.0)):
                points.append(candidate)
                if include_sample_details:
                    sample_details.append(
                        {"rho": rho, "actual_normalized_l2_distance": float(np.linalg.norm(candidate - center)), "boundary_fallback": False}
                    )
                break
            rejected += 1
        else:
            if last_direction is None or last_rho is None:
                raise RuntimeError("Unable to generate a finite local direction")
            direction = last_direction.copy()
            safe = _safe_radius(center, direction)
            if safe <= 0.0:
                lower_outward = (center <= 0.0) & (direction < 0.0)
                upper_outward = (center >= 1.0) & (direction > 0.0)
                direction[lower_outward | upper_outward] *= -1.0
                reflected += 1
                safe = _safe_radius(center, direction)
            if safe <= 0.0:
                raise RuntimeError("No nonzero boundary-safe local step is available")
            rho = min(last_rho, float(np.nextafter(safe, 0.0)))
            candidate = center + rho * direction
            if not np.all((candidate >= 0.0) & (candidate <= 1.0)):
                raise RuntimeError("Directional contraction failed to preserve bounds")
            points.append(candidate)
            contracted += 1
            if include_sample_details:
                sample_details.append(
                    {"rho": last_rho, "actual_normalized_l2_distance": float(np.linalg.norm(candidate - center)), "boundary_fallback": True}
                )

    metadata = {
        **SAMPLER_METADATA,
        "radius": float(radius),
        "dimension": int(center.size),
        "requested_samples": int(n_samples),
        "rejected_out_of_bounds": rejected,
        "directional_contractions": contracted,
        "fallback_reflections": reflected,
    }
    if include_sample_details:
        metadata["sample_details"] = sample_details
    return points, metadata
