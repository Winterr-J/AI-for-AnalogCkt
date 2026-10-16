"""Orchestrate real evaluation of one selected Active Learning candidate."""

from ActiveLearning.InitializeD0.evaluator import evaluate_current_mirror
from ActiveLearning.InitializeD0.validation import (
    check_current_mirror_functionality,
    check_meets_spec,
)


__all__ = ["evaluate_selected_candidate"]


def _evaluate_structured_runtime(candidate, runtime, target_specs):
    """Use a runtime's structured evaluator/validator contract when available."""

    if runtime is None:
        return None
    evaluator = runtime.create_evaluator()
    functional_validator = runtime.create_functional_validator()
    if not (
        hasattr(evaluator, "evaluate")
        and hasattr(functional_validator, "validate")
    ):
        return None

    params = candidate["params"]
    evaluation = evaluator.evaluate(params)
    validation = functional_validator.validate(
        params,
        evaluation,
        target_specs=target_specs,
    )
    status = dict(validation["status"])
    status["functional_feasible"] = status.get("functional_valid")
    return {
        "sample_id": candidate["candidate_id"],
        "source": "active_learning_query",
        "params": params,
        "normalized_params": candidate.get("normalized_params"),
        "metrics": dict(validation.get("metrics") or {}),
        "functional_metrics": dict(validation.get("functional_metrics") or {}),
        "functional_checks": dict(validation.get("functional_checks") or {}),
        "failure_reasons": list(validation.get("failure_reasons") or []),
        "diagnostics": dict(validation.get("diagnostics") or {}),
        "status": status,
    }


def evaluate_selected_candidate(
    candidate,
    adapter,
    target_specs,
    *,
    runtime=None,
    run_functional=False,
    functional_config=None,
):
    """Return one measured record ready for the caller to add to Dt."""
    structured_record = _evaluate_structured_runtime(
        candidate,
        runtime,
        target_specs,
    )
    if structured_record is not None:
        return structured_record

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
