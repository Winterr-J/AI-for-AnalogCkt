"""Current Mirror D0 initialization orchestration and dataset persistence."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .evaluator import evaluate_current_mirror
from .sampling import (
    generate_v1_global_samples,
    generate_v2_local_global_samples,
    generate_v3_physical_local_global_samples,
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

    if sampling_mode == "v1":
        samples = generate_v1_global_samples(
            parameter_template,
            n_samples=n_samples,
            seed=seed,
        )
    elif sampling_mode == "v2":
        samples = generate_v2_local_global_samples(
            parameter_template,
            n_local=n_local,
            n_global=n_global,
            local_radius=local_radius,
            seed=seed,
        )
    elif sampling_mode == "v3":
        samples = generate_v3_physical_local_global_samples(
            parameter_template,
            n_local=n_local,
            n_global=n_global,
            local_factor=local_factor,
            seed=seed,
        )
    else:
        raise ValueError("sampling_mode must be 'v1', 'v2', or 'v3'")

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

    with jsonl_path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    param_names = _ordered_keys(records, "params")
    normalized_names = _ordered_keys(records, "normalized_params")
    fieldnames = [
        "sample_id",
        "sampling_source",
        *param_names,
        *(f"norm_{name}" for name in normalized_names),
        "current_error",
        "output_resistance",
        "compliance_relative_variation",
        "tracking_max_error",
        "parameter_valid",
        "simulation_valid",
        "functional_feasible",
        "meets_spec",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {
                "sample_id": record["sample_id"],
                "sampling_source": record["sampling_source"],
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
