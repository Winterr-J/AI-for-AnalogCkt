import numpy as np


def retrieve_top_k(
    query_vector,
    feature_vectors,
    k=3,
) -> list[int]:

    if k < 0:
        raise ValueError("k must be non-negative")

    query = np.asarray(query_vector, dtype=float)
    features = np.asarray(feature_vectors, dtype=float)

    if len(features) == 0 or k == 0:
        return []

    if query.ndim != 1:
        raise ValueError("query_vector must be one-dimensional")
    if features.ndim != 2:
        raise ValueError("feature_vectors must be two-dimensional")

    if features.shape[1] != len(query):
        raise ValueError("query and feature dimensions must match")

    if not np.all(np.isfinite(query)) or not np.all(np.isfinite(features)):
        raise ValueError("inputs must be finite")

    distances = np.linalg.norm(features - query, axis=1)

    indices = np.argsort(
        distances,
        kind="stable",
    )

    return [
        int(i)
        for i in indices[:min(k, len(indices))]
    ]
