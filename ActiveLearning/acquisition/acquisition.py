"""Conservative feasibility-first multi-objective acquisition."""

import math


__all__ = ["select_next_candidate", "SEMANTIC_NEAR_TIE_TAU"]

_EPSILON = 1.0e-12
_CONSERVATIVE_MODE = "conservative_feasibility"
_MEAN_ONLY_MODE = "mean_only_feasibility"
_UNCERTAINTY_REWARD_MODE = "uncertainty_reward"
_SUPPORTED_MODES = {
    _CONSERVATIVE_MODE,
    _MEAN_ONLY_MODE,
    _UNCERTAINTY_REWARD_MODE,
}
_V1_VALIDITY_MODE = "performance_tiebreak"
_V1_1_VALIDITY_MODE = "semantic_near_tie"
SEMANTIC_NEAR_TIE_TAU = 0.025


def _performance_semantic_class(candidate):
    """Classify only from the acquisition diagnostics already computed."""
    if candidate["risk_violation_score"] == 0:
        return "CF"
    if candidate["mean_only_violation_score"] == 0:
        return "MF"
    return "IF"


def _select_semantic_near_tie(scored):
    """V1.1: compare validity only near the performance winner in its class."""
    # max() keeps the original input-order fallback on equal performance scores.
    performance_only_winner = max(
        scored, key=lambda candidate: candidate["acquisition_score"]
    )
    winner_class = _performance_semantic_class(performance_only_winner)
    class_pool = [
        candidate for candidate in scored
        if _performance_semantic_class(candidate) == winner_class
    ]
    pool_max = max(candidate["acquisition_score"] for candidate in scored)
    pool_min = min(candidate["acquisition_score"] for candidate in scored)
    pool_span = max(pool_max - pool_min, _EPSILON)
    reference_score = performance_only_winner["acquisition_score"]
    for candidate in scored:
        candidate["performance_semantic_class"] = _performance_semantic_class(candidate)
        candidate["normalized_performance_gap"] = (
            (reference_score - candidate["acquisition_score"]) / pool_span
        )
        candidate["matches_performance_winner_class"] = (
            candidate["performance_semantic_class"] == winner_class
        )
        # Compatibility alias: "best" now means the performance winner's class,
        # not the globally highest CF/MF/IF class.
        candidate["in_best_semantic_class"] = (
            candidate["matches_performance_winner_class"]
        )
        candidate["in_performance_equivalent_pool"] = (
            candidate["matches_performance_winner_class"]
            and candidate["normalized_performance_gap"] <= SEMANTIC_NEAR_TIE_TAU
        )
    equivalent_pool = [
        candidate for candidate in scored
        if candidate["in_performance_equivalent_pool"]
    ]
    # max() preserves the existing input-order fallback on complete ties.
    selected = max(
        equivalent_pool,
        key=lambda candidate: (
            -candidate["validity_risk"], candidate["acquisition_score"]
        ),
    )
    if len(class_pool) == 1:
        reason = "SINGLE_WINNER_CLASS_CANDIDATE"
    elif len(equivalent_pool) == 1:
        reason = "SINGLE_NEAR_EQUIVALENT_CANDIDATE"
    else:
        lowest_risk = min(candidate["validity_risk"] for candidate in equivalent_pool)
        lowest_risk_pool = [
            candidate for candidate in equivalent_pool
            if candidate["validity_risk"] == lowest_risk
        ]
        if len(lowest_risk_pool) == 1:
            reason = "VALIDITY_SELECTED_WITHIN_SAME_CLASS_NEAR_TIE"
        elif len({candidate["acquisition_score"] for candidate in lowest_risk_pool}) > 1:
            reason = "VALIDITY_TIE_FALLBACK_TO_PERFORMANCE"
        else:
            reason = "FULL_TIE_PRESERVE_ORDER"
    selection_changed = selected is not performance_only_winner
    validity_selection_applied = (
        selection_changed
        and selected["validity_risk"] < performance_only_winner["validity_risk"]
    )
    assert not selection_changed or validity_selection_applied
    winner_class_ids = [candidate["candidate_id"] for candidate in class_pool]
    selected.update({
        "performance_winner_semantic_class": winner_class,
        "winner_class_candidate_ids": winner_class_ids,
        # Compatibility aliases; both refer to the performance winner's class.
        "best_semantic_class": winner_class,
        "class_pool_candidate_ids": winner_class_ids,
        "pool_score_max": pool_max,
        "pool_score_min": pool_min,
        "pool_score_span": pool_span,
        "near_tie_tau": SEMANTIC_NEAR_TIE_TAU,
        "performance_equivalent_candidate_ids": [
            candidate["candidate_id"] for candidate in equivalent_pool
        ],
        "performance_only_winner_id": performance_only_winner["candidate_id"],
        "v1_1_selected_candidate_id": selected["candidate_id"],
        "selection_changed_from_performance_only": selection_changed,
        "validity_selection_applied": validity_selection_applied,
        "semantic_class_override_applied": False,
        "v1_1_selection_changed": selection_changed,
        "validity_tiebreak_applied": validity_selection_applied,
        "selection_reason": reason,
    })
    return selected


def select_next_candidate(
    predictions,
    performance_template,
    target_specs,
    *,
    beta=0.7,
    kappa=1.0,
    acquisition_mode=_CONSERVATIVE_MODE,
    return_scored=False,
    validity_by_candidate_id=None,
    validity_mode=_V1_VALIDITY_MODE,
    near_tie_tau=SEMANTIC_NEAR_TIE_TAU,
):
    """Select the candidate with the smallest worst conservative violation."""

    if not isinstance(kappa, (int, float)) or isinstance(kappa, bool):
        raise TypeError("kappa must be numeric")
    kappa = float(kappa)
    if not math.isfinite(kappa) or kappa < 0.0:
        raise ValueError("kappa must be finite and non-negative")
    if acquisition_mode not in _SUPPORTED_MODES:
        raise ValueError(f"unsupported acquisition mode: {acquisition_mode!r}")
    if validity_mode not in {_V1_VALIDITY_MODE, _V1_1_VALIDITY_MODE}:
        raise ValueError(f"unsupported validity mode: {validity_mode!r}")
    if validity_mode == _V1_1_VALIDITY_MODE:
        if near_tie_tau != SEMANTIC_NEAR_TIE_TAU:
            raise ValueError("semantic_near_tie requires frozen near_tie_tau=0.025")
        if validity_by_candidate_id is None:
            raise ValueError("semantic_near_tie requires candidate validity diagnostics")
    if acquisition_mode == _UNCERTAINTY_REWARD_MODE:
        if not isinstance(beta, (int, float)) or isinstance(beta, bool):
            raise TypeError("beta must be numeric")
        beta = float(beta)
        if not math.isfinite(beta) or not 0.0 <= beta <= 1.0:
            raise ValueError("beta must be finite and in [0, 1]")

    template = performance_template["performance_template"]
    metric_names = template["active_metrics"]
    scored = []

    for candidate in predictions:
        metric_scores = {}
        for name in metric_names:
            target_spec = target_specs[name]
            target = float(target_spec["value"])
            operator = target_spec["operator"]
            denominator = max(abs(target), _EPSILON)
            prediction = candidate["prediction"][name]
            mean = float(prediction["mean"])
            std = float(prediction["std"])

            if operator in {"<", "<="}:
                risk_value = mean + kappa * std
                mean_violation = max(0.0, (mean - target) / denominator)
                risk_violation = max(0.0, (risk_value - target) / denominator)
            elif operator in {">", ">="}:
                risk_value = mean - kappa * std
                mean_violation = max(0.0, (target - mean) / denominator)
                risk_violation = max(0.0, (target - risk_value) / denominator)
            else:
                raise ValueError(f"unsupported target operator: {operator!r}")
            metric_scores[name] = {
                "mean": mean,
                "std": std,
                "target": target,
                "operator": operator,
                "mean_violation": mean_violation,
                "risk_value": risk_value,
                "risk_violation": risk_violation,
                "normalized_violation": mean_violation,
                "normalized_std": std / denominator,
            }

        mean_violation_score = sum(
            score["normalized_violation"] for score in metric_scores.values()
        ) / len(metric_names)
        uncertainty_score = sum(
            score["normalized_std"] for score in metric_scores.values()
        ) / len(metric_names)
        risk_violation_score = max(
            score["risk_violation"] for score in metric_scores.values()
        )
        mean_only_violation_score = max(
            score["mean_violation"] for score in metric_scores.values()
        )
        if acquisition_mode == _CONSERVATIVE_MODE:
            ranking_violation_score = risk_violation_score
            std_used_for_ranking = True
            acquisition_score = -ranking_violation_score
        elif acquisition_mode == _MEAN_ONLY_MODE:
            ranking_violation_score = mean_only_violation_score
            std_used_for_ranking = False
            acquisition_score = -ranking_violation_score
        else:
            ranking_violation_score = mean_violation_score
            std_used_for_ranking = True
            acquisition_score = (
                -beta * mean_violation_score
                + (1.0 - beta) * uncertainty_score
            )
        scored_candidate = {
            "candidate_id": candidate["candidate_id"],
            "params": candidate["params"],
            "acquisition_mode": acquisition_mode,
            "kappa": kappa,
            "std_used_for_ranking": std_used_for_ranking,
            "mean_violation_score": mean_violation_score,
            "violation_score": mean_violation_score,
            "uncertainty_score": uncertainty_score,
            "risk_violation_score": risk_violation_score,
            "mean_only_violation_score": mean_only_violation_score,
            "acquisition_score": acquisition_score,
            "metric_scores": metric_scores,
        }
        if acquisition_mode == _UNCERTAINTY_REWARD_MODE:
            scored_candidate["beta"] = beta
        if validity_by_candidate_id is not None:
            scored_candidate.update(validity_by_candidate_id[candidate["candidate_id"]])
        scored.append(scored_candidate)

    performance_only_winner = max(
        scored, key=lambda candidate: candidate["acquisition_score"]
    )
    if validity_mode == _V1_1_VALIDITY_MODE:
        selected = _select_semantic_near_tie(scored)
    elif validity_by_candidate_id is None:
        selected = performance_only_winner
    else:
        # Exact primary-score ties only. max() retains input order on full ties.
        selected = max(
            scored,
            key=lambda candidate: (
                candidate["acquisition_score"], -candidate["validity_risk"]
            ),
        )
        tied = [
            candidate for candidate in scored
            if candidate["acquisition_score"] == performance_only_winner["acquisition_score"]
        ]
        selected["performance_only_winner_id"] = performance_only_winner["candidate_id"]
        selected["performance_tie_candidate_ids"] = [
            candidate["candidate_id"] for candidate in tied
        ]
        selected["performance_tie_validity_risks"] = {
            candidate["candidate_id"]: candidate["validity_risk"]
            for candidate in tied
        }
        selected["validity_tiebreak_applied"] = (
            selected["candidate_id"] != performance_only_winner["candidate_id"]
        )
    if return_scored:
        return selected, scored
    return selected
