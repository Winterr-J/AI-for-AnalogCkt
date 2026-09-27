"""Generic deterministic Maximal Marginal Relevance selection."""

from collections.abc import Sequence

import numpy as np


def select_mmr(
    relevance_scores: Sequence[float],
    feature_vectors: Sequence[Sequence[float]],
    k: int,
    lambda_weight: float = 0.7,
) -> list[int]:
    """Return indices selected by Maximal Marginal Relevance."""

    if k < 0:
        raise ValueError("k must be non-negative")
    if not 0.0 <= lambda_weight <= 1.0:
        raise ValueError("lambda_weight must be within [0, 1]")

    relevance = np.asarray(relevance_scores, dtype=float)
    features = np.asarray(feature_vectors, dtype=float)

    if relevance.size == 0:
        return []

    if relevance.ndim != 1 or features.ndim != 2:
        raise ValueError("relevance must be 1D and features must be 2D")
    if len(relevance) != len(features):
        raise ValueError("relevance and features must have equal length")
    if not np.all(np.isfinite(relevance)) or not np.all(np.isfinite(features)):
        raise ValueError("inputs must be finite")
    if np.any((relevance < 0) | (relevance > 1)):
        raise ValueError("relevance must be within [0, 1]")

    limit = min(k, len(relevance))
    if limit == 0:
        return []

    distances = np.linalg.norm(
        features[:, None, :] - features[None, :, :],
        axis=2,
    )
    similarities = 1.0 / (1.0 + distances)

    selected = [int(np.argmax(relevance))]
    remaining = [i for i in range(len(relevance)) if i not in selected]

    while len(selected) < limit:
        chosen = max(
            remaining,
            key=lambda i: (
                lambda_weight * relevance[i]
                - (1 - lambda_weight)
                * max(similarities[i, j] for j in selected),
                -i,
            ),
        )

        selected.append(chosen)
        remaining.remove(chosen)

    return selected