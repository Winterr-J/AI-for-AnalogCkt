"""Optional, bounded experiment preparation before D0 and Active Learning.

This module does not change the AL optimisation loop. Real SPICE and LLM use
require separate opt-in flags in the CLI; tests can inject an offline point
evaluator and LLM callable.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import math
import os
import platform
from pathlib import Path
from statistics import median
import sys
from tempfile import mkdtemp
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import yaml

from ActiveLearning.InitializeD0.validation.spec_validator import check_meets_spec
from ActiveLearning.InitializeD0.validation.parameter_validator import validate_parameters
from ActiveLearning.focused_profiling import _physical_to_unit
from ActiveLearning.inverse_design.inverse_designer import _strip_markdown_fence
from ActiveLearning.inverse_design.llm_client import (
    LLM_MODEL,
    LLMQuotaExceededError,
    LLMTransientError,
    _GEMINI_MODEL,
    call_inverse_design_llm,
)
from ActiveLearning.onboarding import build_active_learning_circuit_config
from ActiveLearning.profiling import (
    _evaluate_opamp_point,
    _evaluate_point,
    _functional_config,
    _parameter_space,
    _unit_to_physical,
    generate_parameter_samples,
)
from ActiveLearning.profiling_local_sampler import sample_local_norm_bounded


_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _ROOT.parent
_DEFAULT_OUTPUT_ROOT = _ROOT / "results" / "auto_target"
_SUCCESS = "READY_FOR_D0"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, default=str)
        + "\n",
        encoding="utf-8",
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def metric_contract(performance_template: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Use the active metric definitions, directions, and units already frozen."""

    body = performance_template["performance_template"]
    definitions = body["metrics"]
    result = {}
    for name in body["active_metrics"]:
        definition = definitions[name]
        direction = definition["direction"]
        if direction not in {"minimize", "maximize"}:
            raise ValueError(f"Unsupported metric direction for {name}: {direction}")
        result[name] = {
            "direction": direction,
            "operator": "<=" if direction == "minimize" else ">=",
            "unit": definition["unit"],
            "description": definition.get("description", ""),
            "validity_requirements": list(definition.get("validity_requirements") or []),
        }
    if not result:
        raise ValueError("Performance template has no active metrics")
    return result


def _valid_record(record: Mapping[str, Any], metric_names: Sequence[str]) -> bool:
    return (
        record.get("parameter_valid") is True
        and record.get("simulation_valid") is True
        and record.get("functional_valid") is True
        and all(_finite_number(record.get(name)) is not None for name in metric_names)
    )


def _local_samples(
    runtime: Any, anchor_params: Mapping[str, float], count: int, radius: float, seed: int,
    prefix: str,
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    space = _parameter_space(runtime)
    center = np.asarray(
        [_physical_to_unit(anchor_params[item["name"]], item) for item in space],
        dtype=float,
    )
    vectors, _ = sample_local_norm_bounded(
        center, radius, count, np.random.default_rng(seed)
    )
    return [
        {
            "sample_id": f"{prefix}_{index:04d}",
            "source": "local",
            "params": {
                item["name"]: _unit_to_physical(float(value), item)
                for item, value in zip(space, vector)
            },
            "normalized_params": {
                item["name"]: float(value) for item, value in zip(space, vector)
            },
        }
        for index, vector in enumerate(vectors)
    ]


def _global_samples(runtime: Any, count: int, seed: int, prefix: str) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    samples = generate_parameter_samples(
        runtime, samples=count, sampler="sobol", seed=seed
    )
    return [
        {**sample, "sample_id": f"{prefix}_{index:04d}", "source": "global"}
        for index, sample in enumerate(samples)
    ]


def _real_point_evaluator(runtime: Any) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Reuse profiling's per-point evaluator without its extra nominal run."""

    if runtime.circuit_type == "OpAmp":
        evaluator = runtime.create_evaluator()
        validator = runtime.create_functional_validator()
        return lambda sample: _evaluate_opamp_point(sample, runtime, evaluator, validator)
    if runtime.circuit_type == "CurrentMirror":
        from ActiveLearning.InitializeD0.evaluator import evaluate_current_mirror
        from ActiveLearning.InitializeD0.validation import check_current_mirror_functionality

        functional_config = _functional_config(runtime.adapter)
        return lambda sample: _evaluate_point(
            sample,
            runtime,
            evaluate_current_mirror,
            check_current_mirror_functionality,
            functional_config,
        )
    raise ValueError(f"Unsupported circuit type: {runtime.circuit_type}")


def _evaluate(
    sample: Mapping[str, Any], point_evaluator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    started = perf_counter()
    try:
        record = dict(point_evaluator(sample))
    except Exception as error:
        record = {
            "parameter_valid": True,
            "simulation_valid": False,
            "functional_valid": False,
            "failure_reason": f"{type(error).__name__}: {error}",
            "failure_reasons": ["simulation_exception"],
        }
    record.setdefault("sample_id", sample["sample_id"])
    record.setdefault("params", dict(sample["params"]))
    record.setdefault("source", sample.get("source", "unknown"))
    record["preparation_evaluation_wall_time_seconds"] = perf_counter() - started
    return record


def _margin(record: Mapping[str, Any]) -> float:
    rail_values = [
        _finite_number(record.get(name))
        for name in ("rail_margin_low", "rail_margin_high")
    ]
    if all(value is not None for value in rail_values):
        return min(rail_values)
    variation = _finite_number(record.get("compliance_relative_variation"))
    return -variation if variation is not None else 0.0


def _evaluator_unstable(records: Sequence[Mapping[str, Any]]) -> bool:
    return len(records) >= 3 and all(
        "simulation_exception" in (item.get("failure_reasons") or [])
        for item in records[-3:]
    )


def discover_anchor(
    runtime: Any,
    point_evaluator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    metric_names: Sequence[str],
    *,
    max_evaluations: int = 12,
    local_evaluations: int = 6,
    local_radius: float = 0.02,
    seed: int = 42,
) -> dict[str, Any]:
    """Nominal, bounded local, then bounded Sobol; only functional points qualify."""

    if max_evaluations < 1 or local_evaluations < 0:
        raise ValueError("Anchor budgets must be non-negative with at least one evaluation")
    if not 0.0 < local_radius <= 1.0:
        raise ValueError("local_radius must be in (0, 1]")
    started = perf_counter()
    nominal_params = {
        item.name: float(item.nominal) for item in runtime.runtime_parameters
    }
    nominal = _evaluate(
        {"sample_id": "anchor_nominal", "source": "nominal", "params": nominal_params},
        point_evaluator,
    )
    records = [nominal]
    chosen = nominal if _valid_record(nominal, metric_names) else None
    if chosen is None:
        local_count = min(local_evaluations, max_evaluations - 1)
        for sample in _local_samples(
            runtime, nominal_params, local_count, local_radius, seed, "anchor_local"
        ):
            records.append(_evaluate(sample, point_evaluator))
            if _evaluator_unstable(records):
                break
        valid_local = [item for item in records[1:] if _valid_record(item, metric_names)]
        if valid_local:
            chosen = max(valid_local, key=_margin)
    if chosen is None and not _evaluator_unstable(records):
        global_count = max_evaluations - len(records)
        for sample in _global_samples(runtime, global_count, seed + 1, "anchor_global"):
            record = _evaluate(sample, point_evaluator)
            records.append(record)
            if _valid_record(record, metric_names):
                chosen = record
                break
            if _evaluator_unstable(records):
                break
    if chosen is not None:
        status = "ANCHOR_FOUND"
    elif _evaluator_unstable(records):
        status = "ANCHOR_SIMULATION_FAILED"
    elif all(item.get("parameter_valid") is not True for item in records):
        status = "NO_VALID_ANCHOR"
    elif all(not item.get("simulation_valid") for item in records):
        status = "ANCHOR_SIMULATION_FAILED"
    elif all(item.get("simulation_valid") is True for item in records):
        status = "NO_VALID_ANCHOR"
    elif len(records) >= max_evaluations:
        status = "ANCHOR_BUDGET_EXHAUSTED"
    else:
        status = "NO_VALID_ANCHOR"
    return {
        "status": status,
        "anchor": chosen,
        "records": records,
        "evaluated_candidates": len(records),
        "spice_point_evaluations": sum(
            item.get("parameter_valid") is True for item in records
        ),
        "sampling": {"method": "nominal_local_sobol", "seed": seed,
                     "local_radius": local_radius, "max_evaluations": max_evaluations},
        "elapsed_seconds": perf_counter() - started,
    }


def _failure_distribution(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        if record.get("parameter_valid") is not True:
            counts["parameter_invalid"] += 1
        elif record.get("simulation_valid") is not True:
            counts["simulation_invalid"] += 1
        elif record.get("functional_valid") is not True:
            counts["functional_invalid"] += 1
        for reason in record.get("failure_reasons") or []:
            counts[str(reason)] += 1
    return dict(sorted(counts.items()))


def summarize_profile(
    runtime: Any, anchor_result: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]], metric_names: Sequence[str],
    elapsed_seconds: float,
) -> dict[str, Any]:
    valid = [item for item in records if _valid_record(item, metric_names)]
    return {
        "circuit_type": runtime.circuit_type,
        "circuit_id": runtime.circuit_id,
        "anchor": anchor_result["anchor"],
        "anchor_discovery": {
            "status": anchor_result["status"],
            "evaluated_candidates": anchor_result["evaluated_candidates"],
            "sampling": anchor_result["sampling"],
            "elapsed_seconds": anchor_result["elapsed_seconds"],
            "failure_distribution": _failure_distribution(anchor_result["records"]),
        },
        "total_samples": len(records),
        "parameter_valid_samples": sum(item.get("parameter_valid") is True for item in records),
        "simulation_valid_samples": sum(item.get("simulation_valid") is True for item in records),
        "functional_valid_samples": len(valid),
        "performance": {
            name: {
                "min": min(float(item[name]) for item in valid),
                "median": median(float(item[name]) for item in valid),
                "max": max(float(item[name]) for item in valid),
            }
            for name in metric_names
        } if valid else {},
        "valid_candidate_metrics": [
            {"sample_id": item["sample_id"],
             "metrics": {name: float(item[name]) for name in metric_names}}
            for item in valid
        ],
        "failure_distribution": _failure_distribution(records),
        "elapsed_seconds": elapsed_seconds,
        "spice_point_evaluations": sum(
            item.get("parameter_valid") is True for item in records
        ),
        "anchor_spice_point_evaluations": anchor_result["spice_point_evaluations"],
        "records": list(records),
    }


def run_bounded_profile(
    runtime: Any, anchor_result: Mapping[str, Any],
    point_evaluator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    metric_names: Sequence[str],
    *, samples: int = 25, max_evaluations: int = 40,
    additional_samples: int = 10, min_valid_samples: int = 2,
    local_radius: float = 0.02, seed: int = 42,
) -> dict[str, Any]:
    if samples < 1 or max_evaluations < samples or additional_samples < 0:
        raise ValueError("Invalid profiling budgets")
    if min_valid_samples < 1 or not 0.0 < local_radius <= 1.0:
        raise ValueError("Invalid profiling validity/radius setting")
    anchor = anchor_result["anchor"]
    if anchor is None:
        raise ValueError("Profiling requires a functionally valid anchor")
    started = perf_counter()
    records: list[dict[str, Any]] = []
    unstable = False
    batches = [samples]
    if additional_samples:
        batches.append(min(additional_samples, max_evaluations - samples))
    for batch_index, count in enumerate(batches):
        if count <= 0:
            continue
        if batch_index and sum(_valid_record(item, metric_names) for item in records) >= min_valid_samples:
            break
        local_count = count // 2
        global_count = count - local_count
        batch = _local_samples(
            runtime, anchor["params"], local_count, local_radius,
            seed + batch_index * 2, f"profile_local_{batch_index}"
        ) + _global_samples(
            runtime, global_count, seed + batch_index * 2 + 1,
            f"profile_global_{batch_index}"
        )
        for sample in batch[:max_evaluations - len(records)]:
            records.append(_evaluate(sample, point_evaluator))
            if _evaluator_unstable(records):
                unstable = True
                break
        if unstable:
            break
    summary = summarize_profile(runtime, anchor_result, records, metric_names,
                                perf_counter() - started)
    summary["evaluator_unstable"] = unstable
    summary["sampling"] = {
        "method": "anchor_local_plus_sobol", "seed": seed,
        "local_radius": local_radius, "initial_samples": samples,
        "additional_samples": additional_samples,
        "max_evaluations": max_evaluations,
    }
    return summary


def _prompt_rows(rows: Sequence[Mapping[str, Any]], contract: Mapping[str, Any],
                 limit: int = 60) -> list[dict[str, Any]]:
    """Bound context while retaining joint rows and every metric extreme."""

    if len(rows) <= limit:
        return list(rows)
    selected: set[int] = set()
    for name in contract:
        ordered = sorted(range(len(rows)), key=lambda index: rows[index]["metrics"][name])
        selected.update((ordered[0], ordered[-1]))
    remaining = [index for index in range(len(rows)) if index not in selected]
    slots = max(0, limit - len(selected))
    if slots:
        selected.update(remaining[round(position)] for position in np.linspace(
            0, len(remaining) - 1, min(slots, len(remaining))
        ))
    return [rows[index] for index in sorted(selected)[:limit]]


def build_target_prompt(
    runtime: Any, performance_template: Mapping[str, Any],
    profile: Mapping[str, Any], *, correction_reasons: Sequence[str] = (),
) -> str:
    contract = metric_contract(performance_template)
    anchor = profile["anchor"]
    payload = {
        "task": "Recommend an observed-feasible, non-trivial joint experimental AL target.",
        "circuit": {"type": runtime.circuit_type, "id": runtime.circuit_id,
                    "functionality": performance_template["performance_template"]["circuit_type"]},
        "metric_contract": contract,
        "operating_conditions": runtime.operating_conditions,
        "anchor": {"sample_id": anchor["sample_id"],
                   "metrics": {name: anchor[name] for name in contract}},
        "profiling": {
            "counts": {key: profile[key] for key in (
                "total_samples", "simulation_valid_samples", "functional_valid_samples"
            )},
            "performance": profile["performance"],
            "joint_candidate_rows": _prompt_rows(profile["valid_candidate_metrics"], contract),
        },
        "rules": [
            "Use only active metrics, exact operator directions and matching units.",
            "At least one single observed row must meet every threshold jointly.",
            "The anchor must fail at least one threshold.",
            "Do not combine independent per-metric best values into a fictional row.",
            "Return one JSON object with target_specs, reason, supporting_sample_ids, confidence.",
            "Each target_specs item must have operator, finite numeric value, and unit.",
        ],
        "previous_validation_errors": list(correction_reasons),
    }
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)


def parse_target_response(raw_response: str) -> dict[str, Any]:
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise ValueError("LLM returned empty target text")
    payload = json.loads(_strip_markdown_fence(raw_response))
    if not isinstance(payload, dict):
        raise ValueError("LLM target response must be a JSON object")
    return payload


def validate_target_recommendation(
    payload: Mapping[str, Any], performance_template: Mapping[str, Any],
    anchor: Mapping[str, Any], valid_candidate_metrics: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    contract = metric_contract(performance_template)
    errors: list[str] = []
    raw = payload.get("target_specs")
    if not isinstance(raw, Mapping) or set(raw) != set(contract):
        errors.append("target_specs must contain exactly the active performance metrics")
        raw = {}
    normalized = {}
    for name, definition in contract.items():
        spec = raw.get(name)
        if not isinstance(spec, Mapping):
            errors.append(f"{name}: target entry must be an object")
            continue
        if spec.get("operator") != definition["operator"]:
            errors.append(f"{name}: operator must be {definition['operator']}")
        if spec.get("unit") != definition["unit"]:
            errors.append(f"{name}: unit must be {definition['unit']}")
        value = _finite_number(spec.get("value"))
        if value is None:
            errors.append(f"{name}: value must be finite numeric")
        else:
            normalized[name] = {"operator": definition["operator"], "value": value}
    supporters: list[str] = []
    if not errors:
        for row in valid_candidate_metrics:
            if check_meets_spec(row["metrics"], normalized)["meets_spec"] is True:
                supporters.append(str(row["sample_id"]))
        if not supporters:
            errors.append("No one functionally valid profiling sample meets the full target jointly")
        anchor_metrics = {name: anchor.get(name) for name in contract}
        if check_meets_spec(anchor_metrics, normalized)["meets_spec"] is True:
            errors.append("Anchor already meets the complete target")
    declared = payload.get("supporting_sample_ids")
    if not isinstance(declared, list) or not declared or any(
        not isinstance(item, str) for item in declared
    ):
        errors.append("supporting_sample_ids must be a non-empty list of sample IDs")
    elif not errors and not set(declared).issubset(supporters):
        errors.append("Declared supporting_sample_ids are not joint supporters")
    if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
        errors.append("reason must be non-empty text")
    return {"valid": not errors, "errors": errors, "target_specs": normalized,
            "joint_supporting_sample_ids": supporters}


def recommend_target(
    runtime: Any, performance_template: Mapping[str, Any], profile: Mapping[str, Any],
    *, llm_client: Callable[..., str], provider: str | None = None,
    model: str | None = None, max_llm_calls: int = 4,
) -> dict[str, Any]:
    """One proposal and at most one correction; transient calls retry once."""

    started = perf_counter()
    usage: list[dict[str, Any]] = []
    attempts = 0
    if max_llm_calls < 1:
        raise ValueError("max_llm_calls must be positive")
    selected_provider = (provider or os.getenv("AL_LLM_PROVIDER", "openai")).strip().lower()
    selected_model = model or (
        os.getenv("GEMINI_MODEL", _GEMINI_MODEL)
        if selected_provider == "gemini"
        else os.getenv("OPENAI_MODEL", LLM_MODEL)
    )
    correction_reasons: list[str] = []
    last_error = "No target was produced"
    failure_status = "TARGET_GENERATION_FAILED"
    last_validation: dict[str, Any] | None = None
    for round_index in range(2):
        if attempts >= max_llm_calls:
            break
        prompt = build_target_prompt(
            runtime, performance_template, profile,
            correction_reasons=correction_reasons,
        )
        raw = None
        for transport_attempt in range(2):
            if attempts >= max_llm_calls:
                break
            attempts += 1
            try:
                raw = llm_client(
                    prompt, provider=selected_provider, model=selected_model, usage_log=usage,
                    usage_context={"stage": "auto_target", "round": round_index + 1},
                    max_attempts=1,
                )
                break
            except (LLMTransientError, TimeoutError) as error:
                last_error = f"{type(error).__name__}: transient provider failure"
                if transport_attempt == 1:
                    break
            except LLMQuotaExceededError:
                last_error = "LLMQuotaExceededError: provider quota exhausted"
                break
            except Exception as error:
                last_error = f"{type(error).__name__}: provider request failed"
                break
        if raw is None:
            break
        try:
            parsed = parse_target_response(raw)
            validation = validate_target_recommendation(
                parsed, performance_template, profile["anchor"],
                profile["valid_candidate_metrics"],
            )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            validation = {"valid": False, "errors": [f"{type(error).__name__}: {error}"],
                          "target_specs": {}, "joint_supporting_sample_ids": []}
            parsed = {}
            failure_status = "TARGET_GENERATION_FAILED"
        else:
            if not validation["valid"]:
                failure_status = "TARGET_VALIDATION_FAILED"
        if validation["valid"]:
            return {
                "status": "TARGET_VALIDATED", "proposal": parsed,
                "validation": validation, "attempts": attempts,
                "correction_attempts": round_index, "usage": usage,
                "elapsed_seconds": perf_counter() - started,
                "provider": usage[-1].get("provider", selected_provider) if usage else selected_provider,
                "model": usage[-1].get("model", selected_model) if usage else selected_model,
            }
        last_validation = validation
        correction_reasons = list(validation["errors"])
        last_error = "; ".join(correction_reasons)
    return {
        "status": failure_status, "reason": last_error,
        "validation": last_validation,
        "attempts": attempts, "correction_attempts": min(1, int(bool(correction_reasons))),
        "usage": usage, "elapsed_seconds": perf_counter() - started,
        "provider": usage[-1].get("provider", selected_provider) if usage else selected_provider,
        "model": usage[-1].get("model", selected_model) if usage else selected_model,
    }


def generate_experiment_config(
    runtime: Any, profile: Mapping[str, Any], target_result: Mapping[str, Any],
    *, output_path: Path, base_config_path: Path | None, seed: int,
    profile_path: Path, d0_mode: str = "independent",
) -> dict[str, Any]:
    """Copy experiment settings only; AutoConfig remains the structural source."""

    if output_path.exists():
        raise FileExistsError(f"Generated configuration already exists: {output_path}")
    if d0_mode not in {"independent", "reuse"}:
        raise ValueError("d0_mode must be independent or reuse")
    if base_config_path is not None and base_config_path.is_file():
        original = yaml.safe_load(base_config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(original, dict):
            raise ValueError("Existing experiment config must be a YAML mapping")
        config = deepcopy(original)
    else:
        if d0_mode == "reuse":
            raise ValueError("reuse D0 mode requires an existing experiment config")
        config = {
            "active_learning": {"max_iterations": 5},
            "data": {
                "d0_path": f"d0/d0_v3_seed{seed}.jsonl",
                "initialize_if_missing": True,
                "sampling_mode": "v3", "seed": seed,
                "n_samples": 20, "n_local": 10, "n_global": 9,
            },
        }
    data = config.setdefault("data", {})
    if d0_mode == "independent":
        anchor = profile["anchor"]
        if not validate_parameters(anchor["params"], runtime.param_template)["parameter_valid"]:
            raise ValueError("profiling anchor is outside the AutoConfig parameter space")
        sampling_mode = str(data.get("sampling_mode", "v3"))
        data.update({
            "d0_mode": "independent",
            "d0_path": f"d0/d0_{sampling_mode}_seed{seed}.jsonl",
            "d0_path_relative_to": "config",
            "initialize_if_missing": True,
            "sampling_mode": sampling_mode,
            "seed": seed,
            "initial_anchor": {
                "source": "profiling",
                "sample_id": str(anchor["sample_id"]),
                "params": dict(anchor["params"]),
            },
        })
    else:
        if not data.get("d0_path"):
            raise ValueError("reuse D0 mode requires an existing data.d0_path")
        if data.get("d0_path_relative_to") == "config":
            original_d0 = base_config_path.parent / data["d0_path"]
            data["d0_path"] = Path(os.path.relpath(
                original_d0.resolve(), output_path.parent.resolve()
            )).as_posix()
        data.update({"d0_mode": "reuse", "initialize_if_missing": False})
        data.pop("initial_anchor", None)
    validation = target_result["validation"]
    config["target_specs"] = deepcopy(validation["target_specs"])
    config["target_source"] = "auto_generated"
    config["performance_target_selected"] = True
    config["benchmark_version"] = "auto_target_v1_" + output_path.parent.name
    config["target_provenance"] = {
        "source": "automatic_profiling_llm_v1",
        "purpose": "experimental_benchmark_not_user_specification",
        "profiling_backend": profile.get("evaluation_backend", "unknown"),
        "circuit_type": runtime.circuit_type,
        "circuit_id": runtime.circuit_id,
        "anchor": {"sample_id": profile["anchor"]["sample_id"],
                   "params": profile["anchor"]["params"],
                   "metrics": {name: profile["anchor"][name]
                               for name in validation["target_specs"]}},
        "profiling_artifact": Path(os.path.relpath(
            profile_path.resolve(), output_path.parent.resolve()
        )).as_posix(),
        "target_recommendation": "target_generation.json",
        "generated_config": output_path.name,
        "d0_mode": d0_mode,
        "d0_anchor_source": "profiling" if d0_mode == "independent" else "historical_d0",
        "d0_anchor_sample_id": (
            str(profile["anchor"]["sample_id"]) if d0_mode == "independent" else None
        ),
        "d0_seed": data.get("seed"),
        "joint_supporting_sample_ids": validation["joint_supporting_sample_ids"],
        "llm_declared_supporting_sample_ids": target_result["proposal"]["supporting_sample_ids"],
        "reason": target_result["proposal"]["reason"],
        "confidence": target_result["proposal"].get("confidence"),
        "provider": target_result.get("provider"),
        "model": target_result.get("model"),
        "llm_backend": target_result.get("llm_backend"),
        "target_generation_attempts": target_result["attempts"],
        "seed": seed,
        "simulator_environment": {"platform": platform.platform(),
                                  "python": sys.version.split()[0]},
    }
    output_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config


def _artifact_profile(path: Path, runtime: Any, metric_names: Sequence[str]) -> dict[str, Any]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("circuit_type") != runtime.circuit_type or profile.get("circuit_id") != runtime.circuit_id:
        raise ValueError("Profiling artifact circuit identity does not match AutoConfig")
    anchor = profile.get("anchor")
    rows = profile.get("valid_candidate_metrics")
    if not isinstance(anchor, dict) or not _valid_record(anchor, metric_names):
        raise ValueError("Profiling artifact has no functional-valid anchor")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or set(row.get("metrics", {})) != set(metric_names)
        or any(_finite_number(row["metrics"][name]) is None for name in metric_names)
        for row in rows
    ):
        raise ValueError("Profiling artifact has invalid joint metric rows")
    records = profile.get("records")
    if not isinstance(records, list):
        raise ValueError("Profiling artifact must retain per-candidate validity records")
    certified = {
        str(item["sample_id"]): item
        for item in records
        if isinstance(item, dict) and "sample_id" in item
        and _valid_record(item, metric_names)
    }
    if len(rows) != len(certified) or len({str(row.get("sample_id")) for row in rows}) != len(rows) or any(
        str(row.get("sample_id")) not in certified
        or any(float(row["metrics"][name]) != float(certified[str(row["sample_id"])][name])
               for name in metric_names)
        for row in rows
    ):
        raise ValueError("Joint rows do not match functionally valid profiling records")
    if profile.get("functional_valid_samples") != len(certified):
        raise ValueError("Profiling artifact functional-valid count is inconsistent")
    if profile.get("total_samples") != len(records):
        raise ValueError("Profiling artifact total sample count is inconsistent")
    return profile


def prepare_experiment(
    circuit_type: str, circuit_id: str, *,
    runtime: Any | None = None,
    point_evaluator: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    llm_client: Callable[..., str] | None = None,
    allow_spice: bool = False, allow_llm: bool = False,
    profile_artifact: str | Path | None = None,
    base_config_path: str | Path | None = None,
    output_root: str | Path = _DEFAULT_OUTPUT_ROOT,
    profile_samples: int = 25, max_profile_evaluations: int = 40,
    additional_samples: int = 10, min_valid_samples: int = 2,
    max_anchor_evaluations: int = 12, anchor_local_evaluations: int = 6,
    local_radius: float = 0.02, seed: int = 42,
    d0_mode: str = "independent",
    provider: str | None = None, model: str | None = None,
    max_llm_calls: int = 4,
) -> dict[str, Any]:
    """Stable batch-callable V1 API; failures are structured and isolated."""

    started = perf_counter()
    safe_name = "_".join((circuit_type, circuit_id))
    if not all(char.isalnum() or char in "_-" for char in safe_name):
        raise ValueError("circuit type and id must contain only letters, digits, - or _")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(mkdtemp(prefix=safe_name + "_", dir=root))
    result: dict[str, Any] = {
        "status": "AUTOCONFIG_FAILED", "failed_stage": "autoconfig",
        "reason": None, "error_class": None,
        "circuit_type": circuit_type, "circuit_id": circuit_id,
        "artifact_paths": {"run_directory": str(run_dir)},
        "costs": {"evaluation_backend": "artifact" if profile_artifact else (
                      "mock" if point_evaluator is not None else "spice"),
                  "anchor_spice_point_evaluations": 0,
                  "profile_spice_point_evaluations": 0,
                  "llm_calls": 0, "llm_retries": 0, "llm_usage": [],
                  "anchor_seconds": 0.0, "profiling_seconds": 0.0,
                  "target_generation_seconds": 0.0},
    }

    def finish() -> dict[str, Any]:
        result["elapsed_seconds"] = perf_counter() - started
        report_path = run_dir / "preparation_result.json"
        result["artifact_paths"]["result"] = str(report_path)
        _write_json(report_path, result)
        return result

    try:
        runtime = runtime or build_active_learning_circuit_config(
            circuit_type, circuit_id, repository_root=_REPOSITORY_ROOT
        )
        if runtime.circuit_type != circuit_type or runtime.circuit_id != circuit_id:
            raise ValueError("Provided runtime identity does not match requested circuit")
        template = yaml.safe_load(runtime.performance_template_path.read_text(encoding="utf-8"))
        contract = metric_contract(template)
    except Exception as error:
        result.update(reason=str(error), error_class=type(error).__name__)
        return finish()

    if base_config_path is None:
        base = (_ROOT / "InitializeD0" / "examples" / circuit_type / circuit_id
                / "active_learning_config.yaml")
    else:
        base = Path(base_config_path).resolve()
        if not base.is_file():
            result.update(status="CONFIG_GENERATION_FAILED", failed_stage="config",
                          reason=f"Base config does not exist: {base}", error_class="FileNotFoundError")
            return finish()

    profile_path = run_dir / "profiling.json"
    if profile_artifact is not None:
        try:
            profile = _artifact_profile(Path(profile_artifact), runtime, tuple(contract))
            profile_path = Path(profile_artifact).resolve()
        except Exception as error:
            result.update(status="PROFILING_FAILED", failed_stage="profiling",
                          reason=str(error), error_class=type(error).__name__)
            return finish()
    else:
        if point_evaluator is None and not allow_spice:
            result.update(status="PROFILING_FAILED", failed_stage="profiling",
                          reason="Real SPICE requires explicit allow_spice=True or an injected evaluator",
                          error_class="PermissionError")
            return finish()
        try:
            evaluate_point = point_evaluator or _real_point_evaluator(runtime)
            anchor_result = discover_anchor(
                runtime, evaluate_point, tuple(contract),
                max_evaluations=max_anchor_evaluations,
                local_evaluations=anchor_local_evaluations,
                local_radius=local_radius, seed=seed,
            )
        except Exception as error:
            result.update(status="ANCHOR_SIMULATION_FAILED", failed_stage="anchor",
                          reason=str(error), error_class=type(error).__name__)
            return finish()
        result["costs"]["anchor_spice_point_evaluations"] = anchor_result["spice_point_evaluations"]
        result["costs"]["anchor_seconds"] = anchor_result["elapsed_seconds"]
        anchor_path = run_dir / "anchor.json"
        _write_json(anchor_path, anchor_result)
        result["artifact_paths"]["anchor"] = str(anchor_path)
        if anchor_result["anchor"] is None:
            result.update(status=anchor_result["status"], failed_stage="anchor",
                          reason="No functionally valid anchor within the bounded search",
                          error_class="AnchorSearchFailure")
            return finish()
        try:
            profile = run_bounded_profile(
                runtime, anchor_result, evaluate_point, tuple(contract),
                samples=profile_samples, max_evaluations=max_profile_evaluations,
                additional_samples=additional_samples,
                min_valid_samples=min_valid_samples,
                local_radius=local_radius, seed=seed + 100,
            )
            result["costs"]["profile_spice_point_evaluations"] = profile["spice_point_evaluations"]
            result["costs"]["profiling_seconds"] = profile["elapsed_seconds"]
            profile["evaluation_backend"] = (
                "mock" if point_evaluator is not None else "spice"
            )
            _write_json(profile_path, profile)
        except Exception as error:
            result.update(status="PROFILING_FAILED", failed_stage="profiling",
                          reason=str(error), error_class=type(error).__name__)
            return finish()
    result["artifact_paths"]["profiling"] = str(profile_path)
    result["profile_counts"] = {
        key: profile[key] for key in (
            "total_samples", "parameter_valid_samples", "simulation_valid_samples",
            "functional_valid_samples",
        )
    }
    if profile.get("evaluator_unstable"):
        result.update(status="PROFILING_FAILED", failed_stage="profiling",
                      reason="Evaluator raised three consecutive simulation exceptions",
                      error_class="EvaluatorUnstable")
        return finish()
    if profile["functional_valid_samples"] < min_valid_samples:
        result.update(status="INSUFFICIENT_VALID_SAMPLES", failed_stage="profiling",
                      reason=f"Need {min_valid_samples} functional-valid profile rows",
                      error_class="InsufficientValidSamples")
        return finish()
    if llm_client is None and not allow_llm:
        result.update(status="AWAITING_LLM_APPROVAL", failed_stage="target_generation",
                      reason="Real LLM request requires explicit allow_llm=True")
        return finish()
    try:
        target_result = recommend_target(
            runtime, template, profile,
            llm_client=llm_client or call_inverse_design_llm,
            provider=provider, model=model, max_llm_calls=max_llm_calls,
        )
    except Exception as error:
        result.update(status="TARGET_GENERATION_FAILED", failed_stage="target_generation",
                      reason=str(error), error_class=type(error).__name__)
        return finish()
    target_result["llm_backend"] = (
        "injected" if llm_client is not None else "provider_api"
    )
    result["costs"].update(
        llm_calls=target_result["attempts"],
        llm_retries=max(0, target_result["attempts"] - 1),
        llm_usage=target_result["usage"],
        llm_backend=target_result["llm_backend"],
        target_generation_seconds=target_result["elapsed_seconds"],
    )
    target_path = run_dir / "target_generation.json"
    try:
        _write_json(target_path, target_result)
    except Exception as error:
        result.update(status="TARGET_GENERATION_FAILED", failed_stage="target_generation",
                      reason=str(error), error_class=type(error).__name__)
        return finish()
    result["artifact_paths"]["target_generation"] = str(target_path)
    if target_result["status"] != "TARGET_VALIDATED":
        result.update(
            status=target_result["status"],
            failed_stage=("target_validation" if target_result["status"] == "TARGET_VALIDATION_FAILED"
                          else "target_generation"),
            reason=target_result["reason"],
            error_class=("TargetValidationError" if target_result["status"] == "TARGET_VALIDATION_FAILED"
                         else "TargetGenerationError"),
            validation=target_result.get("validation"),
        )
        return finish()
    result["validation"] = target_result["validation"]
    try:
        config_path = run_dir / "active_learning_config.yaml"
        generated_config = generate_experiment_config(
            runtime, profile, target_result, output_path=config_path,
            base_config_path=base if base.is_file() else None,
            seed=seed, profile_path=profile_path, d0_mode=d0_mode,
        )
        from ActiveLearning.main import _resolve_experiment

        resolved_d0_path = _resolve_experiment(
            circuit_type, circuit_id, config_path, runtime
        )[3]
        result["artifact_paths"]["config"] = str(config_path)
        result["d0"] = {
            "mode": d0_mode,
            "path": str(resolved_d0_path.resolve()),
            "anchor_sample_id": generated_config["data"].get("initial_anchor", {}).get("sample_id"),
            "seed": generated_config["data"].get("seed"),
            "initialized": False,
        }
    except Exception as error:
        result.update(status="CONFIG_GENERATION_FAILED", failed_stage="config",
                      reason=str(error), error_class=type(error).__name__)
        return finish()
    result.update(status=_SUCCESS, failed_stage=None, reason=None)
    return finish()


def main(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Bounded profiling and experimental target preparation")
    parser.add_argument("--circuit-type", required=True)
    parser.add_argument("--circuit-id", required=True)
    parser.add_argument("--profile-samples", type=int, default=25)
    parser.add_argument("--max-profile-evaluations", type=int, default=40)
    parser.add_argument("--additional-samples", type=int, default=10)
    parser.add_argument("--min-valid-samples", type=int, default=2)
    parser.add_argument("--max-anchor-evaluations", type=int, default=12)
    parser.add_argument("--anchor-local-evaluations", type=int, default=6)
    parser.add_argument("--local-radius", type=float, default=0.02)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--d0-mode", choices=("independent", "reuse"), default="independent")
    parser.add_argument("--profile-artifact")
    parser.add_argument("--base-config")
    parser.add_argument("--output-root", default=str(_DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--provider", choices=("openai", "gemini"))
    parser.add_argument("--model")
    parser.add_argument("--max-llm-calls", type=int, default=4)
    parser.add_argument("--allow-spice", action="store_true")
    parser.add_argument("--allow-llm", action="store_true")
    args = parser.parse_args(argv)
    result = prepare_experiment(
        args.circuit_type, args.circuit_id,
        profile_samples=args.profile_samples,
        max_profile_evaluations=args.max_profile_evaluations,
        additional_samples=args.additional_samples,
        min_valid_samples=args.min_valid_samples,
        max_anchor_evaluations=args.max_anchor_evaluations,
        anchor_local_evaluations=args.anchor_local_evaluations,
        local_radius=args.local_radius,
        seed=args.seed, d0_mode=args.d0_mode,
        profile_artifact=args.profile_artifact,
        base_config_path=args.base_config, output_root=args.output_root,
        provider=args.provider, model=args.model,
        max_llm_calls=args.max_llm_calls,
        allow_spice=args.allow_spice, allow_llm=args.allow_llm,
    )
    print(json.dumps({key: result[key] for key in (
        "status", "failed_stage", "reason", "artifact_paths", "costs"
    )}, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    raise SystemExit(
        0 if main()["status"] in {_SUCCESS, "AWAITING_LLM_APPROVAL"} else 1
    )
