"""Offline Batch046 SampleHold Phase A contract tests; no native execution."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest
import yaml

from ActiveLearning.main import run_configured_active_learning
from ActiveLearning.onboarding import AutoConfigError, build_active_learning_circuit_config
from ActiveLearning.onboarding.sample_hold import (
    SampleHoldEvaluatorAdapter, SampleHoldFunctionalValidator,
)
from ActiveLearning.onboarding.sample_hold_phase_a import validate_pilot_static


ROOT = Path(__file__).resolve().parents[1]
PILOTS = ROOT / "ActiveLearning/experiments/SampleHold"
PERFORMANCE = yaml.safe_load(
    (ROOT / "ActiveLearning/circuit_types/SampleHold/performance_template.yaml")
    .read_text(encoding="utf-8")
)


@pytest.mark.parametrize("circuit_id,dimension", [("circuit_0259", 3), ("circuit_0263", 6)])
@pytest.mark.parametrize("filename,iterations", [
    ("one_iteration_pilot_v1.yaml", 1),
    ("five_iteration_validation_v1.yaml", 5),
])
def test_frozen_pilot_static_contract(circuit_id, dimension, filename, iterations):
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    assert len(runtime.param_template["optimization_space"]["variables"]) == dimension
    path = PILOTS / circuit_id / filename
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    d0 = ROOT / "ActiveLearning/InitializeD0/examples/SampleHold" / circuit_id / "d0/d0_v3_seed42.jsonl"
    status = validate_pilot_static(runtime, config, path, d0, PERFORMANCE)
    assert status == {"records": 20, "iterations": iterations,
                      "physical_attempts_max": iterations,
                      "native_execution_authorized": False}
    assert hashlib.sha256(d0.read_bytes()).hexdigest() == config["data"]["expected_d0_sha256"]
    assert config["active_learning"]["stop_on_meets_spec"] is False


def test_pilot_fails_closed_on_target_and_budget_changes():
    circuit_id = "circuit_0259"
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    path = PILOTS / circuit_id / "one_iteration_pilot_v1.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    d0 = ROOT / "ActiveLearning/InitializeD0/examples/SampleHold" / circuit_id / "d0/d0_v3_seed42.jsonl"
    bad_target = deepcopy(config)
    bad_target["target_specs"]["track_end_error_v"]["operator"] = ">="
    with pytest.raises(AutoConfigError, match="targets"):
        validate_pilot_static(runtime, bad_target, path, d0, PERFORMANCE)
    bad_budget = deepcopy(config)
    bad_budget["sample_hold_pilot"]["budget"]["physical_attempts_max"] = 2
    with pytest.raises(AutoConfigError, match="guard"):
        validate_pilot_static(runtime, bad_budget, path, d0, PERFORMANCE)


def test_0605_static_mapping_does_not_admit_formal_al():
    runtime = build_active_learning_circuit_config("SampleHold", "circuit_0605")
    assert len(runtime.param_template["optimization_space"]["variables"]) == 6
    config_path = PILOTS / "circuit_0259/one_iteration_pilot_v1.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    d0 = ROOT / "ActiveLearning/InitializeD0/examples/SampleHold/circuit_0259/d0/d0_v3_seed42.jsonl"
    with pytest.raises(AutoConfigError, match="0259/0263"):
        validate_pilot_static(runtime, config, config_path, d0, PERFORMANCE)


def test_executor_adapter_is_callable_only_when_explicitly_injected():
    runtime = build_active_learning_circuit_config("SampleHold", "circuit_0259")
    params = {p["name"]: p["nominal_value"] for p in runtime.param_template["parameters"]}
    with pytest.raises(AutoConfigError, match="not authorized"):
        SampleHoldEvaluatorAdapter(runtime).evaluate(params)
    calls = []
    adapter = SampleHoldEvaluatorAdapter(runtime, executor=lambda values: calls.append(values) or "ok")
    assert adapter.evaluate(params) == "ok"
    assert len(calls) == 1
    assert set(calls[0]) == set(runtime.candidate_builder.expand_candidate(params))


def test_functional_and_numeric_states_remain_independent():
    metrics = {"track_end_error_v": 1e-11, "sampling_error_v": 1e-10,
               "hold_isolation_error_v": 1e-15, "hold_droop_v_per_s": 0.1}
    targets = {name: {"operator": "<=", "value": 1e-8}
               for name in ("track_end_error_v", "sampling_error_v",
                            "hold_isolation_error_v")}
    validator = SampleHoldFunctionalValidator()
    base = {"metrics": metrics, "parameter_valid": True,
            "simulation_valid": True, "measurement_valid": True,
            "functional_valid": True, "failure_reasons": [], "diagnostics": {}}
    good = validator.validate({}, base, target_specs=targets)["status"]
    assert all(good[key] is True for key in (
        "parameter_valid", "simulation_valid", "measurement_valid",
        "functional_valid", "meets_spec"))
    invalid = validator.validate({}, {**base, "functional_valid": False},
                                 target_specs=targets)["status"]
    assert invalid["parameter_valid"] is True
    assert invalid["simulation_valid"] is True
    assert invalid["functional_valid"] is False
    assert invalid["meets_spec"] is None


def test_main_requires_linux_runtime_preflight_before_native_execution():
    path = PILOTS / "circuit_0259/one_iteration_pilot_v1.yaml"
    with pytest.raises(
            AutoConfigError,
            match=r"requires Ubuntu/Linux"
    ):
        run_configured_active_learning(
            "SampleHold", "circuit_0259", config_path=path,
            llm_client=lambda _: pytest.fail("LLM must not be called"),
        )
