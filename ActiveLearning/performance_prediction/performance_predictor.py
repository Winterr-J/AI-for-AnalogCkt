"""Orchestrate local-context LLM performance prediction."""

import json
from numbers import Real
from pathlib import Path

import numpy as np

from ActiveLearning.common import parameter_to_similarity_vector, retrieve_top_k
from ActiveLearning.inverse_design.llm_client import call_inverse_design_llm


__all__ = ["predict_candidate_performance"]

_PROMPT_DIR = Path(__file__).parent / "prompts"
_GENERIC_PROMPT_PATH = _PROMPT_DIR / "generic_performance_prediction_v1.txt"
_CIRCUIT_TEMPLATE_FILES = {"currentmirror": "current_mirror_v1.txt"}
_MAX_ROUND_ATTEMPTS = 3


def _extract_performance_definitions(performance_template):
    body = performance_template["performance_template"]
    keys = ("name", "unit", "direction", "description", "calculation")
    return {
        name: {
            key: body["metrics"][name].get(key)
            for key in keys
            if body["metrics"][name].get(key) is not None
        }
        for name in body["active_metrics"]
    }


def _filter_usable_records(records, metric_names):
    usable = []
    for record in records:
        status = record["status"]
        metrics = record["metrics"]
        if not (status["parameter_valid"] and status["simulation_valid"]):
            continue
        if not all(
            name in metrics
            and isinstance(metrics[name], Real)
            and not isinstance(metrics[name], bool)
            and np.isfinite(metrics[name])
            for name in metric_names
        ):
            continue
        usable.append(record)
    return usable


def _build_neighbor_comparison(candidate_params, historical_params):
    """Describe one historical point relative to the new candidate."""
    comparison = {}
    for name, candidate_value in candidate_params.items():
        historical_value = historical_params[name]
        if (
            isinstance(candidate_value, Real)
            and not isinstance(candidate_value, bool)
            and isinstance(historical_value, Real)
            and not isinstance(historical_value, bool)
        ):
            candidate_numeric = float(candidate_value)
            historical_numeric = float(historical_value)
            values = {
                "candidate_value": candidate_numeric,
                "historical_value": historical_numeric,
                "absolute_delta": candidate_numeric - historical_numeric,
            }
            if (
                candidate_numeric != 0.0
                and historical_numeric != 0.0
                and np.signbit(candidate_numeric) == np.signbit(historical_numeric)
            ):
                if candidate_numeric < 0.0:
                    values["magnitude_ratio"] = (
                        abs(candidate_numeric) / abs(historical_numeric)
                    )
                else:
                    values["relative_ratio"] = (
                        candidate_numeric / historical_numeric
                    )
            comparison[name] = values
        else:
            comparison[name] = {
                "candidate_value": candidate_value,
                "historical_value": historical_value,
                "same": candidate_value == historical_value,
            }
    return comparison


def _build_prediction_cases(
    candidates,
    usable_records,
    metric_names,
    parameter_order,
    retrieval_k,
    *,
    parameter_template=None,
    return_diagnostics=False,
):
    if parameter_template is None:
        historical_vectors = [
            [record["normalized_params"][name] for name in parameter_order]
            for record in usable_records
        ]
        similarity_space = "linear_normalized"
    else:
        historical_vectors = [
            parameter_to_similarity_vector(
                record["params"],
                parameter_template,
                normalized_params=record.get("normalized_params"),
            )
            for record in usable_records
        ]
        similarity_space = "log_aware"
    cases = []
    diagnostics = {}
    for candidate in candidates:
        if parameter_template is None:
            query = [candidate["normalized_params"][name] for name in parameter_order]
        else:
            query = parameter_to_similarity_vector(
                candidate["params"],
                parameter_template,
                normalized_params=candidate.get("normalized_params"),
            )
        indices = retrieve_top_k(query, historical_vectors, k=retrieval_k)
        comparisons = {
            index: _build_neighbor_comparison(
                candidate["params"],
                usable_records[index]["params"],
            )
            for index in indices
        }
        diagnostics[candidate["candidate_id"]] = [
            {
                "rank": rank,
                "sample_id": usable_records[index].get(
                    "sample_id",
                    usable_records[index].get("candidate_id"),
                ),
                "params": usable_records[index]["params"],
                "metrics": {
                    name: usable_records[index]["metrics"][name]
                    for name in metric_names
                },
                "comparison_to_candidate": comparisons[index],
                "similarity_space": similarity_space,
                "distance": float(
                    np.linalg.norm(
                        np.asarray(historical_vectors[index], dtype=float)
                        - np.asarray(query, dtype=float)
                    )
                ),
            }
            for rank, index in enumerate(indices, start=1)
        ]
        cases.append(
            {
                "candidate_id": candidate["candidate_id"],
                "params": candidate["params"],
                "historical_context": [
                    {
                        "params": usable_records[index]["params"],
                        "metrics": {
                            name: usable_records[index]["metrics"][name]
                            for name in metric_names
                        },
                        "comparison_to_candidate": comparisons[index],
                    }
                    for index in indices
                ],
            }
        )
    if return_diagnostics:
        return cases, diagnostics
    return cases


def _build_prompt(circuit_info, performance_definitions, prediction_cases):
    prompt = _GENERIC_PROMPT_PATH.read_text(encoding="utf-8")
    sections = {
        "circuit_info": circuit_info,
        "performance_definitions": performance_definitions,
        "prediction_cases": prediction_cases,
    }
    for name, value in sections.items():
        prompt = prompt.replace(
            f"{{{name}}}",
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True),
        )

    circuit_type = circuit_info["circuit_type"]
    normalized = "".join(
        character for character in str(circuit_type).casefold() if character.isalnum()
    )
    filename = _CIRCUIT_TEMPLATE_FILES.get(normalized)
    if filename is None:
        raise ValueError(
            f"Unsupported circuit type for performance prediction: {circuit_type!r}"
        )
    circuit_prompt = (_PROMPT_DIR / "circuit_types" / filename).read_text(encoding="utf-8")
    return prompt.rstrip() + "\n\n" + circuit_prompt.strip() + "\n"


def _strip_markdown_fence(raw_response):
    stripped = raw_response.strip()
    lines = stripped.splitlines()
    if (len(lines) >= 3 and lines[0].strip().casefold() in {"```", "```json"}
            and lines[-1].strip() == "```"):
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _parse_predictions(raw_response, candidate_ids, metric_names):
    try:
        predictions = json.loads(_strip_markdown_fence(raw_response))["predictions"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("invalid LLM prediction response") from error
    if len(predictions) != len(candidate_ids):
        raise ValueError("prediction count mismatch")

    expected = set(candidate_ids)
    parsed = {}
    for item in predictions:
        candidate_id = item["candidate_id"]
        if candidate_id not in expected:
            raise ValueError(f"unknown candidate_id: {candidate_id}")
        if candidate_id in parsed:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")

        metrics = item["predicted_metrics"]
        if not all(
            name in metrics
            and isinstance(metrics[name], (int, float))
            and not isinstance(metrics[name], bool)
            and np.isfinite(metrics[name])
            for name in metric_names
        ):
            raise ValueError(f"invalid metrics for {candidate_id}")
        parsed[candidate_id] = {name: float(metrics[name]) for name in metric_names}

    if set(parsed) != expected:
        raise ValueError("missing candidate predictions")
    return parsed


def _aggregate_predictions(
    candidates,
    metric_names,
    rounds,
    retrieval_diagnostics,
    committee_attempt_counts,
):
    results = []
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        prediction = {}
        for metric_name in metric_names:
            values = [round_[candidate_id][metric_name] for round_ in rounds]
            prediction[metric_name] = {
                "mean": float(np.mean(values)),
                "std": 0.0 if len(values) == 1 else float(np.std(values, ddof=1)),
            }
        raw_predictions = [
            {
                "round": round_index,
                "metrics": dict(round_[candidate_id]),
            }
            for round_index, round_ in enumerate(rounds, start=1)
        ]
        results.append(
            {
                "candidate_id": candidate_id,
                "params": candidate["params"],
                "prediction": prediction,
                "retrieval_context": retrieval_diagnostics[candidate_id],
                "raw_predictions": raw_predictions,
                "committee_attempt_counts": list(committee_attempt_counts),
                "committee_unique_count": {
                    metric_name: len(
                        {round_[candidate_id][metric_name] for round_ in rounds}
                    )
                    for metric_name in metric_names
                },
            }
        )
    return results


def _collect_committee_rounds(
    client,
    prompt,
    candidate_ids,
    metric_names,
    committee_size,
):
    rounds = []
    attempt_counts = []
    for round_index in range(1, committee_size + 1):
        last_error = None
        for attempt in range(1, _MAX_ROUND_ATTEMPTS + 1):
            raw_response = client(prompt)
            try:
                parsed = _parse_predictions(
                    raw_response,
                    candidate_ids,
                    metric_names,
                )
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                last_error = error
                continue
            rounds.append(parsed)
            attempt_counts.append(attempt)
            break
        else:
            raise ValueError(
                "Performance prediction committee round "
                f"{round_index} failed after {_MAX_ROUND_ATTEMPTS} attempts "
                f"(expected {len(candidate_ids)} predictions); "
                f"last validation error: {type(last_error).__name__}: {last_error}"
            ) from last_error
    return rounds, attempt_counts


def predict_candidate_performance(
    candidates,
    records,
    circuit_info,
    performance_template,
    *,
    parameter_template=None,
    retrieval_k=3,
    committee_size=5,
    llm_client=None,
):
    """Predict candidate metrics using retrieved context and an LLM committee."""
    if not candidates:
        return []

    parameter_order = sorted(candidates[0]["normalized_params"])
    definitions = _extract_performance_definitions(performance_template)
    metric_names = list(definitions)
    usable_records = _filter_usable_records(records, metric_names)
    if not usable_records:
        raise ValueError("No usable historical samples for performance prediction")

    cases, retrieval_diagnostics = _build_prediction_cases(
        candidates,
        usable_records,
        metric_names,
        parameter_order,
        retrieval_k,
        parameter_template=parameter_template,
        return_diagnostics=True,
    )
    prompt = _build_prompt(circuit_info, definitions, cases)
    client = call_inverse_design_llm if llm_client is None else llm_client
    candidate_ids = [candidate["candidate_id"] for candidate in candidates]
    rounds, attempt_counts = _collect_committee_rounds(
        client,
        prompt,
        candidate_ids,
        metric_names,
        committee_size,
    )
    return _aggregate_predictions(
        candidates,
        metric_names,
        rounds,
        retrieval_diagnostics,
        attempt_counts,
    )
