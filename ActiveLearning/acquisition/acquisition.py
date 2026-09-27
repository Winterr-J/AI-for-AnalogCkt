"""Target-centered multi-objective acquisition."""


__all__ = ["select_next_candidate"]


def select_next_candidate(
    predictions,
    performance_template,
    target_specs,
    *,
    beta=0.7,
    return_scored=False,
):
    """Return the candidate with the largest violation/uncertainty score."""
    if not 0 <= beta <= 1:
        raise ValueError("beta must be in [0, 1]")

    template = performance_template["performance_template"]
    metric_names = template["active_metrics"]
    scored = []

    for candidate in predictions:
        metric_scores = {}
        for name in metric_names:
            target = float(target_specs[name]["value"])
            if target == 0:
                raise ValueError(f"target for {name!r} must be non-zero")

            direction = template["metrics"][name]["direction"]
            prediction = candidate["prediction"][name]
            if direction == "minimize":
                normalized_violation = max(
                    0.0, (prediction["mean"] - target) / abs(target)
                )
            elif direction == "maximize":
                normalized_violation = max(
                    0.0, (target - prediction["mean"]) / abs(target)
                )
            else:
                raise KeyError(direction)
            metric_scores[name] = {
                "normalized_violation": normalized_violation,
                "normalized_std": prediction["std"] / abs(target),
            }

        violation_score = sum(
            score["normalized_violation"] for score in metric_scores.values()
        ) / len(metric_names)
        uncertainty_score = sum(
            score["normalized_std"] for score in metric_scores.values()
        ) / len(metric_names)
        acquisition_score = (
            -beta * violation_score + (1 - beta) * uncertainty_score
        )
        scored.append(
            {
                "candidate_id": candidate["candidate_id"],
                "params": candidate["params"],
                "violation_score": violation_score,
                "uncertainty_score": uncertainty_score,
                "acquisition_score": acquisition_score,
                "metric_scores": metric_scores,
            }
        )

    selected = max(scored, key=lambda candidate: candidate["acquisition_score"])
    if return_scored:
        return selected, scored
    return selected
