"""Persistent, append-only experiment artifacts for Active Learning runs."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path


RUN_INDEX_FIELDS = (
    "run_id",
    "timestamp",
    "circuit_type",
    "circuit_id",
    "acquisition_mode",
    "iterations_completed",
    "stop_reason",
    "parameter_valid",
    "simulation_valid",
    "functional_valid",
    "meets_spec",
    "joint_success",
    "numerical_spec_success",
    "functional_joint_success",
    "functional_unverified",
    "current_error",
    "output_resistance",
    "total_seconds",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "llm_calls",
    "run_directory",
)


def _success_classification(circuit_type, status):
    numerical = status.get("meets_spec") is True
    if circuit_type != "CurrentMirror":
        return numerical, None, False
    required = (
        status.get("parameter_valid"),
        status.get("simulation_valid"),
        status.get("functional_valid"),
        status.get("meets_spec"),
    )
    joint = status.get("joint_success")
    if not all(type(value) is bool for value in required) or type(joint) is not bool:
        if joint is True:
            raise ValueError("CurrentMirror joint_success lacks explicit validity evidence")
        return numerical, None, True
    expected = all(required)
    if joint is not expected:
        raise ValueError("CurrentMirror joint_success contradicts explicit validity fields")
    return numerical, joint, False


def _safe_label(value):
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(value)
    )


def create_run_directory(
    runs_root,
    circuit_type,
    circuit_id,
    acquisition_mode,
    *,
    now=None,
    data_expansion_mode="v1",
):
    """Create and return unique run metadata without overwriting prior runs."""

    started_at = datetime.now().astimezone() if now is None else now
    timestamp_id = started_at.strftime("%Y%m%d_%H%M%S")
    base_run_id = f"{timestamp_id}_{_safe_label(acquisition_mode)}"
    if data_expansion_mode != "v1":
        base_run_id += f"_dataexp_{_safe_label(data_expansion_mode)}"
    parent = Path(runs_root) / circuit_type / circuit_id
    parent.mkdir(parents=True, exist_ok=True)

    suffix = 1
    while True:
        run_id = base_run_id if suffix == 1 else f"{base_run_id}_{suffix}"
        run_directory = parent / run_id
        try:
            run_directory.mkdir()
        except FileExistsError:
            suffix += 1
            continue
        break

    (run_directory / "iteration_log.jsonl").touch()
    return {
        "run_id": run_id,
        "timestamp": started_at.isoformat(timespec="seconds"),
        "run_directory": run_directory,
    }


def _write_json(path, payload):
    with Path(path).open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False, default=float)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_config_snapshot(run_directory, effective_config):
    path = Path(run_directory) / "config_snapshot.json"
    _write_json(path, effective_config)
    return path


def append_jsonl_record(path, record):
    """Append and durably flush one completed record."""

    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=float) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_completed_run(
    runs_root,
    run_context,
    result,
    *,
    circuit_type,
    circuit_id,
    acquisition_mode,
    data_expansion_mode="v1",
    benchmark_version=None,
):
    """Write a completed summary and append exactly one run-index row."""

    final_record = result.get("best_or_final_record")
    status = final_record.get("status", {}) if final_record else {}
    functional_valid = status.get("functional_valid")
    if functional_valid is None and circuit_type != "CurrentMirror":
        functional_valid = status.get("functional_feasible")
    metrics = final_record.get("metrics", {}) if final_record else {}
    total_timing = result.get("total_timing", {})
    total_tokens = result.get("total_tokens", {})
    numerical_success, functional_joint_success, functional_unverified = (
        _success_classification(circuit_type, status)
    )
    summary = {
        "success_schema_version": "al_success_status_v2",
        "run_id": run_context["run_id"],
        "timestamp": run_context["timestamp"],
        "circuit_type": circuit_type,
        "circuit_id": circuit_id,
        "benchmark_version": benchmark_version,
        "acquisition_mode": acquisition_mode,
        "data_expansion_mode": data_expansion_mode,
        "iterations_completed": result.get("iterations_completed"),
        "stop_reason": result.get("stop_reason"),
        "parameter_valid": status.get("parameter_valid"),
        "simulation_valid": status.get("simulation_valid"),
        "functional_valid": functional_valid,
        "meets_spec": status.get("meets_spec"),
        "joint_success": (
            functional_joint_success if circuit_type == "CurrentMirror" else None
        ),
        "numerical_spec_success": numerical_success,
        "functional_joint_success": functional_joint_success,
        "functional_unverified": functional_unverified,
        "final_metrics": metrics,
        "total_timing": total_timing,
        "total_tokens": total_tokens,
        "llm_calls": result.get("llm_calls"),
        "usage_unknown": result.get("usage_unknown", False),
        "evaluation_counts": result.get("evaluation_counts", {}),
    }
    if circuit_type != "CurrentMirror":
        summary.pop("joint_success")
        summary.pop("functional_joint_success")
        summary.pop("functional_unverified")
    if result.get("sample_hold_pilot_execution") is not None:
        summary["sample_hold_pilot_execution"] = result[
            "sample_hold_pilot_execution"
        ]
    _write_json(Path(run_context["run_directory"]) / "summary.json", summary)

    row = {
        "run_id": run_context["run_id"],
        "timestamp": run_context["timestamp"],
        "circuit_type": circuit_type,
        "circuit_id": circuit_id,
        "acquisition_mode": acquisition_mode,
        "iterations_completed": result.get("iterations_completed"),
        "stop_reason": result.get("stop_reason"),
        "parameter_valid": status.get("parameter_valid"),
        "simulation_valid": status.get("simulation_valid"),
        "functional_valid": functional_valid,
        "meets_spec": status.get("meets_spec"),
        "joint_success": functional_joint_success,
        "numerical_spec_success": numerical_success,
        "functional_joint_success": functional_joint_success,
        "functional_unverified": (
            functional_unverified if circuit_type == "CurrentMirror" else None
        ),
        "current_error": metrics.get("current_error"),
        "output_resistance": metrics.get("output_resistance"),
        "total_seconds": total_timing.get("total_seconds"),
        "input_tokens": total_tokens.get("input_tokens"),
        "output_tokens": total_tokens.get("output_tokens"),
        "total_tokens": total_tokens.get("total_tokens"),
        "llm_calls": result.get("llm_calls"),
        "run_directory": str(Path(run_context["run_directory"]).resolve()),
    }
    index_path = Path(runs_root) / "run_index.csv"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not index_path.exists() or index_path.stat().st_size == 0
    fieldnames = RUN_INDEX_FIELDS
    if not needs_header:
        with index_path.open(encoding="utf-8", newline="") as stream:
            existing_header = next(csv.reader(stream), [])
        if existing_header:
            # Never rewrite or backfill a historical index. New roots use V2
            # columns; existing roots retain their original append-only schema.
            fieldnames = tuple(existing_header)
    with index_path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        if needs_header:
            writer.writeheader()
        writer.writerow({name: row.get(name) for name in fieldnames})
        stream.flush()
        os.fsync(stream.fileno())
    return summary
