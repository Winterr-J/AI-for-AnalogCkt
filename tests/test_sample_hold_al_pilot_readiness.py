"""Offline readiness tests for the bounded authentic SampleHold AL path."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from ActiveLearning import main as al_main
from ActiveLearning.active_learning import run_active_learning
from ActiveLearning.main import run_configured_active_learning
from ActiveLearning.onboarding import AutoConfigError, build_active_learning_circuit_config
from ActiveLearning.onboarding import sample_hold_batch as batch
from ActiveLearning.onboarding.sample_hold_phase_a import validate_pilot_static


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "ActiveLearning" / "InitializeD0" / "examples" / "SampleHold"
PILOTS = ROOT / "ActiveLearning" / "experiments" / "SampleHold"
METRICS = (
    "track_end_error_v",
    "sampling_error_v",
    "hold_isolation_error_v",
)
IDS = ("circuit_0259", "circuit_0263")


def _records(circuit_id):
    path = EXAMPLES / circuit_id / "d0" / "d0_v3_seed42.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _pilot_config(circuit_id):
    return yaml.safe_load(
        (PILOTS / circuit_id / "one_iteration_pilot_v1.yaml").read_text(
            encoding="utf-8"
        )
    )


def _five_iteration_config(circuit_id):
    return yaml.safe_load(
        (PILOTS / circuit_id / "five_iteration_validation_v1.yaml").read_text(
            encoding="utf-8"
        )
    )


def _nominal_targets(records):
    nominal = next(record for record in records if record["sampling_source"] == "nominal")
    return {
        name: {"operator": "<=", "value": nominal["metrics"][name]}
        for name in METRICS
    }


def _evaluation():
    return {
        "metrics": {
            "track_end_error_v": 1.0e-10,
            "sampling_error_v": 3.0e-9,
            "hold_isolation_error_v": 2.0e-14,
            "hold_droop_v_per_s": 0.2,
        },
        "parameter_valid": True,
        "simulation_valid": True,
        "measurement_valid": True,
        "functional_valid": True,
        "failure_reasons": [],
        "diagnostics": {"offline_mock_native": True},
    }


def _executor(tmp_path, circuit_id, launcher, *, physical_attempts_max=1):
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    config_path = tmp_path / f"{circuit_id}.yaml"
    config_path.write_text(yaml.safe_dump({
        "circuit_info": {"circuit_type": "SampleHold", "circuit_id": circuit_id},
        "data": {},
        "sample_hold_pilot": {
            "enabled": True,
            "execution_id": f"sc{circuit_id[-4:]}_al_pilot_test",
            "output_root": str((tmp_path / "physical").resolve()),
            "budget": {
                "physical_attempts_max": physical_attempts_max,
                "single_attempt_timeout_s": 0.25,
                "overall_timeout_s": 5.0,
                "automatic_retries": 0,
            },
        },
    }), encoding="utf-8")
    pilot = yaml.safe_load(config_path.read_text(encoding="utf-8"))["sample_hold_pilot"]
    return runtime, batch.SampleHoldALExecutor(
        runtime, config_path, pilot, launcher=launcher,
    )


def _mock_plan():
    return {
        "file_sha256": "f" * 64,
        "fixture": SimpleNamespace(fixture_hash="e" * 64),
        "config": {"stimulus_sha256": "d" * 64},
        "closure": {},
    }


@pytest.mark.parametrize("circuit_id", IDS)
def test_missing_s0_fails_closed_without_creating_native_state(tmp_path, circuit_id):
    config = _pilot_config(circuit_id)
    config["target_status"] = "operator_decision_required"
    config["performance_target_selected"] = False
    assert config["performance_target_selected"] is False
    config["target_specs"] = {
        name: {"operator": "<=", "value": None} for name in METRICS
    }
    config_path = tmp_path / f"{circuit_id}.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="target_specs values must be configured"):
        run_configured_active_learning(
            "SampleHold", circuit_id,
            config_path=config_path,
            llm_client=lambda _prompt: pytest.fail("LLM must remain unreachable"),
        )


@pytest.mark.parametrize("circuit_id", IDS)
def test_one_and_five_iteration_configs_preserve_targets_and_isolate_outputs(circuit_id):
    pilot = _pilot_config(circuit_id)
    validation = _five_iteration_config(circuit_id)
    assert validation["target_specs"] == pilot["target_specs"]
    assert validation["data"] == pilot["data"]
    assert pilot["active_learning"] == {
        "max_iterations": 1,
        "stop_on_meets_spec": False,
        "prediction_prompt_version": "compact_v1",
    }
    assert validation["active_learning"] == {
        "max_iterations": 5,
        "stop_on_meets_spec": False,
        "prediction_prompt_version": "compact_v1",
    }
    assert pilot["sample_hold_pilot"]["budget"]["physical_attempts_max"] == 1
    assert validation["sample_hold_pilot"]["budget"]["physical_attempts_max"] == 5
    assert (validation["sample_hold_pilot"]["execution_id"]
            != pilot["sample_hold_pilot"]["execution_id"])
    assert validation["output"]["runs_root"] != pilot["output"]["runs_root"]


@pytest.mark.parametrize("circuit_id", IDS)
@pytest.mark.parametrize(
    "config_name, expected_iterations",
    (("one_iteration_pilot_v1.yaml", 1), ("five_iteration_validation_v1.yaml", 5)),
)
def test_one_and_five_iteration_configs_pass_read_only_runtime_validation(
        monkeypatch, circuit_id, config_name, expected_iterations):
    monkeypatch.setattr(batch, "_frozen_plan", lambda _runtime: _mock_plan())
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    config_path = PILOTS / circuit_id / config_name
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    performance_template = yaml.safe_load(
        runtime.performance_template_path.read_text(encoding="utf-8")
    )
    status = validate_pilot_static(
        runtime, config, config_path,
        EXAMPLES / circuit_id / "d0" / "d0_v3_seed42.jsonl",
        performance_template,
    )
    assert status["physical_attempts_max"] == expected_iterations


@pytest.mark.parametrize("circuit_id", IDS)
@pytest.mark.parametrize("defect", ("missing_target", "prompt_version", "budget_mismatch"))
def test_five_iteration_config_rejects_invalid_contract_before_execution(
        tmp_path, circuit_id, defect):
    config = _five_iteration_config(circuit_id)
    if defect == "missing_target":
        config["target_specs"].pop("sampling_error_v")
        expected = "operator-selected positive <= targets"
    elif defect == "prompt_version":
        config["active_learning"]["prediction_prompt_version"] = "v1"
        expected = "prediction_prompt_version=compact_v1"
    else:
        config["sample_hold_pilot"]["budget"]["physical_attempts_max"] = 1
        expected = "physical_attempts_max=max_iterations"
    config_path = tmp_path / f"{circuit_id}_{defect}.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    with pytest.raises(AutoConfigError, match=expected):
        run_configured_active_learning(
            "SampleHold", circuit_id, config_path=config_path,
            llm_client=lambda _prompt: pytest.fail("LLM must remain unreachable"),
        )


@pytest.mark.parametrize("circuit_id", IDS)
def test_nominal_s0_would_stop_on_d0_only_when_early_stop_is_enabled(circuit_id):
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    records = _records(circuit_id)
    result = run_active_learning(
        records=records,
        circuit_info={"circuit_type": "SampleHold", "circuit_id": circuit_id},
        parameter_template=runtime.param_template,
        performance_template=yaml.safe_load(
            runtime.performance_template_path.read_text(encoding="utf-8")
        ),
        target_specs=_nominal_targets(records),
        adapter=None,
        runtime=runtime,
        max_iterations=1,
        stop_on_meets_spec=True,
        llm_client=lambda _prompt: pytest.fail("initial D0 stop must precede LLM"),
    )
    assert result["iterations_completed"] == 0
    assert result["stop_reason"] == "initial_d0_meets_spec"
    assert result["d0_meets_spec_count"] >= 1


@pytest.mark.parametrize("circuit_id", IDS)
def test_executor_uses_same_child_once_and_reuses_durable_cache(
        tmp_path, monkeypatch, circuit_id):
    launches = []
    runtime_holder = {}
    monkeypatch.setattr(batch, "_frozen_plan", lambda _runtime: _mock_plan())

    def success(command, *, cwd, timeout, environment):
        launches.append(command)
        assert "--al-pilot" in command
        assert 0 < timeout <= 0.25
        candidate = Path(command[command.index("--candidate") + 1])
        request = json.loads((candidate / "input.json").read_text(encoding="utf-8"))
        plan = batch._frozen_plan(runtime_holder["runtime"])
        (candidate / "native_result.json").write_text(json.dumps({
            "vector_sha256": request["vector_sha256"],
            "mock": True,
            "fixture_sha256": plan["fixture"].fixture_hash,
            "stimulus_sha256": plan["config"]["stimulus_sha256"],
            "evaluation": _evaluation(),
        }), encoding="utf-8")
        return SimpleNamespace(
            returncode=0, stdout="", stderr="",
            job_evidence={
                "contained_before_resume": True,
                "active_processes_at_return": 0,
            },
        )

    runtime, executor = _executor(tmp_path, circuit_id, success)
    runtime_holder["runtime"] = runtime
    params = {item["name"]: item["nominal_value"]
              for item in runtime.param_template["parameters"]}
    arguments = runtime.candidate_builder.expand_candidate(params)
    first = executor(arguments)
    second = executor(arguments)
    assert first == second
    assert len(launches) == 1
    stats = executor.execution_statistics()
    assert stats["physical_attempts_reserved"] == 1
    assert stats["outcomes"] == ["complete"]
    assert stats["lock_present"] is False

    changed = dict(params)
    changed[next(iter(changed))] *= 1.01
    with pytest.raises(AutoConfigError, match="physical attempt budget exhausted"):
        executor(runtime.candidate_builder.expand_candidate(changed))


def test_executor_enforces_five_attempt_validation_budget(tmp_path, monkeypatch):
    launches = []
    runtime_holder = {}
    monkeypatch.setattr(batch, "_frozen_plan", lambda _runtime: _mock_plan())

    def success(command, *, cwd, timeout, environment):
        launches.append(command)
        assert "--al-pilot" in command
        candidate = Path(command[command.index("--candidate") + 1])
        request = json.loads((candidate / "input.json").read_text(encoding="utf-8"))
        plan = batch._frozen_plan(runtime_holder["runtime"])
        (candidate / "native_result.json").write_text(json.dumps({
            "vector_sha256": request["vector_sha256"],
            "mock": True,
            "fixture_sha256": plan["fixture"].fixture_hash,
            "stimulus_sha256": plan["config"]["stimulus_sha256"],
            "evaluation": _evaluation(),
        }), encoding="utf-8")
        return SimpleNamespace(
            returncode=0, stdout="", stderr="",
            job_evidence={
                "contained_before_resume": True,
                "active_processes_at_return": 0,
            },
        )

    runtime, executor = _executor(
        tmp_path, "circuit_0259", success, physical_attempts_max=5,
    )
    runtime_holder["runtime"] = runtime
    params = {item["name"]: item["nominal_value"]
              for item in runtime.param_template["parameters"]}
    base_arguments = runtime.candidate_builder.expand_candidate(params)
    varied_name = next(iter(runtime.candidate_builder.scales))
    for index in range(5):
        arguments = dict(base_arguments)
        arguments[varied_name] *= 1.0 + index * 0.001
        executor(arguments)

    stats = executor.execution_statistics()
    assert len(launches) == 5
    assert stats["physical_attempts_reserved"] == 5
    assert stats["physical_attempts_max"] == 5
    sixth = dict(base_arguments)
    sixth[varied_name] *= 1.006
    with pytest.raises(AutoConfigError, match="physical attempt budget exhausted"):
        executor(sixth)


@pytest.mark.parametrize("kind", ("failure", "timeout"))
def test_executor_counts_failure_or_timeout_and_never_retries(
        tmp_path, monkeypatch, kind):
    monkeypatch.setattr(batch, "_frozen_plan", lambda _runtime: _mock_plan())
    def failed(command, *, cwd, timeout, environment):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        raise RuntimeError("mock native failure")

    runtime, executor = _executor(tmp_path, "circuit_0259", failed)
    params = {item["name"]: item["nominal_value"]
              for item in runtime.param_template["parameters"]}
    arguments = runtime.candidate_builder.expand_candidate(params)
    expected = subprocess.TimeoutExpired if kind == "timeout" else RuntimeError
    with pytest.raises(expected):
        executor(arguments)
    assert executor.execution_statistics()["outcomes"] == [kind if kind == "timeout" else "failed"]
    with pytest.raises(AutoConfigError, match="already attempted"):
        executor(arguments)
    assert executor.execution_statistics()["physical_attempts_reserved"] == 1


@pytest.mark.parametrize("circuit_id", IDS)
def test_configured_main_runs_one_mock_iteration_and_keeps_d0_separate(
        tmp_path, monkeypatch, circuit_id):
    config = deepcopy(_pilot_config(circuit_id))
    records = _records(circuit_id)
    config["target_status"] = "experimental_selected"
    config["performance_target_selected"] = True
    config["target_specs"] = _nominal_targets(records)
    config["sample_hold_pilot"]["output_root"] = str((tmp_path / "native").resolve())
    config["sample_hold_pilot"]["execution_id"] = f"sc{circuit_id[-4:]}_al_pilot_mock"
    config_path = tmp_path / f"{circuit_id}.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    d0_path = EXAMPLES / circuit_id / "d0" / "d0_v3_seed42.jsonl"
    d0_before = d0_path.read_bytes()
    evaluations = []

    class FakeExecutor:
        def __call__(self, arguments):
            evaluations.append(dict(arguments))
            return _evaluation()

        def execution_statistics(self):
            return {"physical_attempts_reserved": len(evaluations),
                    "physical_attempts_max": 1, "outcomes": ["complete"]}

    monkeypatch.setattr(
        al_main, "_validate_sample_hold_pilot",
        lambda *_args, **_kwargs: FakeExecutor(),
    )

    nominal = {item["name"]: item["nominal_value"] for item in
               build_active_learning_circuit_config(
                   "SampleHold", circuit_id).param_template["parameters"]}

    def llm(prompt):
        if "Runtime target specifications" in prompt:
            return json.dumps({"x_seed": nominal})
        cases_line = next(line for line in prompt.splitlines()
                          if line.startswith("Candidate cases with nearest-first"))
        cases = json.loads(cases_line.split(": ", 1)[1])
        return json.dumps({"predictions": [
            {"candidate_id": case["candidate_id"], "predicted_metrics": {
                "track_end_error_v": 1.0e-10,
                "sampling_error_v": 3.0e-9,
                "hold_isolation_error_v": 2.0e-14,
            }} for case in cases
        ]})

    contexts = []
    result = run_configured_active_learning(
        "SampleHold", circuit_id, config_path=config_path,
        llm_client=llm, runs_root=tmp_path / "runs",
        run_context_callback=contexts.append,
    )
    assert result["iterations_completed"] == 1
    assert result["stop_reason"] == "max_iterations"
    assert result["d0_meets_spec_count"] >= 1
    assert result["evaluation_counts"]["initial_d0_records_loaded"] == 20
    assert result["evaluation_counts"]["new_real_spice_evaluations"] == 1
    assert len(evaluations) == 1
    assert result["sample_hold_pilot_execution"]["physical_attempts_reserved"] == 1
    assert d0_path.read_bytes() == d0_before
    run_dir = contexts[0]["run_directory"]
    persisted = (run_dir / "active_learning_records.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(persisted) == 21
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["sample_hold_pilot_execution"]["physical_attempts_max"] == 1


@pytest.mark.parametrize("circuit_id", IDS)
def test_configured_main_runs_five_feedback_iterations_with_accumulated_history(
        tmp_path, monkeypatch, circuit_id):
    config = deepcopy(_five_iteration_config(circuit_id))
    config["sample_hold_pilot"]["output_root"] = str((tmp_path / "native").resolve())
    config_path = tmp_path / f"{circuit_id}_five.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    d0_path = EXAMPLES / circuit_id / "d0" / "d0_v3_seed42.jsonl"
    d0_before = d0_path.read_bytes()
    evaluations = []

    class FakeExecutor:
        def __call__(self, arguments):
            evaluations.append(dict(arguments))
            evaluation = _evaluation()
            if len(evaluations) == 3:
                evaluation.update({
                    "simulation_valid": False,
                    "measurement_valid": False,
                    "functional_valid": False,
                    "failure_reasons": ["offline_invalid_simulation"],
                })
            return evaluation

        def execution_statistics(self):
            return {
                "physical_attempts_reserved": len(evaluations),
                "physical_attempts_max": 5,
                "outcomes": ["complete"] * len(evaluations),
            }

    monkeypatch.setattr(
        al_main, "_validate_sample_hold_pilot",
        lambda *_args, **_kwargs: FakeExecutor(),
    )
    nominal = {item["name"]: item["nominal_value"] for item in
               build_active_learning_circuit_config(
                   "SampleHold", circuit_id).param_template["parameters"]}

    def llm(prompt):
        if "Runtime target specifications" in prompt:
            return json.dumps({"x_seed": nominal})
        cases_line = next(line for line in prompt.splitlines()
                          if line.startswith("Candidate cases with nearest-first"))
        cases = json.loads(cases_line.split(": ", 1)[1])
        return json.dumps({"predictions": [
            {"candidate_id": case["candidate_id"], "predicted_metrics": {
                "track_end_error_v": 1.0e-10,
                "sampling_error_v": 3.0e-9,
                "hold_isolation_error_v": 2.0e-14,
            }} for case in cases
        ]})

    contexts = []
    result = run_configured_active_learning(
        "SampleHold", circuit_id, config_path=config_path,
        llm_client=llm, runs_root=tmp_path / "runs",
        run_context_callback=contexts.append,
    )
    assert result["iterations_completed"] == 5
    assert result["stop_reason"] == "max_iterations"
    assert result["stop_on_meets_spec"] is False
    assert result["evaluation_counts"]["initial_d0_records_loaded"] == 20
    assert result["evaluation_counts"]["new_real_spice_evaluations"] == 5
    assert len(evaluations) == 5
    assert [entry["history_size_before_iteration"]
            for entry in result["iteration_log"]] == [20, 21, 22, 23, 24]
    assert result["iteration_log"][2]["simulation_valid"] is False
    assert result["iteration_log"][2]["meets_spec"] is None
    assert result["records"][22]["failure_reasons"] == [
        "offline_invalid_simulation"
    ]
    assert result["sample_hold_pilot_execution"]["physical_attempts_reserved"] == 5
    assert d0_path.read_bytes() == d0_before
    run_dir = contexts[0]["run_directory"]
    assert len((run_dir / "active_learning_records.jsonl").read_text(
        encoding="utf-8").splitlines()) == 25
    assert len((run_dir / "iteration_log.jsonl").read_text(
        encoding="utf-8").splitlines()) == 5
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["iterations_completed"] == 5
    assert summary["sample_hold_pilot_execution"]["physical_attempts_max"] == 5
