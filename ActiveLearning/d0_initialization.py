"""Initialize a configured D0 dataset from an AutoConfig runtime."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from ActiveLearning.InitializeD0 import build_runtime_d0, save_d0_dataset


def _functional_config(data_config: Mapping[str, Any], adapter: Any) -> dict[str, Any]:
    configured = data_config.get("functional_config")
    if configured is not None:
        return dict(configured)
    if not hasattr(adapter, "vout_nominal"):
        return {}
    nominal = float(adapter.vout_nominal)
    return {
        "compliance_vout_start": max(0.05, nominal - 1.0),
        "compliance_vout_stop": nominal + 1.0,
        "compliance_points": 11,
        "reference_scales": (0.8, 1.0, 1.2),
    }


def initialize_configured_d0(
    runtime: Any,
    experiment_config: Mapping[str, Any],
    target_specs: Mapping[str, Any],
    output_path: str | Path,
) -> dict[str, Any]:
    """Build and persist D0 using only AutoConfig structure and experiment settings."""

    destination = Path(output_path)
    if destination.suffix.casefold() != ".jsonl":
        raise ValueError("configured D0 path must end in .jsonl")
    if destination.exists() or destination.with_suffix(".csv").exists():
        raise FileExistsError(f"configured D0 output already exists: {destination}")
    data_config = dict(experiment_config.get("data") or {})
    if data_config.get("d0_mode") == "reuse":
        raise ValueError("reuse D0 mode cannot initialize a new dataset")
    manifest_path = destination.with_name(destination.stem + "_initialization.json")
    if data_config.get("d0_mode") == "independent" and manifest_path.exists():
        raise FileExistsError(f"configured D0 manifest already exists: {manifest_path}")
    initial_anchor = data_config.get("initial_anchor")
    if data_config.get("d0_mode") == "independent" and initial_anchor is None:
        raise ValueError("independent D0 requires a profiling initial_anchor")
    sampling_mode = str(data_config.get("sampling_mode", "v3"))
    if sampling_mode == "v4" and "local_radius" not in data_config:
        raise ValueError("D0 V4 requires data.local_radius to be configured")
    seed = int(data_config.get("seed", 42))
    settings = {
        "sampling_mode": sampling_mode,
        "seed": seed,
        "n_samples": int(data_config.get("n_samples", 20)),
        "n_local": int(data_config.get("n_local", 10)),
        "n_global": int(data_config.get("n_global", 9)),
        "local_radius": (
            float(data_config["local_radius"])
            if "local_radius" in data_config
            else (None if sampling_mode == "v4" else 0.05)
        ),
        "local_factor": float(data_config.get("local_factor", 1.25)),
    }
    started = perf_counter()
    records = build_runtime_d0(
        runtime,
        functional_config=_functional_config(data_config, runtime.adapter),
        target_specs=target_specs,
        initial_anchor=initial_anchor,
        **settings,
    )
    artifacts = save_d0_dataset(records, destination.parent, destination.stem)
    if artifacts["jsonl"].resolve() != destination.resolve():
        raise RuntimeError("D0 persistence path does not match configured d0_path")
    if data_config.get("d0_mode") == "independent":
        metadata = {
            "d0_mode": "independent",
            "target_source": experiment_config.get("target_source", "user_specified"),
            "anchor_source": initial_anchor.get("source", "profiling"),
            "anchor_sample_id": str(initial_anchor["sample_id"]),
            "seed": seed,
            "sampling": settings,
            "records": len(records),
            "logical_candidate_evaluations": sum(
                record["status"].get("parameter_valid") is True for record in records
            ),
            "elapsed_seconds": perf_counter() - started,
            "generated_config": "../" + str(
                (experiment_config.get("target_provenance") or {}).get(
                    "generated_config", "active_learning_config.yaml"
                )
            ),
            "artifacts": {name: path.name for name, path in artifacts.items()},
        }
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump(metadata, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        artifacts["manifest"] = manifest_path
    return {
        "records": records,
        "artifacts": {name: path.resolve() for name, path in artifacts.items()},
        "sampling": settings,
        "d0_mode": data_config.get("d0_mode", "configured"),
        "anchor_sample_id": (
            str(initial_anchor["sample_id"]) if initial_anchor is not None else None
        ),
    }
