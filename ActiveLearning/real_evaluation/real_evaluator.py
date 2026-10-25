"""Orchestrate real evaluation of one selected Active Learning candidate."""

from ActiveLearning.InitializeD0.evaluator import evaluate_current_mirror
from ActiveLearning.InitializeD0.validation import (
    check_current_mirror_functionality,
    check_meets_spec,
    validate_parameters,
)


__all__ = ["evaluate_selected_candidate"]


def _empty_record(candidate):
    return {
        "sample_id": candidate["candidate_id"],
        "source": "active_learning_query",
        "params": candidate["params"],
        "normalized_params": candidate.get("normalized_params"),
        "metrics": {
            "current_error": None,
            "output_resistance": None,
        },
        "functional_metrics": {
            "compliance_relative_variation": None,
            "tracking_max_error": None,
        },
        "functional_checks": {},
        "failure_reasons": [],
        "status": {
            "parameter_valid": True,
            "simulation_valid": None,
            "functional_valid": None,
            "functional_status": None,
            "functional_feasible": None,
            "meets_spec": None,
        },
    }


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
    record = _empty_record(candidate)
    parameter_template = getattr(runtime, "param_template", None)
    if parameter_template is not None:
        parameter_validation = validate_parameters(
            candidate["params"], parameter_template,
        )
        if parameter_validation["parameter_valid"] is not True:
            record["status"]["parameter_valid"] = False
            record["failure_reasons"] = list(
                parameter_validation.get("violations") or []
            )
            return record
    structured_record = _evaluate_structured_runtime(
        candidate,
        runtime,
        target_specs,
    )
    if structured_record is not None:
        return structured_record

    params = candidate["params"]

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
        functional_valid = functional["functional_feasible"] is True
        record["status"]["functional_valid"] = functional_valid
        record["status"]["functional_feasible"] = functional_valid
        record["status"]["functional_status"] = functional["functional_status"]
        record["failure_reasons"] = list(functional.get("failure_reasons") or [])
        record["functional_checks"] = dict(functional.get("basic_behavior") or {})
        record["functional_metrics"] = {
            "compliance_relative_variation": functional["compliance"][
                "relative_variation"
            ],
            "tracking_max_error": functional["reference_tracking"][
                "max_tracking_error"
            ],
        }

    return record
