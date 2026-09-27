"""Expand one inverse-design seed into a diverse candidate set."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from ActiveLearning.InitializeD0.sampling import (
    denormalize_parameters,
    normalize_parameters,
    sample_global_normalized,
    sample_physical_local,
)
from ActiveLearning.InitializeD0.validation import validate_parameters
from ActiveLearning.common import select_mmr


__all__ = ["expand_candidates"]


def expand_candidates(
    x_seed: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
    *,
    n_local: int = 80,
    n_global: int = 20,
    local_factor: float = 1.25,
    k: int = 10,
    mmr_lambda: float = 0.7,
    seed: int = 42,
    historical_records=None,
    return_diagnostics: bool = False,
):
    """Sample, validate, and MMR-select candidates around ``x_seed``."""

    parameter_order = list(parameter_template["optimization_space"]["variables"])
    normalized_seed = normalize_parameters(x_seed, parameter_template)
    seed_vector = np.asarray(
        [normalized_seed[name] for name in parameter_order],
        dtype=float,
    )
    rng = np.random.default_rng(seed)

    local_samples = sample_physical_local(
        x_seed,
        parameter_template,
        n_local,
        local_factor,
        rng,
    )
    global_samples = sample_global_normalized(
        parameter_template,
        n_global,
        rng,
    )

    raw_candidates = []

    def add_raw_candidate(params, normalized_params, source):
        raw_candidates.append(
            {
                "candidate_id": f"candidate_{len(raw_candidates):04d}",
                "sampling_source": source,
                "params": params,
                "normalized_params": normalized_params,
            }
        )

    for params in local_samples:
        add_raw_candidate(
            params,
            normalize_parameters(params, parameter_template),
            "local",
        )
    for normalized_params in global_samples:
        add_raw_candidate(
            denormalize_parameters(normalized_params, parameter_template),
            normalized_params,
            "global",
        )

    def parameter_vector(params):
        try:
            return tuple(float(params[name]) for name in parameter_order)
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
        if status.get("parameter_valid") is not True:
            continue
        if status.get("simulation_valid") is not True:
            continue
        vector = parameter_vector(record.get("params"))
        if vector is not None:
            historical_vectors.append(vector)

    diagnostics = {
        "filtered_historical_duplicates": 0,
        "filtered_within_pool_duplicates": 0,
    }
    accepted_vectors = []

    def accept_candidate(candidate):
        if validate_parameters(candidate["params"], parameter_template)[
            "parameter_valid"
        ] is not True:
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

    valid_candidates = [
        candidate for candidate in raw_candidates if accept_candidate(candidate)
    ]

    source_quotas = {
        "local": min(8, k),
        "global": min(2, max(0, k - 8)),
    }
    requested_by_source = {"local": n_local, "global": n_global}
    valid_by_source = {
        source: [
            candidate
            for candidate in valid_candidates
            if candidate["sampling_source"] == source
        ]
        for source in ("local", "global")
    }

    max_top_up_attempts = max(100, 10 * (n_local + n_global))
    for source in ("local", "global"):
        quota = source_quotas[source]
        if requested_by_source[source] < quota:
            continue
        attempts = 0
        while len(valid_by_source[source]) < quota and attempts < max_top_up_attempts:
            attempts += 1
            if source == "local":
                params = sample_physical_local(
                    x_seed,
                    parameter_template,
                    1,
                    local_factor,
                    rng,
                )[0]
                normalized_params = normalize_parameters(params, parameter_template)
            else:
                normalized_params = sample_global_normalized(
                    parameter_template,
                    1,
                    rng,
                )[0]
                params = denormalize_parameters(
                    normalized_params,
                    parameter_template,
                )
            add_raw_candidate(params, normalized_params, source)
            candidate = raw_candidates[-1]
            if accept_candidate(candidate):
                valid_candidates.append(candidate)
                valid_by_source[source].append(candidate)

    if not valid_candidates:
        raise ValueError("No valid candidates remain after parameter validation")
    selected = []
    for source in ("local", "global"):
        pool = valid_by_source[source]
        feature_vectors = [
            [candidate["normalized_params"][name] for name in parameter_order]
            for candidate in pool
        ]
        seed_relevance = []
        for candidate, feature_vector in zip(pool, feature_vectors):
            distance = float(
                np.linalg.norm(np.asarray(feature_vector, dtype=float) - seed_vector)
            )
            relevance = 1.0 / (1.0 + distance)
            candidate["seed_relevance"] = relevance
            seed_relevance.append(relevance)

        selection_relevance = (
            seed_relevance if source == "local" else [1.0] * len(pool)
        )
        selected_indices = select_mmr(
            selection_relevance,
            feature_vectors,
            k=source_quotas[source],
            lambda_weight=mmr_lambda,
        )
        selected.extend(pool[index] for index in selected_indices)

    if return_diagnostics:
        return selected, diagnostics
    return selected
