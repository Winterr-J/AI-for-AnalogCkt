"""Low-error focused Real-SPICE profiling for benchmark construction.

This workflow is intentionally isolated from D0, Dt, retrieval, and the Active
Learning loop.  It reads a validated global profiling artifact, samples local
regions in the same transformed unit space, and reuses the production Current
Mirror evaluator and functional validator.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ActiveLearning.InitializeD0.evaluator import evaluate_current_mirror
from ActiveLearning.InitializeD0.validation import check_current_mirror_functionality
from ActiveLearning.onboarding import build_active_learning_circuit_config
from ActiveLearning.profiling import (
    _evaluate_point,
    _functional_config,
    _parameter_space,
    _pareto_records,
    _unit_to_physical,
)
from ActiveLearning.profiling_local_sampler import (
    SAMPLER_METADATA,
    SAMPLER_VERSION,
    sample_local_norm_bounded,
)


_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _ROOT.parent
_DEFAULT_GLOBAL_ROOT = _ROOT / "results" / "profiling"
_DEFAULT_OUTPUT_ROOT = _ROOT / "results" / "focused_profiling"
_LOW_ERROR_LEVELS = (0.05, 0.10, 0.15, 0.20)
_DISTRIBUTION_QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.95)


def resolve_focused_profiling_mode(circuit_type: str) -> str:
    modes = {
        "CurrentMirror": "current_mirror_low_error_v1",
        "OpAmp": "opamp_local_regions_v1",
    }
    try:
        return modes[str(circuit_type)]
    except KeyError as error:
        raise ValueError(
            f"Unsupported focused profiling circuit type: {circuit_type}"
        ) from error


def _physical_to_unit(value: float, parameter: Mapping[str, Any]) -> float:
    """Invert profiling's scale/transform mapping into a coordinate in [0, 1]."""

    lower = float(parameter["lower"])
    upper = float(parameter["upper"])
    numeric = float(value)
    transform = str(parameter.get("transform") or "").casefold()
    scale = str(parameter.get("scale") or "linear").casefold()
    if not math.isfinite(numeric) or not lower <= numeric <= upper:
        raise ValueError(
            f"{parameter['name']}: value {value!r} is outside [{lower}, {upper}]"
        )

    if transform == "log_magnitude" or scale in {"log-magnitude", "log_magnitude"}:
        if numeric == 0.0 or not (upper < 0.0 or lower > 0.0):
            raise ValueError(
                f"{parameter['name']}: log-magnitude requires a fixed nonzero sign"
            )
        magnitude_lower = min(abs(lower), abs(upper))
        magnitude_upper = max(abs(lower), abs(upper))
        coordinate = (
            (math.log(abs(numeric)) - math.log(magnitude_lower))
            / (math.log(magnitude_upper) - math.log(magnitude_lower))
        )
    elif scale == "log":
        if lower <= 0.0 or numeric <= 0.0:
            raise ValueError(f"{parameter['name']}: log scale requires positive values")
        coordinate = (
            (math.log(numeric) - math.log(lower))
            / (math.log(upper) - math.log(lower))
        )
    elif scale in {"linear", "none", ""}:
        coordinate = (numeric - lower) / (upper - lower)
    else:
        raise NotImplementedError(
            f"{parameter['name']}: unsupported profiling scale {parameter['scale']!r}"
        )
    if coordinate < -1.0e-12 or coordinate > 1.0 + 1.0e-12:
        raise ValueError(f"{parameter['name']}: transformed coordinate outside [0, 1]")
    return float(min(1.0, max(0.0, coordinate)))


def _normalized_params(
    params: Mapping[str, Any],
    parameter_space: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    return {
        parameter["name"]: _physical_to_unit(params[parameter["name"]], parameter)
        for parameter in parameter_space
    }


def _point_key(
    normalized_params: Mapping[str, Any],
    parameter_names: Sequence[str],
) -> tuple[str, ...]:
    return tuple(float(normalized_params[name]).hex() for name in parameter_names)


def _read_global_csv(
    path: Path,
    parameter_space: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    parameter_names = [str(item["name"]) for item in parameter_space]
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {
            "sample_id",
            "simulation_valid",
            "functional_valid",
            "current_error",
            "output_resistance",
            *(f"param_{name}" for name in parameter_names),
        }
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                f"Global profiling CSV is missing columns: {sorted(missing)}"
            )
        for row in reader:
            params = {
                name: float(row[f"param_{name}"])
                for name in parameter_names
            }
            normalized = {}
            for parameter in parameter_space:
                name = str(parameter["name"])
                stored = row.get(f"normalized_{name}")
                normalized[name] = (
                    float(stored)
                    if stored not in (None, "")
                    else _physical_to_unit(params[name], parameter)
                )
            functional_text = row.get("functional_valid", "")
            records.append(
                {
                    "sample_id": row["sample_id"],
                    "params": params,
                    "normalized_params": normalized,
                    "simulation_valid": row["simulation_valid"] == "True",
                    "functional_valid": (
                        True
                        if functional_text == "True"
                        else False if functional_text == "False" else None
                    ),
                    "current_error": (
                        float(row["current_error"])
                        if row.get("current_error") not in (None, "")
                        else None
                    ),
                    "output_resistance": (
                        float(row["output_resistance"])
                        if row.get("output_resistance") not in (None, "")
                        else None
                    ),
                }
            )
    return records


def load_latest_global_profile(
    runtime: Any,
    *,
    profiling_root: str | Path = _DEFAULT_GLOBAL_ROOT,
    expected_samples: int = 300,
    expected_sampler: str = "sobol",
    expected_seed: int = 42,
    expected_simulation_valid: int | None = None,
    expected_functional_valid: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], Path, Path]:
    """Load and strictly validate the newest matching global profile."""

    root = Path(profiling_root)
    summaries = sorted(
        root.glob(f"{runtime.circuit_id}_profiling_*_summary.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not summaries:
        raise FileNotFoundError(
            f"No global profiling summary found for {runtime.circuit_id} in {root}"
        )
    summary_path = summaries[0]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    expected = {
        "circuit_id": runtime.circuit_id,
        "requested_samples": int(expected_samples),
        "sampler": expected_sampler.casefold(),
        "seed": int(expected_seed),
    }
    if expected_simulation_valid is not None:
        expected["simulation_valid_samples"] = int(expected_simulation_valid)
    if expected_functional_valid is not None:
        expected["functional_valid_samples"] = int(expected_functional_valid)
    actual = {
        **{name: summary.get(name) for name in expected},
        "sampler": str(summary.get("sampler") or "").casefold(),
    }
    mismatches = [
        f"{name}: expected {value!r}, got {actual[name]!r}"
        for name, value in expected.items()
        if actual[name] != value
    ]
    if mismatches:
        raise RuntimeError(
            "Latest global profiling artifact is not the required refactored "
            "300-point Sobol run: " + "; ".join(mismatches)
        )
    if summary.get("target_pool_filter") != (
        "simulation_valid == True AND functional_valid == True"
    ):
        raise RuntimeError("Global profile does not use the required target-pool filter")
    csv_text = summary.get("artifacts", {}).get("csv")
    if not csv_text:
        raise RuntimeError("Global profiling summary does not identify its CSV artifact")
    csv_path = Path(csv_text)
    if not csv_path.is_absolute():
        csv_path = summary_path.parent / csv_path
    if not csv_path.is_file():
        raise FileNotFoundError(f"Global profiling CSV not found: {csv_path}")
    records = _read_global_csv(csv_path, _parameter_space(runtime))
    simulation_valid = sum(record["simulation_valid"] is True for record in records)
    functional_valid = sum(
        record["simulation_valid"] is True
        and record["functional_valid"] is True
        for record in records
    )
    if len(records) != expected_samples:
        raise RuntimeError(
            f"Global profiling CSV row count mismatch: {len(records)} != {expected_samples}"
        )
    if (
        simulation_valid != int(summary.get("simulation_valid_samples", -1))
        or functional_valid != int(summary.get("functional_valid_samples", -1))
    ):
        raise RuntimeError(
            "Global profiling CSV validity counts do not match its validated summary"
        )
    return summary, records, csv_path.resolve(), summary_path.resolve()


def _distance(first: Mapping[str, Any], second: Mapping[str, Any]) -> float:
    names = list(first)
    if set(names) != set(second):
        raise ValueError("Normalized parameter vectors use different parameter names")
    return float(
        math.sqrt(sum((float(first[name]) - float(second[name])) ** 2 for name in names))
    )


def select_anchors(
    runtime: Any,
    global_records: Sequence[Mapping[str, Any]],
    nominal_record: Mapping[str, Any],
    *,
    error_threshold: float = 0.10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select nominal, lowest-error, and a separated low-error anchor."""

    space = _parameter_space(runtime)
    nominal_params = {
        parameter.name: float(parameter.nominal)
        for parameter in runtime.runtime_parameters
    }
    nominal_normalized = _normalized_params(nominal_params, space)
    if not (
        nominal_record.get("simulation_valid") is True
        and nominal_record.get("functional_valid") is True
    ):
        raise RuntimeError("Stored nominal reference is not simulation/functional valid")
    anchors = [
        {
            "anchor_id": "anchor_a",
            "source": "nominal",
            "sample_id": str(nominal_record.get("sample_id") or "nominal_reference"),
            "params": nominal_params,
            "normalized_params": nominal_normalized,
            "current_error": float(nominal_record["current_error"]),
            "output_resistance": float(nominal_record["output_resistance"]),
        }
    ]
    pool = [
        dict(record)
        for record in global_records
        if record.get("simulation_valid") is True
        and record.get("functional_valid") is True
        and record.get("current_error") is not None
        and record.get("output_resistance") is not None
    ]
    if len(pool) < 2:
        raise RuntimeError("Global target pool must contain at least two anchor candidates")
    lowest = min(
        enumerate(pool),
        key=lambda item: (float(item[1]["current_error"]), item[0]),
    )[1]
    anchors.append(
        {
            "anchor_id": "anchor_b",
            "source": "global_profiling",
            "sample_id": str(lowest["sample_id"]),
            "params": dict(lowest["params"]),
            "normalized_params": dict(lowest["normalized_params"]),
            "current_error": float(lowest["current_error"]),
            "output_resistance": float(lowest["output_resistance"]),
        }
    )
    remaining_low_error = [
        record
        for record in pool
        if record["sample_id"] != lowest["sample_id"]
        and float(record["current_error"]) <= float(error_threshold)
    ]
    if remaining_low_error:
        second = max(
            enumerate(remaining_low_error),
            key=lambda item: (
                _distance(
                    lowest["normalized_params"],
                    item[1]["normalized_params"],
                ),
                -float(item[1]["current_error"]),
                -item[0],
            ),
        )[1]
    else:
        shortlist_size = min(10, max(2, int(math.ceil(math.sqrt(len(pool))))))
        low_error_shortlist = sorted(
            (
                record
                for record in pool
                if record["sample_id"] != lowest["sample_id"]
            ),
            key=lambda record: float(record["current_error"]),
        )[: min(shortlist_size, len(pool) - 1)]
        second = max(
            enumerate(low_error_shortlist),
            key=lambda item: (
                _distance(
                    lowest["normalized_params"],
                    item[1]["normalized_params"],
                ),
                -float(item[1]["current_error"]),
                -item[0],
            ),
        )[1]
    anchors.append(
        {
            "anchor_id": "anchor_c",
            "source": "global_profiling",
            "sample_id": str(second["sample_id"]),
            "params": dict(second["params"]),
            "normalized_params": dict(second["normalized_params"]),
            "current_error": float(second["current_error"]),
            "output_resistance": float(second["output_resistance"]),
        }
    )
    distances = []
    for first_index in range(len(anchors)):
        for second_index in range(first_index + 1, len(anchors)):
            first = anchors[first_index]
            second_anchor = anchors[second_index]
            distances.append(
                {
                    "first": first["anchor_id"],
                    "second": second_anchor["anchor_id"],
                    "distance": _distance(
                        first["normalized_params"],
                        second_anchor["normalized_params"],
                    ),
                }
            )
    return anchors, distances


def generate_focused_samples(
    runtime: Any,
    anchors: Sequence[Mapping[str, Any]],
    *,
    radii: Sequence[float],
    samples_per_region: int,
    seed: int,
    existing_global_records: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generate unique V2 norm-bounded samples around generic anchors."""

    if len(anchors) != 3:
        raise ValueError("Focused profiling requires exactly three anchors")
    if samples_per_region < 1:
        raise ValueError("samples_per_region must be at least 1")
    if not radii or any(not 0.0 < float(radius) <= 1.0 for radius in radii):
        raise ValueError("radii must contain values in (0, 1]")
    space = _parameter_space(runtime)
    names = [str(parameter["name"]) for parameter in space]
    existing_keys = {
        _point_key(record["normalized_params"], names)
        for record in existing_global_records
        if record.get("normalized_params")
    }
    selected_keys: set[tuple[str, ...]] = set()
    samples: list[dict[str, Any]] = []
    generated = 0
    within_duplicates = 0
    global_duplicates = 0
    boundary_rejections = 0
    directional_contractions = 0
    fallback_reflections = 0

    region_index = 0
    for anchor in anchors:
        center = np.asarray(
            [float(anchor["normalized_params"][name]) for name in names],
            dtype=float,
        )
        for radius_value in radii:
            radius = float(radius_value)
            local_seed = int(seed) + region_index
            rng = np.random.default_rng(local_seed)
            accepted = 0
            attempt = 0
            while accepted < samples_per_region:
                vectors, local_metadata = sample_local_norm_bounded(
                    center, radius, 1, rng
                )
                vector = vectors[0]
                boundary_rejections += local_metadata["rejected_out_of_bounds"]
                directional_contractions += local_metadata["directional_contractions"]
                fallback_reflections += local_metadata["fallback_reflections"]
                normalized = {
                    name: float(value) for name, value in zip(names, vector)
                }
                key = _point_key(normalized, names)
                generated += 1
                if key in selected_keys:
                    within_duplicates += 1
                elif key in existing_keys:
                    global_duplicates += 1
                else:
                    selected_keys.add(key)
                    params = {
                        parameter["name"]: _unit_to_physical(value, parameter)
                        for parameter, value in zip(space, vector)
                    }
                    samples.append(
                        {
                            "sample_id": f"focused_{len(samples):04d}",
                            "anchor_id": str(anchor["anchor_id"]),
                            "anchor_source": str(anchor["source"]),
                            "anchor_source_sample_id": anchor.get("sample_id"),
                            "radius": radius,
                            "local_seed": local_seed,
                            "local_sample_index": accepted,
                            "distance_l2": float(np.linalg.norm(vector - center)),
                            "sampler_version": SAMPLER_VERSION,
                            "params": params,
                            "normalized_params": normalized,
                        }
                    )
                    accepted += 1
                attempt += 1
                if attempt > max(1000, 10 * samples_per_region):
                    raise RuntimeError(
                        f"Unable to produce enough unique samples for "
                        f"{anchor['anchor_id']} radius={radius}"
                    )
            region_index += 1
    return samples, {
        **SAMPLER_METADATA,
        "dimension": len(space),
        "seed": int(seed),
        "radii": [float(value) for value in radii],
        "generated": generated,
        "within_focused_duplicates_removed": within_duplicates,
        "global_duplicates_removed": global_duplicates,
        "duplicates_removed": within_duplicates + global_duplicates,
        "final_unique_samples": len(samples),
        "boundary_rejections": boundary_rejections,
        "directional_contractions": directional_contractions,
        "fallback_reflections": fallback_reflections,
    }


def run_focused_spice_profile(
    runtime: Any,
    sampled_points: Sequence[Mapping[str, Any]],
    *,
    evaluator: Callable[..., tuple[float | None, float | None, bool]] = evaluate_current_mirror,
    functional_validator: Callable[..., Mapping[str, Any]] = check_current_mirror_functionality,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate exactly the focused samples, without re-running the nominal point."""

    config = _functional_config(runtime.adapter)
    records = []
    total = len(sampled_points)
    for index, sample in enumerate(sampled_points, start=1):
        record = _evaluate_point(
            sample,
            runtime,
            evaluator,
            functional_validator,
            config,
        )
        for name in (
            "anchor_id", "anchor_source", "anchor_source_sample_id", "radius",
            "local_seed", "local_sample_index", "distance_l2", "sampler_version",
        ):
            record[name] = sample.get(name)
        records.append(record)
        if progress is not None:
            progress(index, total)
    return records


def _distribution(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    result = {"count": len(values), "min": float(np.min(array))}
    for quantile in _DISTRIBUTION_QUANTILES:
        result[f"p{int(quantile * 100):02d}"] = float(np.quantile(array, quantile))
    result["max"] = float(np.max(array))
    return result


def _compact_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": record["sample_id"],
        "anchor_id": record.get("anchor_id"),
        "radius": record.get("radius"),
        "current_error": record["current_error"],
        "output_resistance": record["output_resistance"],
    }


def analyze_focused_profile(
    records: Sequence[Mapping[str, Any]],
    nominal_record: Mapping[str, Any],
    parameter_space: Sequence[Mapping[str, Any]],
    *,
    error_threshold: float = 0.10,
) -> dict[str, Any]:
    """Compute focused hit rates, conditional Rout, Pareto, and local deltas."""

    total = len(records)
    if total == 0:
        raise ValueError("Focused profiling records are empty")
    simulation_valid = [record for record in records if record["simulation_valid"]]
    functional_valid = [
        record
        for record in simulation_valid
        if record["functional_valid"] is True
    ]
    hits = {}
    for level in _LOW_ERROR_LEVELS:
        matching = [
            record
            for record in functional_valid
            if float(record["current_error"]) <= level
        ]
        hits[f"error_le_{int(level * 100):02d}_percent"] = {
            "threshold": level,
            "count": len(matching),
            "rate_all_samples": float(len(matching) / total),
            "rate_functional_valid": (
                float(len(matching) / len(functional_valid))
                if functional_valid
                else 0.0
            ),
        }
    low_error = [
        record
        for record in functional_valid
        if float(record["current_error"]) <= float(error_threshold)
    ]
    five_percent = [
        record
        for record in functional_valid
        if float(record["current_error"]) <= 0.05
    ]

    region_rows = []
    region_keys = sorted(
        {(str(record["anchor_id"]), float(record["radius"])) for record in records}
    )
    for anchor_id, radius in region_keys:
        region = [
            record
            for record in records
            if record["anchor_id"] == anchor_id
            and float(record["radius"]) == radius
        ]
        region_functional = [
            record
            for record in region
            if record["simulation_valid"] is True
            and record["functional_valid"] is True
        ]
        region_low_error = [
            record
            for record in region_functional
            if float(record["current_error"]) <= float(error_threshold)
        ]
        region_rows.append(
            {
                "anchor_id": anchor_id,
                "radius": radius,
                "samples": len(region),
                "functional_valid": len(region_functional),
                "error_threshold": float(error_threshold),
                "low_error_count": len(region_low_error),
            }
        )

    nominal_error = float(nominal_record["current_error"])
    nominal_rout = float(nominal_record["output_resistance"])
    low_error_better_rout = [
        record
        for record in low_error
        if float(record["output_resistance"]) > nominal_rout
    ]
    nominal_error_better_rout = [
        record
        for record in functional_valid
        if float(record["current_error"]) <= nominal_error
        and float(record["output_resistance"]) > nominal_rout
    ]

    pareto = _pareto_records(functional_valid) if functional_valid else []
    representative_pareto = pareto
    if len(pareto) > 10:
        indices = np.linspace(0, len(pareto) - 1, 10, dtype=int)
        representative_pareto = [pareto[index] for index in indices]

    nominal_normalized = _normalized_params(
        nominal_record["params"], parameter_space
    )
    parameter_deltas = []
    for parameter in parameter_space:
        name = str(parameter["name"])
        values = [
            float(record["normalized_params"][name]) - nominal_normalized[name]
            for record in low_error
        ]
        parameter_deltas.append(
            {
                "parameter": name,
                "count": len(values),
                "p25": float(np.quantile(values, 0.25)) if values else None,
                "median": float(np.quantile(values, 0.50)) if values else None,
                "p75": float(np.quantile(values, 0.75)) if values else None,
            }
        )

    low_error_distribution = _distribution(
        [float(record["output_resistance"]) for record in low_error]
    )
    five_percent_distribution = _distribution(
        [float(record["output_resistance"]) for record in five_percent]
    )
    if len(low_error) >= 20:
        recommendation = (
            "Focused profiling sufficient for first conditional Rout analysis."
        )
    elif len(low_error) >= 10:
        recommendation = (
            "Borderline; conditional Rout range can be inspected, but another "
            "refinement round may be useful."
        )
    else:
        recommendation = (
            "Insufficient low-error coverage. Recommend second focused-refinement round."
        )

    representative = {
        "lowest_error": (
            _compact_record(min(functional_valid, key=lambda item: float(item["current_error"])))
            if functional_valid
            else None
        ),
        "highest_rout_under_error_threshold": (
            _compact_record(
                max(low_error, key=lambda item: float(item["output_resistance"]))
            )
            if low_error
            else None
        ),
        "balanced_pareto_points": [
            _compact_record(record) for record in representative_pareto
        ],
    }
    return {
        "total_samples": total,
        "simulation_valid_samples": len(simulation_valid),
        "simulation_valid_rate": float(len(simulation_valid) / total),
        "functional_valid_samples": len(functional_valid),
        "functional_valid_rate": float(len(functional_valid) / total),
        "functional_valid_rate_given_simulation_valid": (
            float(len(functional_valid) / len(simulation_valid))
            if simulation_valid
            else 0.0
        ),
        "low_error_hit_rates": hits,
        "region_comparison": region_rows,
        "conditional_rout": {
            "error_threshold": float(error_threshold),
            "distribution": low_error_distribution,
        },
        "five_percent_error_rout": {
            "count": len(five_percent),
            "sufficient_support": len(five_percent) >= 10,
            "distribution": five_percent_distribution,
        },
        "nominal_reference": dict(nominal_record),
        "nominal_improvement": {
            "error_le_threshold_and_rout_gt_nominal": len(low_error_better_rout),
            "error_le_nominal_and_rout_gt_nominal": len(nominal_error_better_rout),
        },
        "pareto_point_count": len(pareto),
        "representative_samples": representative,
        "low_error_normalized_delta_from_nominal": parameter_deltas,
        "reference_rout_candidates": (
            {
                "easy_p50": low_error_distribution["p50"],
                "medium_p75": low_error_distribution["p75"],
                "hard_p90": low_error_distribution["p90"],
            }
            if low_error_distribution is not None
            else None
        ),
        "recommendation": recommendation,
    }


def _write_focused_csv(
    path: Path,
    records: Sequence[Mapping[str, Any]],
    parameter_names: Sequence[str],
) -> None:
    fieldnames = [
        "sample_id",
        "anchor_id",
        "anchor_source",
        "anchor_source_sample_id",
        "radius",
        "local_seed",
        "local_sample_index",
        "distance_l2",
        "sampler_version",
        "parameter_valid",
        "simulation_valid",
        "functional_valid",
        "functional_failure_reasons",
        "current_error",
        "output_resistance",
        "compliance_relative_variation",
        "tracking_max_error",
        "failure_reason",
        *(f"param_{name}" for name in parameter_names),
        *(f"normalized_{name}" for name in parameter_names),
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {name: record.get(name) for name in fieldnames}
            row["functional_failure_reasons"] = ";".join(
                record.get("functional_failure_reasons") or []
            )
            for name in parameter_names:
                row[f"param_{name}"] = record["params"].get(name)
                row[f"normalized_{name}"] = record["normalized_params"].get(name)
            writer.writerow(row)


def save_focused_artifacts(
    runtime: Any,
    records: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Any],
    *,
    source_summary: Mapping[str, Any],
    source_csv_path: Path,
    source_summary_path: Path,
    anchors: Sequence[Mapping[str, Any]],
    anchor_distances: Sequence[Mapping[str, Any]],
    radii: Sequence[float],
    samples_per_region: int,
    seed: int,
    generation: Mapping[str, Any],
    error_threshold: float,
    output_root: str | Path = _DEFAULT_OUTPUT_ROOT,
    diagnostic_only: bool = False,
) -> tuple[Path, Path]:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    stem = f"{runtime.circuit_id}_focused_{run_id}"
    csv_path = root / f"{stem}.csv"
    summary_path = root / f"{stem}_summary.json"
    space = _parameter_space(runtime)
    parameter_names = [str(parameter["name"]) for parameter in space]
    _write_focused_csv(csv_path, records, parameter_names)
    payload = {
        "schema_version": "low_error_focused_profiling_v2",
        "run_id": run_id,
        "circuit_id": runtime.circuit_id,
        "circuit_type": runtime.circuit_type,
        "parameter_count": len(space),
        "source_global_profile": {
            "run_id": source_summary.get("run_id"),
            "csv": str(source_csv_path),
            "summary": str(source_summary_path),
            "samples": source_summary.get("requested_samples"),
            "sampler": source_summary.get("sampler"),
            "seed": source_summary.get("seed"),
            "simulation_valid": source_summary.get("simulation_valid_samples"),
            "functional_valid": source_summary.get("functional_valid_samples"),
        },
        "sampler": "normalized_gaussian_uniform_radius",
        **SAMPLER_METADATA,
        "dimension": len(space),
        "seed": int(seed),
        "radii": [float(radius) for radius in radii],
        "samples_per_region": int(samples_per_region),
        "error_threshold": float(error_threshold),
        "parameter_space": space,
        "parameter_transforms": [
            {
                "name": parameter["name"],
                "scale": parameter.get("scale"),
                "transform": parameter.get("transform"),
                "fixed_sign": parameter.get("fixed_sign"),
            }
            for parameter in space
        ],
        "anchors": [
            {
                **dict(anchor),
                "anchor_source": anchor["source"],
                "anchor_parameter_vector": dict(anchor["params"]),
                "anchor_metrics": {
                    "current_error": anchor.get("current_error"),
                    "output_resistance": anchor.get("output_resistance"),
                },
            }
            for anchor in anchors
        ],
        "anchor_pairwise_distances": [dict(item) for item in anchor_distances],
        "generation": dict(generation),
        "diagnostic_only": bool(diagnostic_only),
        **dict(analysis),
        "data_isolation": {
            "purpose": "benchmark_construction_and_performance_space_analysis_only",
            "included_in_d0": False,
            "included_in_dt": False,
            "included_in_retrieval": False,
            "included_in_llm_context": False,
        },
        "artifacts": {"csv": str(csv_path.resolve())},
    }
    summary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return csv_path.resolve(), summary_path.resolve()


def _format_resistance(value: float) -> str:
    if abs(value) >= 1.0e6:
        return f"{value / 1.0e6:.6g} MOhm"
    if abs(value) >= 1.0e3:
        return f"{value / 1.0e3:.6g} kOhm"
    return f"{value:.6g} Ohm"


def _print_anchors(
    anchors: Sequence[Mapping[str, Any]],
    distances: Sequence[Mapping[str, Any]],
) -> None:
    print("Focused profiling anchors")
    for anchor in anchors:
        print(f"{anchor['anchor_id']}:")
        print(f"  source: {anchor['source']}")
        print(f"  sample_id: {anchor['sample_id']}")
        print(f"  current_error: {anchor['current_error']:.12g}")
        print(f"  Rout: {anchor['output_resistance']:.12g}")
    print("Pairwise normalized/transformed distances:")
    for item in distances:
        print(f"  {item['first']} <-> {item['second']}: {item['distance']:.12g}")


def _print_summary(summary: Mapping[str, Any], csv_path: Path, json_path: Path) -> None:
    print("Low-Error Focused SPICE Profiling")
    print(f"circuit_id: {summary['circuit_id']}")
    print(f"total: {summary['total_samples']}")
    print(f"simulation_valid: {summary['simulation_valid_samples']}")
    print(f"functional_valid: {summary['functional_valid_samples']}")
    print("Low-error hit rates:")
    for item in summary["low_error_hit_rates"].values():
        print(
            f"  error <= {item['threshold']:.0%}: {item['count']} "
            f"({item['rate_all_samples']:.2%} all; "
            f"{item['rate_functional_valid']:.2%} functional-valid)"
        )
    print("Region comparison:")
    for item in summary["region_comparison"]:
        print(
            f"  {item['anchor_id']} radius={item['radius']:.3g}: "
            f"functional={item['functional_valid']}/{item['samples']}, "
            f"error<={item['error_threshold']:.0%}: {item['low_error_count']}"
        )
    conditional = summary["conditional_rout"]["distribution"]
    print(f"Conditional Rout (error <= {summary['error_threshold']:.0%}):")
    if conditional is None:
        print("  unavailable: no qualifying samples")
    else:
        for name in ("min", "p25", "p50", "p75", "p90", "p95", "max"):
            print(f"  {name}: {conditional[name]:.12g} Ohm ({_format_resistance(conditional[name])})")
    print("Nominal improvement:")
    improvement = summary["nominal_improvement"]
    print(
        "  error<=threshold and Rout>nominal: "
        f"{improvement['error_le_threshold_and_rout_gt_nominal']}"
    )
    print(
        "  error<=nominal and Rout>nominal: "
        f"{improvement['error_le_nominal_and_rout_gt_nominal']}"
    )
    print(f"Pareto points: {summary['pareto_point_count']}")
    print(f"Recommendation: {summary['recommendation']}")
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")


def main(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Run low-error focused Real-SPICE profiling"
    )
    parser.add_argument("--circuit-type", required=True)
    parser.add_argument("--circuit-id", required=True)
    parser.add_argument("--error-threshold", type=float, default=0.10)
    parser.add_argument("--samples-per-region", type=int, default=50)
    parser.add_argument(
        "--radii", nargs="+", type=float, required=True,
        help="Required V2 maximum full-vector log-aware normalized L2 distances; choose explicitly because legacy 0.05/0.10 had different semantics",
    )
    parser.add_argument("--seed", type=int, default=43)
    parser.add_argument("--source-summary")
    parser.add_argument(
        "--anchor-ids", nargs="+",
        help="OpAmp anchors: nominal and/or exact sample IDs from --source-summary; omitted selects nominal plus deterministic high-Aol/high-UGF global anchors",
    )
    parser.add_argument(
        "--anchor-json",
        help="OpAmp JSON list of generic explicit anchors with anchor_id and params or normalized_params",
    )
    parser.add_argument(
        "--diagnostic-only", action="store_true",
        help="Mark the output as sampler-validation data, not formal benchmark profiling",
    )
    parser.add_argument("--global-profile-dir", default=str(_DEFAULT_GLOBAL_ROOT))
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_ROOT))
    args = parser.parse_args(argv)
    if not 0.0 < args.error_threshold < 1.0:
        parser.error("--error-threshold must be in (0, 1)")

    runtime = build_active_learning_circuit_config(
        args.circuit_type,
        args.circuit_id,
        repository_root=_REPOSITORY_ROOT,
    )
    focused_mode = resolve_focused_profiling_mode(runtime.circuit_type)
    if focused_mode == "opamp_local_regions_v1":
        from ActiveLearning.opamp_focused_profiling import run_opamp_focused_cli

        return run_opamp_focused_cli(runtime, args)
    source_summary, global_records, source_csv, source_json = (
        load_latest_global_profile(
            runtime,
            profiling_root=args.global_profile_dir,
        )
    )
    anchors, distances = select_anchors(
        runtime,
        global_records,
        source_summary["nominal_reference"],
        error_threshold=args.error_threshold,
    )
    _print_anchors(anchors, distances)
    samples, generation = generate_focused_samples(
        runtime,
        anchors,
        radii=args.radii,
        samples_per_region=args.samples_per_region,
        seed=args.seed,
        existing_global_records=global_records,
    )
    expected_total = len(anchors) * len(args.radii) * args.samples_per_region
    if len(samples) != expected_total:
        raise RuntimeError(
            f"Focused unique sample count mismatch: {len(samples)} != {expected_total}"
        )
    print(
        "Focused samples: "
        f"generated={generation['generated']}, "
        f"duplicates_removed={generation['duplicates_removed']}, "
        f"final_unique={generation['final_unique_samples']}"
    )
    report_every = max(1, expected_total // 20)

    def progress(completed: int, total: int) -> None:
        if completed == total or completed % report_every == 0:
            print(f"focused profiling progress: {completed}/{total}", flush=True)

    records = run_focused_spice_profile(runtime, samples, progress=progress)
    nominal = dict(source_summary["nominal_reference"])
    nominal["params"] = dict(anchors[0]["params"])
    nominal["normalized_params"] = dict(anchors[0]["normalized_params"])
    analysis = analyze_focused_profile(
        records,
        nominal,
        _parameter_space(runtime),
        error_threshold=args.error_threshold,
    )
    csv_path, json_path = save_focused_artifacts(
        runtime,
        records,
        analysis,
        source_summary=source_summary,
        source_csv_path=source_csv,
        source_summary_path=source_json,
        anchors=anchors,
        anchor_distances=distances,
        radii=args.radii,
        samples_per_region=args.samples_per_region,
        seed=args.seed,
        generation=generation,
        error_threshold=args.error_threshold,
        output_root=args.output_dir,
        diagnostic_only=bool(args.diagnostic_only),
    )
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    _print_summary(summary, csv_path, json_path)
    return summary


if __name__ == "__main__":
    main()
