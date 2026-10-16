"""Runtime-driven D0 orchestration with the legacy CurrentMirror API preserved."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from .evaluator import evaluate_current_mirror
from .sampling import (
    generate_v1_global_samples,
    generate_v2_local_global_samples,
    generate_v3_physical_local_global_samples,
    generate_v4_norm_bounded_local_global_samples,
)
from .validation import (
    check_current_mirror_functionality,
    check_meets_spec,
    validate_parameters,
)


def _empty_record(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": sample["sample_id"],
        "sampling_source": sample["sampling_source"],
        "params": dict(sample["params"]),
        "normalized_params": dict(sample["normalized_params"]),
        "metrics": {
            "current_error": None,
            "output_resistance": None,
        },
        "functional_metrics": {
            "compliance_relative_variation": None,
            "tracking_max_error": None,
        },
        "status": {
            "parameter_valid": None,
            "simulation_valid": None,
            "functional_feasible": None,
            "meets_spec": None,
        },
    }


def _generate_samples(
    parameter_template: Mapping[str, Any],
    *,
    sampling_mode: str,
    seed: int,
    n_samples: int,
    n_local: int,
    n_global: int,
    local_radius: float | None,
    local_factor: float,
    center_params: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    center_kwargs = {} if center_params is None else {"center_params": center_params}
    if sampling_mode == "v1":
        return generate_v1_global_samples(
            parameter_template,
            n_samples=n_samples,
            seed=seed,
            **center_kwargs,
        )
    if sampling_mode == "v2":
        return generate_v2_local_global_samples(
            parameter_template,
            n_local=n_local,
            n_global=n_global,
            local_radius=0.05 if local_radius is None else local_radius,
            seed=seed,
            **center_kwargs,
        )
    if sampling_mode == "v3":
        return generate_v3_physical_local_global_samples(
            parameter_template,
            n_local=n_local,
            n_global=n_global,
            local_factor=local_factor,
            seed=seed,
            **center_kwargs,
        )
    if sampling_mode == "v4":
        if local_radius is None:
            raise ValueError("v4 requires an explicit local_radius")
        return generate_v4_norm_bounded_local_global_samples(
            parameter_template,
            local_radius=local_radius,
            n_local=n_local,
            n_global=n_global,
            seed=seed,
            **center_kwargs,
        )
    raise ValueError("sampling_mode must be 'v1', 'v2', 'v3', or 'v4'")


def build_current_mirror_d0(
    parameter_template: Mapping[str, Any],
    adapter: Any,
    functional_config: Mapping[str, Any],
    target_specs: Mapping[str, Any] | None = None,
    sampling_mode: str = "v1",
    seed: int = 42,
    n_samples: int = 20,
    n_local: int = 10,
    n_global: int = 9,
    local_radius: float = 0.05,
    local_factor: float = 1.25,
) -> list[dict[str, Any]]:
    """Build one Current Mirror D0 population using the selected sampler."""

    samples = _generate_samples(
        parameter_template,
        sampling_mode=sampling_mode,
        seed=seed,
        n_samples=n_samples,
        n_local=n_local,
        n_global=n_global,
        local_radius=local_radius,
        local_factor=local_factor,
    )

    records = []
    for sample in samples:
        record = _empty_record(sample)
        params = record["params"]

        parameter_result = validate_parameters(params, parameter_template)
        parameter_valid = parameter_result["parameter_valid"]
        record["status"]["parameter_valid"] = parameter_valid
        if not parameter_valid:
            records.append(record)
            continue

        current_error, output_resistance, simulation_valid = evaluate_current_mirror(
            params,
            adapter=adapter,
        )
        record["status"]["simulation_valid"] = simulation_valid
        if not simulation_valid:
            records.append(record)
            continue

        metrics = {
            "current_error": current_error,
            "output_resistance": output_resistance,
        }
        record["metrics"] = metrics

        functional_result = check_current_mirror_functionality(
            params,
            adapter=adapter,
            **functional_config,
        )
        record["status"]["functional_feasible"] = functional_result[
            "functional_feasible"
        ]
        record["functional_metrics"] = {
            "compliance_relative_variation": functional_result["compliance"][
                "relative_variation"
            ],
            "tracking_max_error": functional_result["reference_tracking"][
                "max_tracking_error"
            ],
        }

        record["status"]["meets_spec"] = check_meets_spec(
            metrics,
            target_specs,
        )["meets_spec"]
        records.append(record)

    return records


def _runtime_metric_names(runtime: Any) -> list[str]:
    with Path(runtime.performance_template_path).open(encoding="utf-8") as stream:
        template = yaml.safe_load(stream)
    return list(template["performance_template"]["active_metrics"])


def _empty_runtime_record(
    sample: Mapping[str, Any],
    metric_names: Sequence[str],
) -> dict[str, Any]:
    record = {
        "sample_id": sample["sample_id"],
        "sampling_source": sample["sampling_source"],
        "params": dict(sample["params"]),
        "normalized_params": dict(sample["normalized_params"]),
        "metrics": {name: None for name in metric_names},
        "functional_metrics": {},
        "functional_checks": {},
        "failure_reasons": [],
        "diagnostics": {},
        "status": {
            "parameter_valid": None,
            "simulation_valid": None,
            "functional_valid": None,
            "functional_status": None,
            # Compatibility alias consumed by existing AL context code.
            "functional_feasible": None,
            "meets_spec": None,
        },
    }
    for key in (
        "requested_radius", "rho", "actual_normalized_l2_distance",
        "boundary_fallback", "anchor_sample_id", "anchor_source",
    ):
        if key in sample:
            record[key] = sample[key]
    return record


def _apply_structured_validation(
    record: dict[str, Any],
    validation: Mapping[str, Any],
) -> None:
    status = dict(validation["status"])
    functional_valid = status.get("functional_valid")
    status["functional_feasible"] = functional_valid
    record["status"].update(status)
    record["metrics"] = dict(validation.get("metrics") or record["metrics"])
    record["functional_checks"] = dict(validation.get("functional_checks") or {})
    record["failure_reasons"] = list(validation.get("failure_reasons") or [])
    record["diagnostics"] = dict(validation.get("diagnostics") or {})


def build_runtime_d0(
    runtime: Any,
    *,
    functional_config: Mapping[str, Any] | None = None,
    target_specs: Mapping[str, Any] | None = None,
    sampling_mode: str = "v1",
    seed: int = 42,
    n_samples: int = 20,
    n_local: int = 10,
    n_global: int = 9,
    local_radius: float | None = None,
    local_factor: float = 1.25,
    initial_anchor: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build D0 through one AutoConfig runtime's evaluator and validator."""

    parameter_template = runtime.param_template
    center_params = None
    if initial_anchor is not None:
        if not isinstance(initial_anchor, Mapping) or not isinstance(
            initial_anchor.get("params"), Mapping
        ) or not initial_anchor.get("sample_id"):
            raise ValueError("initial_anchor requires sample_id and params")
        center_params = dict(initial_anchor["params"])
        anchor_validation = validate_parameters(center_params, parameter_template)
        if not anchor_validation["parameter_valid"]:
            raise ValueError(
                "initial_anchor params are invalid: "
                + "; ".join(anchor_validation["violations"])
            )
    samples = _generate_samples(
        parameter_template,
        sampling_mode=sampling_mode,
        seed=seed,
        n_samples=n_samples,
        n_local=n_local,
        n_global=n_global,
        local_radius=local_radius,
        local_factor=local_factor,
        center_params=center_params,
    )
    if samples:
        samples[0] = {
            **samples[0],
            "sampling_source": "anchor" if initial_anchor is not None else "nominal",
            **({
                "anchor_sample_id": str(initial_anchor["sample_id"]),
                "anchor_source": str(initial_anchor.get("source", "profiling")),
            } if initial_anchor is not None else {}),
        }

    metric_names = _runtime_metric_names(runtime)
    evaluator = runtime.create_evaluator()
    functional_validator = runtime.create_functional_validator()
    records = []
    for sample in samples:
        record = _empty_runtime_record(sample, metric_names)
        params = record["params"]
        parameter_result = validate_parameters(params, parameter_template)
        record["status"]["parameter_valid"] = parameter_result["parameter_valid"]
        if not parameter_result["parameter_valid"]:
            record["status"].update(
                {
                    "functional_valid": False,
                    "functional_status": "invalid",
                    "functional_feasible": False,
                }
            )
            record["failure_reasons"] = ["parameter_invalid"]
            record["diagnostics"] = {
                "parameter_violations": list(parameter_result["violations"])
            }
            records.append(record)
            continue

        if hasattr(evaluator, "evaluate") and hasattr(functional_validator, "validate"):
            evaluation = evaluator.evaluate(params)
            validation = functional_validator.validate(
                params,
                evaluation,
                target_specs=target_specs,
            )
            _apply_structured_validation(record, validation)
            records.append(record)
            continue

        evaluation = evaluator(params)
        if not isinstance(evaluation, tuple) or len(evaluation) != len(metric_names) + 1:
            raise TypeError(
                "legacy runtime evaluator must return one value per active metric "
                "followed by simulation_valid"
            )
        *metric_values, simulation_valid = evaluation
        record["status"]["simulation_valid"] = bool(simulation_valid)
        if not simulation_valid:
            records.append(record)
            continue

        metrics = dict(zip(metric_names, metric_values))
        record["metrics"] = metrics
        functional_result = functional_validator(
            params,
            **dict(functional_config or {}),
        )
        functional_valid = bool(functional_result["functional_feasible"])
        record["status"].update(
            {
                "functional_valid": functional_valid,
                "functional_status": "valid" if functional_valid else "invalid",
                "functional_feasible": functional_valid,
                "meets_spec": check_meets_spec(metrics, target_specs)["meets_spec"],
            }
        )
        record["functional_metrics"] = {
            "compliance_relative_variation": functional_result["compliance"][
                "relative_variation"
            ],
            "tracking_max_error": functional_result["reference_tracking"][
                "max_tracking_error"
            ],
        }
        records.append(record)
    return records


def _ordered_keys(records: Sequence[Mapping[str, Any]], field: str) -> list[str]:
    return list(
        dict.fromkeys(
            key
            for record in records
            for key in record[field]
        )
    )


def save_d0_dataset(
    records: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    dataset_name: str,
) -> dict[str, Path]:
    """Save canonical JSONL records and a flattened CSV summary."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    jsonl_path = destination / f"{dataset_name}.jsonl"
    csv_path = destination / f"{dataset_name}.csv"
    if jsonl_path.exists() or csv_path.exists():
        raise FileExistsError(f"D0 output already exists: {jsonl_path} or {csv_path}")

    with jsonl_path.open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    param_names = _ordered_keys(records, "params")
    normalized_names = _ordered_keys(records, "normalized_params")
    metric_names = _ordered_keys(records, "metrics")
    functional_metric_names = _ordered_keys(records, "functional_metrics")
    status_names = _ordered_keys(records, "status")
    fieldnames = [
        "sample_id",
        "sampling_source",
        *(
            key
            for key in (
                "requested_radius", "rho", "actual_normalized_l2_distance",
                "boundary_fallback", "anchor_sample_id", "anchor_source",
            )
            if any(key in record for record in records)
        ),
        *param_names,
        *(f"norm_{name}" for name in normalized_names),
        *metric_names,
        *functional_metric_names,
        *status_names,
    ]
    with csv_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {
                "sample_id": record["sample_id"],
                "sampling_source": record["sampling_source"],
                **{
                    key: record[key]
                    for key in (
                        "requested_radius", "rho", "actual_normalized_l2_distance",
                        "boundary_fallback", "anchor_sample_id", "anchor_source",
                    )
                    if key in record
                },
                **record["params"],
                **{
                    f"norm_{name}": value
                    for name, value in record["normalized_params"].items()
                },
                **record["metrics"],
                **record["functional_metrics"],
                **record["status"],
            }
            writer.writerow(row)

    return {"jsonl": jsonl_path, "csv": csv_path}
