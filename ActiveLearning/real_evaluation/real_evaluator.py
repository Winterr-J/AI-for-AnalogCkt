"""Orchestrate real evaluation of one selected Current Mirror candidate."""

from ActiveLearning.InitializeD0.evaluator import evaluate_current_mirror
from ActiveLearning.InitializeD0.validation import (
    check_current_mirror_functionality,
    check_meets_spec,
)


__all__ = ["evaluate_selected_candidate"]


def evaluate_selected_candidate(
    candidate,
    adapter,
    target_specs,
    *,
    run_functional=False,
    functional_config=None,
):
    """Return one measured record ready for the caller to add to Dt."""
    params = candidate["params"]
    record = {
        "sample_id": candidate["candidate_id"],
        "source": "active_learning_query",
        "params": params,
        "normalized_params": candidate.get("normalized_params"),
        "metrics": {
            "current_error": None,
            "output_resistance": None,
        },
        "functional_metrics": {
            "compliance_relative_variation": None,
            "tracking_max_error": None,
        },
        "status": {
            "parameter_valid": True,
            "simulation_valid": None,
            "functional_feasible": None,
            "meets_spec": None,
        },
    }

    current_error, output_resistance, simulation_valid = evaluate_current_mirror(
        params,
        adapter=adapter,
    )
    record["status"]["simulation_valid"] = simulation_valid
    if not simulation_valid:
        return record

    record["metrics"] = {
        "current_error": current_error,
        "output_resistance": output_resistance,
    }
    record["status"]["meets_spec"] = check_meets_spec(
        record["metrics"],
        target_specs,
    )["meets_spec"]

    if run_functional:
        functional = check_current_mirror_functionality(
            params,
            adapter=adapter,
            **(functional_config or {}),
        )
        record["status"]["functional_feasible"] = functional[
            "functional_feasible"
        ]
        record["functional_metrics"] = {
            "compliance_relative_variation": functional["compliance"][
                "relative_variation"
            ],
            "tracking_max_error": functional["reference_tracking"][
                "max_tracking_error"
            ],
        }

    return record
