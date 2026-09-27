"""In-memory SPICE benchmark calibration for circuit_0469.

This module is deliberately separate from the Active Learning loop.  It writes
no samples or reports; all calibration results are printed to the terminal.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import yaml

from ActiveLearning.InitializeD0.sampling.initial_sampler import (
    generate_v3_physical_local_global_samples,
)


_ROOT = Path(__file__).resolve().parent
_CIRCUIT_DIR = (
    _ROOT / "InitializeD0" / "examples" / "CurrentMirror" / "circuit_0469"
)
_BASE_ERROR_TARGETS = (0.005, 0.0075, 0.010, 0.0125, 0.015)
_BASE_ROUT_TARGETS = (20e6, 22.5e6, 25e6, 27.5e6, 30e6, 35e6, 40e6)


def dominates(left, right):
    """Return whether left Pareto-dominates right for error-min/Rout-max."""
    no_worse = (
        left["current_error"] <= right["current_error"]
        and left["output_resistance"] >= right["output_resistance"]
    )
    strictly_better = (
        left["current_error"] < right["current_error"]
        or left["output_resistance"] > right["output_resistance"]
    )
    return no_worse and strictly_better


def pareto_front(records):
    """Return all nondominated records, ordered by increasing current error."""
    front = [
        record
        for index, record in enumerate(records)
        if not any(
            dominates(other, record)
            for other_index, other in enumerate(records)
            if other_index != index
        )
    ]
    return sorted(
        front,
        key=lambda record: (
            record["current_error"],
            -record["output_resistance"],
        ),
    )


def feasible_summary(records, error_target, rout_target):
    """Count joint and individual target feasibility within valid records."""
    total = len(records)
    error_count = sum(
        record["current_error"] <= error_target for record in records
    )
    rout_count = sum(
        record["output_resistance"] >= rout_target for record in records
    )
    joint_count = sum(
        record["current_error"] <= error_target
        and record["output_resistance"] >= rout_target
        for record in records
    )
    divisor = total or 1
    return {
        "error_target": float(error_target),
        "rout_target": float(rout_target),
        "count": joint_count,
        "rate": joint_count / divisor,
        "error_only_count": error_count,
        "error_only_rate": error_count / divisor,
        "rout_only_count": rout_count,
        "rout_only_rate": rout_count / divisor,
    }


def scan_targets(records, d0_records, error_targets, rout_targets):
    """Evaluate a Cartesian target grid against calibration and D0 records."""
    scanned = []
    for error_target in sorted(set(float(value) for value in error_targets)):
        for rout_target in sorted(set(float(value) for value in rout_targets)):
            item = feasible_summary(records, error_target, rout_target)
            item["d0_v3_feasible_count"] = feasible_summary(
                d0_records,
                error_target,
                rout_target,
            )["count"]
            scanned.append(item)
    return scanned


def normalized_joint_violation(record, error_target, rout_target):
    """Use the existing AL normalized mean-violation convention."""
    error_violation = max(
        0.0,
        (record["current_error"] - error_target) / abs(error_target),
    )
    rout_violation = max(
        0.0,
        (rout_target - record["output_resistance"]) / abs(rout_target),
    )
    return (error_violation + rout_violation) / 2.0


def select_recommendations(scanned, limit=3):
    """Select easy/medium/hard empirical candidates near 8%, 5%, and 3%."""
    benchmark_candidates = [
        item
        for item in scanned
        if item["d0_v3_feasible_count"] == 0
        and item["count"] > 0
        and item["error_target"] <= 0.05
        and item["rout_target"] >= 25e6
    ]
    eligible = [
        item
        for item in benchmark_candidates
        if 0.01 <= item["rate"] <= 0.10
    ]
    if not eligible:
        eligible = benchmark_candidates

    selected = []
    used_counts = set()
    for desired_rate in (0.08, 0.05, 0.03):
        choices = [item for item in eligible if item["count"] not in used_counts]
        if not choices:
            choices = [item for item in eligible if item not in selected]
        if not choices:
            break
        chosen = min(
            choices,
            key=lambda item: (
                abs(item["rate"] - desired_rate),
                max(0.0, item["error_only_rate"] - 0.95)
                + max(0.0, item["rout_only_rate"] - 0.95),
                abs(item["error_only_rate"] - item["rout_only_rate"]),
            ),
        )
        selected.append(chosen)
        used_counts.add(chosen["count"])

    return sorted(selected[:limit], key=lambda item: item["rate"], reverse=True)


def _load_yaml(path):
    with Path(path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _load_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _valid_d0_records(records):
    return [
        {
            "sample_id": record["sample_id"],
            "params": record["params"],
            "current_error": float(record["metrics"]["current_error"]),
            "output_resistance": float(record["metrics"]["output_resistance"]),
        }
        for record in records
        if record.get("status", {}).get("simulation_valid") is True
    ]


def _metric_distribution(records, name):
    values = np.asarray([record[name] for record in records], dtype=float)
    percentiles = np.percentile(values, [10, 25, 50, 75, 90])
    return {
        "min": float(np.min(values)),
        "p10": float(percentiles[0]),
        "p25": float(percentiles[1]),
        "median": float(percentiles[2]),
        "p75": float(percentiles[3]),
        "p90": float(percentiles[4]),
        "max": float(np.max(values)),
    }


def _representatives(front, limit=20):
    if len(front) <= limit:
        return front
    indices = np.linspace(0, len(front) - 1, limit).round().astype(int)
    return [front[index] for index in dict.fromkeys(indices)]


def _data_driven_thresholds(valid_records, front):
    errors = np.asarray([record["current_error"] for record in valid_records])
    routs = np.asarray([record["output_resistance"] for record in valid_records])
    front_errors = np.asarray([record["current_error"] for record in front])
    front_routs = np.asarray([record["output_resistance"] for record in front])

    error_targets = set(_BASE_ERROR_TARGETS)
    rout_targets = set(_BASE_ROUT_TARGETS)
    error_targets.update((0.02, 0.025, 0.03, 0.04, 0.05))
    rout_targets.update((26e6, 28e6, 32e6))
    for quantile in (0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40):
        error_targets.add(float(np.quantile(errors, quantile)))
        rout_targets.add(float(np.quantile(routs, 1.0 - quantile)))
    if len(front):
        for quantile in (0.20, 0.40, 0.60, 0.80):
            error_targets.add(float(np.quantile(front_errors, quantile)))
            rout_targets.add(float(np.quantile(front_routs, quantile)))
    return error_targets, rout_targets


def _print_record(record):
    params = record["params"]
    print(
        f"source={record.get('source', 'd0')} "
        f"IB1={params['IB1']:.12g} R0={params['R0']:.12g} "
        f"R1={params['R1']:.12g} current_error={record['current_error']:.12g} "
        f"output_resistance={record['output_resistance']:.12g}"
    )


def run_calibration(n_local=700, n_global=300, local_factor=2.0, seed=42):
    """Run the requested real-SPICE calibration entirely in memory."""
    from ActiveLearning.InitializeD0.evaluator.current_mirror_evaluator import (
        evaluate_current_mirror,
    )
    from ActiveLearning.InitializeD0.examples.CurrentMirror.circuit_0469.evaluator_adapter import (
        CIRCUIT_0469_ADAPTER,
    )

    run_start = time.perf_counter()
    parameter_template = _load_yaml(_CIRCUIT_DIR / "parameter_template.yaml")
    d0_records = _valid_d0_records(
        _load_jsonl(_CIRCUIT_DIR / "d0" / "d0_v3_seed42.jsonl")
    )
    generated = generate_v3_physical_local_global_samples(
        parameter_template,
        n_local=n_local,
        n_global=n_global,
        local_factor=local_factor,
        seed=seed,
    )[1:]

    valid_records = []
    failure_count = 0
    source_seconds = {"local": 0.0, "global": 0.0}
    evaluation_start = time.perf_counter()
    for sample in generated:
        point_start = time.perf_counter()
        try:
            current_error, output_resistance, simulation_valid = (
                evaluate_current_mirror(sample["params"], CIRCUIT_0469_ADAPTER)
            )
        except Exception:
            current_error, output_resistance, simulation_valid = None, None, False
        source_seconds[sample["sampling_source"]] += (
            time.perf_counter() - point_start
        )
        if simulation_valid is not True:
            failure_count += 1
            continue
        valid_records.append(
            {
                "source": sample["sampling_source"],
                "params": sample["params"],
                "current_error": float(current_error),
                "output_resistance": float(output_resistance),
            }
        )
    evaluation_seconds = time.perf_counter() - evaluation_start

    front = pareto_front(valid_records)
    error_targets, rout_targets = _data_driven_thresholds(valid_records, front)
    expanded_scan = scan_targets(
        valid_records,
        d0_records,
        error_targets,
        rout_targets,
    )
    recommendations = select_recommendations(expanded_scan)

    print("=== Calibration Summary ===")
    print(f"total_samples: {len(generated)}")
    print(f"local_samples: {sum(s['sampling_source'] == 'local' for s in generated)}")
    print(f"global_samples: {sum(s['sampling_source'] == 'global' for s in generated)}")
    print(f"simulation_valid_count: {len(valid_records)}")
    print(f"simulation_failure_count: {failure_count}")

    print("\n=== Metric Distribution ===")
    for metric in ("current_error", "output_resistance"):
        print(f"{metric}:")
        for name, value in _metric_distribution(valid_records, metric).items():
            print(f"  {name}: {value:.12g}")

    print("\n=== Single-Metric Extremes ===")
    print("minimum current_error (not necessarily a joint optimum):")
    _print_record(min(valid_records, key=lambda record: record["current_error"]))
    print("maximum output_resistance (not necessarily a joint optimum):")
    _print_record(max(valid_records, key=lambda record: record["output_resistance"]))

    print("\n=== Pareto Front Representatives ===")
    print(f"pareto_count: {len(front)}")
    for rank, record in enumerate(_representatives(front), start=1):
        params = record["params"]
        print(
            f"{rank:02d} {record['source']} {params['IB1']:.12g} "
            f"{params['R0']:.12g} {params['R1']:.12g} "
            f"{record['current_error']:.12g} "
            f"{record['output_resistance']:.12g}"
        )

    print("\n=== Target Candidate Scan ===")
    base_scan = scan_targets(
        valid_records,
        d0_records,
        _BASE_ERROR_TARGETS,
        _BASE_ROUT_TARGETS,
    )
    print("error_target rout_target calibration_count calibration_rate d0_v3_count")
    for item in base_scan:
        print(
            f"{item['error_target']:.12g} {item['rout_target']:.12g} "
            f"{item['count']} {item['rate']:.6%} "
            f"{item['d0_v3_feasible_count']}"
        )

    print("\n=== Recommended Benchmark Targets ===")
    if not recommendations:
        print("No candidate has D0 count 0 and calibration count > 0.")
    preferred_exists = any(
        item["d0_v3_feasible_count"] == 0
        and item["count"] > 0
        and item["error_target"] <= 0.05
        and item["rout_target"] >= 25e6
        and 0.03 <= item["rate"] <= 0.08
        for item in expanded_scan
    )
    if not preferred_exists:
        print(
            "note: no data-supported candidate in the preferred 3%-8% rate "
            "band with current_error <= 0.05 and Rout >= 25e6; reporting "
            "the closest reachable sparse candidates."
        )
    labels = ("easy", "medium", "hard")
    for label, item in zip(labels, recommendations):
        print(
            f"{label}: current_error <= {item['error_target']:.12g}, "
            f"output_resistance >= {item['rout_target']:.12g}"
        )
        print(
            f"  calibration_feasible_count={item['count']} "
            f"calibration_feasible_rate={item['rate']:.6%} "
            f"d0_v3_feasible_count={item['d0_v3_feasible_count']}"
        )
        print(
            f"  current_error_only_rate={item['error_only_rate']:.6%} "
            f"rout_only_rate={item['rout_only_rate']:.6%} "
            f"joint_rate={item['rate']:.6%}"
        )
        best_d0 = min(
            d0_records,
            key=lambda record: normalized_joint_violation(
                record,
                item["error_target"],
                item["rout_target"],
            ),
        )
        violation = normalized_joint_violation(
            best_d0,
            item["error_target"],
            item["rout_target"],
        )
        print(
            f"  d0_best_sample_id={best_d0['sample_id']} "
            f"minimum_joint_violation={violation:.12g}"
        )
        _print_record(best_d0)

    total_seconds = time.perf_counter() - run_start
    print("\n=== Runtime ===")
    print(f"total_calibration_wall_seconds: {total_seconds:.6f}")
    print(f"average_spice_seconds_per_sample: {evaluation_seconds / len(generated):.9f}")
    print(f"local_evaluation_seconds: {source_seconds['local']:.6f}")
    print(f"global_evaluation_seconds: {source_seconds['global']:.6f}")

    return {
        "total_samples": len(generated),
        "valid_count": len(valid_records),
        "failure_count": failure_count,
        "pareto_count": len(front),
        "recommendations": recommendations,
        "total_seconds": total_seconds,
    }


def main():
    run_calibration()


if __name__ == "__main__":
    main()
