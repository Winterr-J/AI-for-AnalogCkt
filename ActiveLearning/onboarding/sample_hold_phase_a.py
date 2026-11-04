"""Static-only SampleHold mapping check for the Batch046 migration.

The Windows native closure is deliberately *not* approved by this check.
The original physical ``load_candidate`` remains strict and unmodified.
"""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path

from .errors import AutoConfigError

from ActiveLearning.sc_sample_hold_generic import (
    ROOT, SCHEMA, CircuitMapping, GenericCanaryError, OperatingConditions,
    _asset, _topology_contract, build_fixture, build_stimulus,
    canonical_sha256, sha256_file,
)


_FROZEN_TARGETS = {
    "circuit_0259": (4.873809504128227e-10, 2.023536294082362e-8,
                     4.2558549277297665e-14),
    "circuit_0263": (1.3470979887131307e-10, 3.3800628407298916e-9,
                     1.3516965324811281e-14),
}
_TARGET_NAMES = ("track_end_error_v", "sampling_error_v",
                 "hold_isolation_error_v")


def verify_static_candidate(path: Path) -> dict:
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(ROOT):
        raise GenericCanaryError("SampleHold static mapping path escaped repository")
    config = json.loads(path.read_text(encoding="utf-8"))
    if (config.get("schema") != SCHEMA
            or config.get("status") != "candidate_unapproved"
            or config.get("real_execution_authorized") is not False
            or config.get("operator_fixture_approved") is not False):
        raise GenericCanaryError("SampleHold fixture is not a static unapproved candidate")
    assets = config["source_assets"]
    if set(assets) != {
        "topology", "converted", "ideal_parameters", "fixture_resolution",
        "generic_worker", "native_child", "evaluator", "job_isolation",
        "legacy_guarded_runner", "ngspice_configuration",
    }:
        raise GenericCanaryError("SampleHold source asset set changed")
    paths = {name: _asset(asset) for name, asset in assets.items()
             if name != "ngspice_configuration"}
    ngspice_path = (ROOT / assets["ngspice_configuration"]["path"]).resolve(strict=True)
    if not ngspice_path.is_relative_to(ROOT):
        raise GenericCanaryError("SampleHold ngspice configuration path escaped repository")
    # Deliberately retain the mismatch as evidence; do not accept it as a
    # physical source closure or silently substitute Windows runner bytes.
    native_config_matches = (
        sha256_file(ngspice_path) == assets["ngspice_configuration"]["sha256"]
    )
    _topology_contract(config, paths)
    mapping, conditions = config["mapping"], config["conditions"]
    fixture = build_fixture(
        CircuitMapping(config["circuit_id"], tuple(mapping["inputs"]),
                       tuple(mapping["outputs"]), mapping["clock"],
                       mapping["track_clock_level"], assets["topology"]["sha256"],
                       tuple(mapping["diagnostic_nodes"])),
        OperatingConditions(
            tuple(conditions["vin_range_v"]), conditions["clock_period_s"],
            conditions["clock_low_v"], conditions["clock_high_v"],
            conditions["clock_duty"], conditions["clock_rise_s"],
            conditions["clock_fall_s"], conditions["provenance"],
            fixed_common_mode_v=conditions["fixed_common_mode_v"],
            vin_edge_s=conditions["vin_edge_s"],
        ),
        startup_cycles=config["startup_cycles"], measurement_cycles=6,
        guard_time_s=config["guard_time_s"],
    )
    if fixture.fixture_hash != config["fixture_sha256"]:
        raise GenericCanaryError("SampleHold static fixture hash mismatch")
    stimulus = build_stimulus(
        fixture, startup_input_v=config["startup_input_v"],
        physical_input_bounds_v=config["physical_input_bounds_v"],
    )
    if canonical_sha256(stimulus) != config["stimulus_sha256"]:
        raise GenericCanaryError("SampleHold static stimulus hash mismatch")
    return {"fixture": fixture, "native_source_closure_verified": native_config_matches}


def validate_pilot_static(runtime, config: dict, config_path: Path,
                          d0_path: Path, performance_template: dict) -> dict:
    """Validate old 0259/0263 Pilot inputs without authorizing Windows execution."""
    if runtime.circuit_id not in {"circuit_0259", "circuit_0263"}:
        raise AutoConfigError("SampleHold Phase A AL allows only 0259/0263")
    approved_dir = ROOT / "ActiveLearning/experiments/SampleHold" / runtime.circuit_id
    if Path(config_path).resolve() not in {
        (approved_dir / "one_iteration_pilot_v1.yaml").resolve(),
        (approved_dir / "five_iteration_validation_v1.yaml").resolve(),
    }:
        raise AutoConfigError("SampleHold pilot requires an existing reviewed config path")
    target_specs = config.get("target_specs")
    metrics = performance_template["performance_template"]["active_metrics"]
    if (config.get("circuit_info") != {"circuit_type": "SampleHold",
                                       "circuit_id": runtime.circuit_id}
            or config.get("target_status") != "experimental_selected"
            or config.get("performance_target_selected") is not True
            or not isinstance(target_specs, dict)
            or set(target_specs) != set(metrics)
            or any(not isinstance(spec, dict) or spec.get("operator") != "<="
                   or type(spec.get("value")) not in (int, float)
                   or not math.isfinite(spec["value"]) or spec["value"] <= 0
                   for spec in target_specs.values())
            or tuple(target_specs[name]["value"] for name in _TARGET_NAMES)
               != _FROZEN_TARGETS[runtime.circuit_id]):
        raise AutoConfigError("SampleHold Pilot targets must be explicit finite <= constraints")
    data = config.get("data") or {}
    if (data.get("d0_mode") != "reuse"
            or data.get("initialize_if_missing") is not False
            or data.get("d0_path_relative_to") != "circuit"
            or data.get("d0_path") != "d0/d0_v3_seed42.jsonl"
            or not d0_path.is_file()
            or not isinstance(data.get("expected_d0_sha256"), str)
            or hashlib.sha256(d0_path.read_bytes()).hexdigest() != data["expected_d0_sha256"]):
        raise AutoConfigError("SampleHold Pilot frozen D0 identity mismatch")
    rows = [json.loads(line) for line in d0_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if len(rows) != 20 or len({row.get("sample_id") for row in rows}) != 20:
        raise AutoConfigError("SampleHold Pilot requires 20 unique physical D0 rows")
    settings = config.get("active_learning") or {}
    count = settings.get("max_iterations")
    pilot = config.get("sample_hold_pilot") or {}
    budget = pilot.get("budget") or {}
    if (type(count) is not int or count not in {1, 5}
            or settings.get("stop_on_meets_spec") is not False
            or settings.get("prediction_prompt_version") != "compact_v1"
            or pilot.get("enabled") is not True
            or type(budget.get("physical_attempts_max")) is not int
            or budget["physical_attempts_max"] != count
            or budget.get("automatic_retries") != 0
            or type(budget.get("single_attempt_timeout_s")) not in (int, float)
            or type(budget.get("overall_timeout_s")) not in (int, float)
            or budget["single_attempt_timeout_s"] <= 0
            or budget["overall_timeout_s"] < budget["single_attempt_timeout_s"]):
        raise AutoConfigError("SampleHold Pilot iteration, prompt or physical guard changed")
    return {"records": len(rows), "iterations": count,
            "physical_attempts_max": budget["physical_attempts_max"],
            "native_execution_authorized": False}
