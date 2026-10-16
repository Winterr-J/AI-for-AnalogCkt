"""V2: bounded full-vector local expansion in D0 V4's log-aware space.

Data Expansion controls geometric scale; acquisition ranks performance,
Validity supplies loose risk preference, and the unchanged global sampler
preserves long-range exploration. No profiling samples or state are loaded.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from ActiveLearning.InitializeD0.sampling import (
    denormalize_parameters,
    normalize_parameters,
    sample_global_normalized,
)
from ActiveLearning.InitializeD0.validation import validate_parameters
from ActiveLearning.common import select_mmr
from ActiveLearning.focused_profiling import _physical_to_unit
from ActiveLearning.profiling import _unit_to_physical
from ActiveLearning.profiling_local_sampler import sample_local_norm_bounded


DEFAULT_RAW = {"near_local": 60, "broad_local": 20, "global": 20}
DEFAULT_RADIUS = {"near": 0.005, "broad": 0.02}
DEFAULT_RETAINED = {"near_local": 5, "broad_local": 3, "global": 2}
SOURCES = ("near_local", "broad_local", "global")


def _space(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Use the same template scale/transform fields consumed by D0 V4."""
    definitions = {item["name"]: item for item in template["parameters"]}
    space = []
    for name in template["optimization_space"]["variables"]:
        definition = definitions[name]
        bounds = definition["admissible_range"]
        space.append(
            {
                "name": name,
                "lower": float(bounds["lower"]),
                "upper": float(bounds["upper"]),
                "scale": bounds.get("scale"),
                "transform": bounds.get("transform"),
                "fixed_sign": bounds.get("fixed_sign"),
            }
        )
    return space


def _unit_vector(params: Mapping[str, Any], space: list[dict[str, Any]]) -> np.ndarray:
    return np.asarray(
        [_physical_to_unit(params[item["name"]], item) for item in space],
        dtype=float,
    )


def _read_config(config: Mapping[str, Any] | None, k: int):
    options = config or {}
    raw = {**DEFAULT_RAW, **(options.get("raw") or {})}
    radius = {**DEFAULT_RADIUS, **(options.get("radius") or {})}
    retained = {**DEFAULT_RETAINED, **(options.get("retained") or {})}
    for source in SOURCES:
        if not isinstance(raw[source], int) or raw[source] < retained[source]:
            raise ValueError(f"V2 raw.{source} must be at least retained.{source}")
        if not isinstance(retained[source], int) or retained[source] < 0:
            raise ValueError(f"V2 retained.{source} must be non-negative")
    if sum(retained.values()) != k:
        raise ValueError("V2 retained quotas must sum to k")
    for name in ("near", "broad"):
        value = float(radius[name])
        if not math.isfinite(value) or not 0.0 < value <= 1.0:
            raise ValueError(f"V2 radius.{name} must satisfy 0 < radius <= 1")
        radius[name] = value
    if radius["near"] >= radius["broad"]:
        raise ValueError("V2 near radius must be less than broad radius")
    return raw, radius, retained


def _distance_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "p10": None, "p25": None,
                "median": None, "p75": None, "p90": None, "max": None}
    data = np.asarray(values, dtype=float)
    return {
        "count": len(values),
        "min": float(np.min(data)),
        "p10": float(np.percentile(data, 10)),
        "p25": float(np.percentile(data, 25)),
        "median": float(np.median(data)),
        "p75": float(np.percentile(data, 75)),
        "p90": float(np.percentile(data, 90)),
        "max": float(np.max(data)),
    }


def expand_candidates_v2(
    x_seed: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
    *,
    k: int,
    mmr_lambda: float,
    seed: int,
    historical_records,
    return_diagnostics: bool,
    config: Mapping[str, Any] | None,
):
    raw_counts, radii, quotas = _read_config(config, k)
    space = _space(parameter_template)
    names = [item["name"] for item in space]
    seed_vector = _unit_vector(x_seed, space)
    # Stable independent streams: iteration seeds remain 42, 43, ... upstream.
    near_rng, broad_rng, global_rng = (
        np.random.default_rng(child)
        for child in np.random.SeedSequence(seed).spawn(3)
    )
    rng_by_source = {
        "near_local": near_rng,
        "broad_local": broad_rng,
        "global": global_rng,
    }
    radius_by_source = {"near_local": radii["near"], "broad_local": radii["broad"]}
    raw_candidates: list[dict[str, Any]] = []
    raw_distances = {source: [] for source in SOURCES}

    def add_candidate(source: str, params: Mapping[str, Any],
                      normalized: Mapping[str, Any], requested_radius: float | None):
        feature_vector = _unit_vector(params, space)
        distance = float(np.linalg.norm(feature_vector - seed_vector))
        if requested_radius is not None and distance > requested_radius + 1.0e-10:
            raise RuntimeError(f"V2 {source} candidate exceeded requested radius")
        candidate = {
            "candidate_id": f"candidate_{len(raw_candidates):04d}",
            "sampling_source": source,
            "source": source,
            "params": dict(params),
            # Pipeline compatibility: this field retains the legacy linear map.
            "normalized_params": dict(normalized),
            "requested_radius": requested_radius,
            "actual_seed_distance": distance,
            "sampling_mode": "v2",
            "_feature_vector": feature_vector.tolist(),
        }
        raw_candidates.append(candidate)
        raw_distances[source].append(distance)
        return candidate

    def draw(source: str, count: int):
        if source == "global":
            for normalized in sample_global_normalized(
                parameter_template, count, rng_by_source[source]
            ):
                params = denormalize_parameters(normalized, parameter_template)
                yield add_candidate(source, params, normalized, None)
        else:
            radius = radius_by_source[source]
            vectors, _metadata = sample_local_norm_bounded(
                seed_vector, radius, count, rng_by_source[source]
            )
            for vector in vectors:
                params = {
                    item["name"]: _unit_to_physical(float(value), item)
                    for item, value in zip(space, vector)
                }
                normalized = normalize_parameters(params, parameter_template)
                yield add_candidate(source, params, normalized, radius)

    for source in SOURCES:
        for _ in draw(source, raw_counts[source]):
            pass

    def parameter_vector(params):
        try:
            return tuple(float(params[name]) for name in names)
        except (KeyError, TypeError, ValueError):
            return None

    def same_vector(left, right):
        return all(
            math.isclose(a, b, rel_tol=1.0e-12, abs_tol=1.0e-15)
            for a, b in zip(left, right)
        )

    historical_vectors = []
    for record in historical_records or ():
        status = record.get("status")
        if not isinstance(status, Mapping):
            continue
        if status.get("parameter_valid") is not True or status.get("simulation_valid") is not True:
            continue
        vector = parameter_vector(record.get("params"))
        if vector is not None:
            historical_vectors.append(vector)

    diagnostics = {
        "filtered_historical_duplicates": 0,
        "filtered_within_pool_duplicates": 0,
        "raw_pool_distances": {
            source: _distance_summary(raw_distances[source][:raw_counts[source]])
            for source in SOURCES
        },
        "raw_pool_counts": dict(raw_counts),
    }
    accepted_vectors = []

    def accept(candidate):
        if validate_parameters(candidate["params"], parameter_template)["parameter_valid"] is not True:
            return False
        vector = parameter_vector(candidate["params"])
        if vector is None:
            return False
        if any(same_vector(vector, known) for known in historical_vectors):
            diagnostics["filtered_historical_duplicates"] += 1
            return False
        if any(same_vector(vector, known) for known in accepted_vectors):
            diagnostics["filtered_within_pool_duplicates"] += 1
            return False
        accepted_vectors.append(vector)
        return True

    valid_by_source = {source: [] for source in SOURCES}
    for candidate in raw_candidates:
        if accept(candidate):
            valid_by_source[candidate["sampling_source"]].append(candidate)

    max_top_up_attempts = max(100, 10 * sum(raw_counts.values()))
    for source in SOURCES:
        attempts = 0
        while len(valid_by_source[source]) < quotas[source] and attempts < max_top_up_attempts:
            attempts += 1
            candidate = next(draw(source, 1))
            if accept(candidate):
                valid_by_source[source].append(candidate)
        if len(valid_by_source[source]) < quotas[source]:
            raise ValueError(f"Insufficient valid V2 {source} candidates for quota")

    selected = []
    for source in SOURCES:
        pool = valid_by_source[source]
        features = [candidate["_feature_vector"] for candidate in pool]
        relevance = []
        for candidate in pool:
            value = 1.0 / (1.0 + candidate["actual_seed_distance"])
            candidate["seed_relevance"] = value
            relevance.append(value)
        # Near exploits the seed; broad/global use constant relevance so
        # unchanged MMR diversity, not seed proximity, spreads those pools.
        indices = select_mmr(
            relevance if source == "near_local" else [1.0] * len(pool),
            features,
            k=quotas[source],
            lambda_weight=mmr_lambda,
        )
        for index in indices:
            candidate = dict(pool[index])
            candidate.pop("_feature_vector")
            selected.append(candidate)

    diagnostics["retained_pool_distances"] = {
        source: _distance_summary(
            [item["actual_seed_distance"] for item in selected
             if item["sampling_source"] == source]
        )
        for source in SOURCES
    }
    if return_diagnostics:
        return selected, diagnostics
    return selected
