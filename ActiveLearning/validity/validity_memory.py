"""Validity V1: real-observation memory and soft local invalidity risk."""

import math
from collections.abc import Mapping
from numbers import Real

from ActiveLearning.common import parameter_to_similarity_vector


VALIDITY_V1_DEFAULTS = {
    "enabled": True,
    "k": 5,
    "epsilon": 1.0e-12,
    "mode": "performance_tiebreak",
}


def binary_validity(record, performance_template):
    """Whether a real evaluation is a usable, functionally valid observation.

    ``functional_feasible`` is the legacy spelling. Legacy performance-only
    evaluations explicitly carry this field as None because no functional
    check was run; their finite measured metrics remain usable observations.
    """
    status = record.get("status")
    metrics = record.get("metrics")
    if not isinstance(status, Mapping) or not isinstance(metrics, Mapping):
        return False
    if "functional_valid" in status:
        functional_ok = status["functional_valid"] is True
    elif status.get("functional_feasible") is not None:
        functional_ok = status["functional_feasible"] is True
    else:
        # The legacy performance-only AL evaluator records an explicit None.
        # No functional verdict exists to contradict a finite real measurement.
        functional_ok = "functional_feasible" in status
    if not (
        status.get("parameter_valid") is True
        and status.get("simulation_valid") is True
        and functional_ok
    ):
        return False
    metric_names = performance_template["performance_template"]["active_metrics"]
    return all(
        isinstance(metrics.get(name), Real)
        and not isinstance(metrics[name], bool)
        and math.isfinite(float(metrics[name]))
        for name in metric_names
    )


class ValidityMemory:
    """One independent run's D0 plus its own subsequently evaluated points."""

    def __init__(
        self,
        initial_records,
        parameter_template,
        performance_template,
        *,
        run_id=None,
        k=5,
        epsilon=1.0e-12,
    ):
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError("validity k must be a positive integer")
        if (
            isinstance(epsilon, bool)
            or not isinstance(epsilon, Real)
            or not math.isfinite(float(epsilon))
            or epsilon <= 0
        ):
            raise ValueError("validity epsilon must be finite and positive")
        self.parameter_template = parameter_template
        self.performance_template = performance_template
        self.run_id = run_id
        self.k = k
        self.epsilon = float(epsilon)
        self.entries = []
        self._vectors = []
        for record in initial_records:
            # A record without params is not a real evaluated D0 observation.
            if isinstance(record.get("params"), Mapping):
                self.append(record, source="D0", iteration=None)

    def append(self, record, *, source="AL", iteration=None):
        params = record["params"]
        vector = parameter_to_similarity_vector(
            params,
            self.parameter_template,
            normalized_params=record.get("normalized_params"),
        )
        entry = {
            "sample_id": record["sample_id"],
            "params": dict(params),
            "binary_validity": binary_validity(record, self.performance_template),
            "source": source,
            "run_id": self.run_id if source == "AL" else None,
            "iteration": iteration,
        }
        self.entries.append(entry)
        self._vectors.append(vector)
        return entry

    def risk(self, candidate):
        """Inverse-distance invalidity fraction among the nearest k points."""
        if not self.entries:
            return {
                "validity_risk": 0.0,
                "validity_k": 0,
                "invalid_neighbor_count": 0,
                "invalid_neighbor_fraction": 0.0,
                "nearest_neighbor_distance": None,
            }
        vector = parameter_to_similarity_vector(
            candidate["params"],
            self.parameter_template,
            normalized_params=candidate.get("normalized_params"),
        )
        distances = [
            math.dist(vector, historical_vector)
            for historical_vector in self._vectors
        ]
        neighbor_indices = sorted(
            range(len(self.entries)), key=lambda index: (distances[index], index)
        )[: self.k]
        weights = [
            1.0 / (distances[index] + self.epsilon)
            for index in neighbor_indices
        ]
        invalid_count = sum(
            not self.entries[index]["binary_validity"]
            for index in neighbor_indices
        )
        risk = sum(
            weight * (not self.entries[index]["binary_validity"])
            for index, weight in zip(neighbor_indices, weights)
        ) / sum(weights)
        return {
            "validity_risk": risk,
            "validity_k": len(neighbor_indices),
            "invalid_neighbor_count": invalid_count,
            "invalid_neighbor_fraction": invalid_count / len(neighbor_indices),
            "nearest_neighbor_distance": distances[neighbor_indices[0]],
        }
