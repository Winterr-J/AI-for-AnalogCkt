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
    "meets_spec",
    "current_error",
    "output_resistance",
    "total_seconds",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "llm_calls",
    "run_directory",
)


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
    if functional_valid is None:
        functional_valid = status.get("functional_feasible")
    metrics = final_record.get("metrics", {}) if final_record else {}
    total_timing = result.get("total_timing", {})
    total_tokens = result.get("total_tokens", {})
    summary = {
        "run_id": run_context["run_id"],
        "timestamp": run_context["timestamp"],
        "circuit_type": circuit_type,
        "circuit_id": circuit_id,
        "benchmark_version": benchmark_version,
        "acquisition_mode": acquisition_mode,
        "data_expansion_mode": data_expansion_mode,
        "iterations_completed": result.get("iterations_completed"),
        "stop_reason": result.get("stop_reason"),
        "simulation_valid": status.get("simulation_valid"),
        "functional_valid": functional_valid,
        "meets_spec": status.get("meets_spec"),
        "final_metrics": metrics,
        "total_timing": total_timing,
        "total_tokens": total_tokens,
        "llm_calls": result.get("llm_calls"),
        "evaluation_counts": result.get("evaluation_counts", {}),
    }
    _write_json(Path(run_context["run_directory"]) / "summary.json", summary)

    row = {
        "run_id": run_context["run_id"],
        "timestamp": run_context["timestamp"],
        "circuit_type": circuit_type,
        "circuit_id": circuit_id,
        "acquisition_mode": acquisition_mode,
        "iterations_completed": result.get("iterations_completed"),
        "stop_reason": result.get("stop_reason"),
        "meets_spec": status.get("meets_spec"),
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
    with index_path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=RUN_INDEX_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)
        stream.flush()
        os.fsync(stream.fileno())
    return summary
