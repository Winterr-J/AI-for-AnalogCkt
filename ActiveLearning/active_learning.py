"""Thin orchestration loop for Active Learning V1."""

import inspect
import json
import time
from collections.abc import Mapping
from pathlib import Path

from ActiveLearning.acquisition import select_next_candidate
from ActiveLearning.data_expansion import expand_candidates
from ActiveLearning.inverse_design import (
    call_inverse_design_llm,
    propose_inverse_design_seed,
)
from ActiveLearning.InitializeD0.validation.spec_validator import check_meets_spec
from ActiveLearning.performance_prediction import predict_candidate_performance
from ActiveLearning.real_evaluation import evaluate_selected_candidate


__all__ = ["run_active_learning"]

_STAGE_NAMES = (
    "inverse_design",
    "data_expansion",
    "performance_prediction",
    "acquisition",
    "real_spice",
)
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


def _save_jsonl(items, output_dir, filename):
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / filename
    with path.open("w", encoding="utf-8") as stream:
        for item in items:
            stream.write(json.dumps(item, ensure_ascii=False, default=float) + "\n")
    return path


def _sum_token_usage(entries):
    return {
        field: sum(entry[field] for entry in entries)
        for field in _TOKEN_FIELDS
    }


def _recompute_initial_meets_spec(records, target_specs):
    """Refresh initial-record feasibility for the current experiment targets."""
    first_feasible = None
    for record in records:
        status = record.get("status")
        if isinstance(status, Mapping):
            simulation_valid = status.get("simulation_valid")
        else:
            simulation_valid = record.get("simulation_valid")

        metrics = record.get("metrics")
        if simulation_valid is True and isinstance(metrics, Mapping):
            meets_spec = check_meets_spec(metrics, target_specs)["meets_spec"]
        else:
            meets_spec = None

        if isinstance(status, dict):
            status["meets_spec"] = meets_spec
        if "meets_spec" in record or not isinstance(status, Mapping):
            record["meets_spec"] = meets_spec

        if first_feasible is None and meets_spec is True:
            first_feasible = record
    return first_feasible


def _make_logged_llm_client(client, usage_log, iteration, stage):
    if client is not None and not callable(client):
        return client

    parameters = {}
    if client is not None:
        try:
            parameters = inspect.signature(client).parameters
        except (TypeError, ValueError):
            pass
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    round_number = 0

    def logged(prompt):
        nonlocal round_number
        round_number += 1
        context = {
            "iteration": iteration,
            "stage": stage,
            "round": round_number,
        }
        if client is None:
            return call_inverse_design_llm(
                prompt,
                usage_log=usage_log,
                usage_context=context,
            )

        kwargs = {}
        if accepts_kwargs or "usage_log" in parameters:
            kwargs["usage_log"] = usage_log
        if accepts_kwargs or "usage_context" in parameters:
            kwargs["usage_context"] = context
        return client(prompt, **kwargs)

    return logged


def run_active_learning(
    records,
    circuit_info,
    parameter_template,
    performance_template,
    target_specs,
    adapter,
    *,
    max_iterations=10,
    beta=0.7,
    context_k=5,
    retrieval_k=3,
    committee_size=5,
    data_expansion_seed=42,
    output_dir=None,
    llm_client=None,
):
    """Run the V1 loop and return records, logs, timing, and token usage."""
    run_start = time.perf_counter()
    iteration_log = []
    llm_usage_log = []
    final_record = None
    stop_reason = "max_iterations"
    stage_totals = {f"{stage}_seconds": 0.0 for stage in _STAGE_NAMES}

    initial_feasible_record = _recompute_initial_meets_spec(records, target_specs)
    if initial_feasible_record is not None:
        return {
            "records": records,
            "iteration_log": iteration_log,
            "iterations_completed": 0,
            "stop_reason": "initial_d0_meets_spec",
            "best_or_final_record": initial_feasible_record,
            "total_timing": {
                "total_seconds": time.perf_counter() - run_start,
                **stage_totals,
            },
            "total_tokens": _sum_token_usage(llm_usage_log),
            "llm_calls": 0,
        }

    for iteration_index in range(max_iterations):
        iteration = iteration_index + 1
        history_size_before_iteration = len(records)
        effective_expansion_seed = data_expansion_seed + iteration_index
        iteration_start = time.perf_counter()
        timing = {}

        inverse_client = _make_logged_llm_client(
            llm_client, llm_usage_log, iteration, "inverse_design"
        )
        start = time.perf_counter()
        inverse_result = propose_inverse_design_seed(
            records,
            circuit_info,
            parameter_template,
            target_specs,
            context_k=context_k,
            llm_client=inverse_client,
        )
        timing["inverse_design_seconds"] = time.perf_counter() - start
        if inverse_result["parameter_valid"] is not True:
            raise ValueError("Inverse-design seed failed parameter validation")
        x_seed = inverse_result["x_seed"]
        inverse_context = inverse_result["inverse_context"]
        best_known_anchor = inverse_result.get("best_known_anchor")
        if best_known_anchor is None:
            inverse_anchor = None
        else:
            inverse_anchor = {
                "sample_id": best_known_anchor["sample_id"],
                "params": best_known_anchor["params"],
                "metrics": best_known_anchor["measured_metrics"],
                "joint_violation": best_known_anchor["joint_violation"],
            }

        start = time.perf_counter()
        candidates, expansion_diagnostics = expand_candidates(
            x_seed,
            parameter_template,
            seed=effective_expansion_seed,
            historical_records=records,
            return_diagnostics=True,
        )
        timing["data_expansion_seconds"] = time.perf_counter() - start

        prediction_client = _make_logged_llm_client(
            llm_client, llm_usage_log, iteration, "performance_prediction"
        )
        start = time.perf_counter()
        predictions = predict_candidate_performance(
            candidates,
            records,
            circuit_info,
            performance_template,
            parameter_template=parameter_template,
            retrieval_k=retrieval_k,
            committee_size=committee_size,
            llm_client=prediction_client,
        )
        timing["performance_prediction_seconds"] = time.perf_counter() - start

        start = time.perf_counter()
        x_next, scored_candidates = select_next_candidate(
            predictions,
            performance_template,
            target_specs,
            beta=beta,
            return_scored=True,
        )
        timing["acquisition_seconds"] = time.perf_counter() - start

        predictions_by_id = {
            prediction["candidate_id"]: prediction for prediction in predictions
        }
        candidates_by_id = {
            candidate["candidate_id"]: candidate for candidate in candidates
        }
        candidate_diagnostics = [
            {
                **scored_candidate,
                "source": candidates_by_id[scored_candidate["candidate_id"]].get(
                    "sampling_source"
                ),
                "prediction": predictions_by_id[scored_candidate["candidate_id"]][
                    "prediction"
                ],
                "retrieval_context": predictions_by_id[
                    scored_candidate["candidate_id"]
                ].get("retrieval_context", []),
                "raw_predictions": predictions_by_id[
                    scored_candidate["candidate_id"]
                ].get("raw_predictions", []),
                "committee_attempt_counts": predictions_by_id[
                    scored_candidate["candidate_id"]
                ].get("committee_attempt_counts", []),
                "committee_unique_count": predictions_by_id[
                    scored_candidate["candidate_id"]
                ].get("committee_unique_count", {}),
            }
            for scored_candidate in scored_candidates
        ]
        selected_prediction = next(
            prediction
            for prediction in predictions
            if prediction["candidate_id"] == x_next["candidate_id"]
        )
        selected = next(
            candidate
            for candidate in candidates
            if candidate["candidate_id"] == x_next["candidate_id"]
        )
        evaluation_candidate = {
            **x_next,
            "candidate_id": f"iter_{iteration:03d}_{x_next['candidate_id']}",
            "normalized_params": selected["normalized_params"],
        }
        start = time.perf_counter()
        new_record = evaluate_selected_candidate(
            evaluation_candidate,
            adapter,
            target_specs,
            run_functional=False,
        )
        timing["real_spice_seconds"] = time.perf_counter() - start
        new_record["iteration"] = iteration
        records.append(new_record)
        final_record = new_record
        timing["iteration_total_seconds"] = time.perf_counter() - iteration_start

        iteration_usage = [
            entry for entry in llm_usage_log if entry["iteration"] == iteration
        ]
        inverse_tokens = _sum_token_usage(
            [entry for entry in iteration_usage if entry["stage"] == "inverse_design"]
        )
        prediction_tokens = _sum_token_usage(
            [
                entry
                for entry in iteration_usage
                if entry["stage"] == "performance_prediction"
            ]
        )
        tokens = {
            "inverse_design": inverse_tokens,
            "performance_prediction": prediction_tokens,
            "iteration_total": _sum_token_usage(iteration_usage),
        }
        iteration_log.append(
            {
                "iteration": iteration,
                "history_size_before_iteration": history_size_before_iteration,
                "data_expansion_seed": effective_expansion_seed,
                **expansion_diagnostics,
                "inverse_context": inverse_context,
                "inverse_anchor": inverse_anchor,
                "x_seed": x_seed,
                "x_seed_vs_anchor": inverse_result.get("x_seed_vs_anchor"),
                "x_seed_anchor_distance": inverse_result.get(
                    "x_seed_anchor_distance"
                ),
                "candidate_count": len(candidates),
                "candidate_diagnostics": candidate_diagnostics,
                "selected_candidate_id": x_next["candidate_id"],
                "selected_prediction": selected_prediction["prediction"],
                "violation_score": x_next["violation_score"],
                "uncertainty_score": x_next["uncertainty_score"],
                "acquisition_score": x_next["acquisition_score"],
                "metric_scores": x_next["metric_scores"],
                "simulation_valid": new_record["status"]["simulation_valid"],
                "meets_spec": new_record["status"]["meets_spec"],
                "metrics": new_record["metrics"],
                "timing": timing,
                "tokens": tokens,
            }
        )
        for key in stage_totals:
            stage_totals[key] += timing[key]

        if output_dir is not None:
            _save_jsonl(records, output_dir, "active_learning_records.jsonl")
            _save_jsonl(iteration_log, output_dir, "iteration_log.jsonl")
            _save_jsonl(llm_usage_log, output_dir, "llm_usage.jsonl")

        if new_record["status"]["meets_spec"] is True:
            stop_reason = "meets_spec"
            break

    total_timing = {
        "total_seconds": time.perf_counter() - run_start,
        **stage_totals,
    }
    return {
        "records": records,
        "iteration_log": iteration_log,
        "iterations_completed": len(iteration_log),
        "stop_reason": stop_reason,
        "best_or_final_record": final_record,
        "total_timing": total_timing,
        "total_tokens": _sum_token_usage(llm_usage_log),
        "llm_calls": len(llm_usage_log),
    }
