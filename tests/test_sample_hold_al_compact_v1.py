"""Offline SampleHold Compact Prompt V1 and shared-AL integration tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ActiveLearning.acquisition import select_next_candidate
from ActiveLearning.active_learning import run_active_learning
from ActiveLearning.inverse_design import propose_inverse_design_seed
from ActiveLearning.onboarding import build_active_learning_circuit_config
from ActiveLearning.onboarding.sample_hold import (
    SampleHoldEvaluatorAdapter,
    SampleHoldFunctionalValidator,
)
from ActiveLearning.performance_prediction import performance_predictor as predictor


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_HOLD = ROOT / "ActiveLearning" / "InitializeD0" / "examples" / "SampleHold"
PERFORMANCE_PATH = (
    ROOT / "ActiveLearning" / "circuit_types" / "SampleHold" / "performance_template.yaml"
)
IDS = ("circuit_0259", "circuit_0263")
ACTIVE_METRICS = (
    "track_end_error_v",
    "sampling_error_v",
    "hold_isolation_error_v",
)


def _records(circuit_id):
    path = SAMPLE_HOLD / circuit_id / "d0" / "d0_v3_seed42.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _performance_template():
    return yaml.safe_load(PERFORMANCE_PATH.read_text(encoding="utf-8"))


def _circuit_info(circuit_id, runtime):
    return {
        "circuit_type": "SampleHold",
        "circuit_id": circuit_id,
        "simulation_environment": "ideal",
        "testbench_contract": deepcopy(runtime.testbench_config),
    }


def _targets(value=1.0e-20):
    return {name: {"operator": "<=", "value": value} for name in ACTIVE_METRICS}


def _candidate(record, candidate_id):
    return {
        "candidate_id": candidate_id,
        "params": dict(record["params"]),
        "normalized_params": dict(record["normalized_params"]),
    }


@pytest.mark.parametrize("circuit_id,dimension", [
    ("circuit_0259", 3),
    ("circuit_0263", 6),
])
def test_inverse_prompt_uses_exact_runtime_keys_for_3d_and_6d(circuit_id, dimension):
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    records = _records(circuit_id)
    expected = list(runtime.param_template["optimization_space"]["variables"])
    nominal = {item["name"]: item["nominal_value"]
               for item in runtime.param_template["parameters"]}
    prompts = []

    def client(prompt):
        prompts.append(prompt)
        return json.dumps({"x_seed": nominal})

    result = propose_inverse_design_seed(
        records, _circuit_info(circuit_id, runtime), runtime.param_template,
        _targets(), context_k=5, llm_client=client,
    )
    assert len(expected) == dimension
    assert list(result["x_seed"]) == expected
    assert result["parameter_valid"] is True
    prompt = prompts[0]
    assert "Return JSON only" in prompt
    assert "You are not a simulator" in prompt
    assert "MMR measured context" in prompt
    assert "largest normalized violated constraint" in prompt
    assert all(name in prompt for name in expected)
    assert "hold_droop_v_per_s" in prompt  # measured context may retain record-only data

    with pytest.raises(ValueError, match="exactly the runtime parameters"):
        propose_inverse_design_seed(
            records, _circuit_info(circuit_id, runtime), runtime.param_template,
            _targets(), llm_client=lambda _: json.dumps({
                "x_seed": {**nominal, "invented": 1.0},
            }),
        )


@pytest.mark.parametrize("circuit_id", IDS)
def test_compact_prompt_deduplicates_pool_and_retains_top3_links(circuit_id):
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    history = _records(circuit_id)
    candidates = [
        _candidate(history[0], "candidate_a"),
        _candidate(history[0], "candidate_b"),
    ]
    template = _performance_template()
    definitions = predictor._extract_performance_definitions(template)
    assert tuple(definitions) == ACTIVE_METRICS
    cases, diagnostics = predictor._build_prediction_cases(
        candidates,
        predictor._filter_usable_records(history, list(definitions)),
        list(definitions),
        sorted(candidates[0]["normalized_params"]),
        3,
        parameter_template=runtime.param_template,
        return_diagnostics=True,
    )
    pool, compact_cases = predictor._build_compact_components(cases, diagnostics)
    compact = predictor._build_sample_hold_compact_prompt(
        _circuit_info(circuit_id, runtime), definitions, cases, diagnostics,
        runtime.param_template,
    )
    verbose = predictor._build_prompt(
        _circuit_info(circuit_id, runtime), definitions, cases,
    )

    assert len(pool) == 3
    assert [item["id"] for item in pool] == ["h001", "h002", "h003"]
    assert len(compact_cases) == 2
    assert all(len(case["neighbors"]) == 3 for case in compact_cases)
    assert compact_cases[0]["neighbors"] == compact_cases[1]["neighbors"]
    assert len(compact.encode("utf-8")) < len(verbose.encode("utf-8"))
    metric_line = next(
        line for line in compact.splitlines()
        if line.startswith("Runtime metric definitions and directions:")
    )
    assert '"hold_droop_v_per_s"' not in metric_line
    assert all(f'"{name}"' in compact for name in ACTIVE_METRICS)
    assert all(f'"{name}"' in compact for name in
               runtime.param_template["optimization_space"]["variables"])
    assert '"unit":"V"' in compact
    assert '"scale":' in compact
    assert "e-" in compact


@pytest.mark.parametrize("circuit_id", IDS)
def test_compact_prediction_parser_committee_and_acquisition(circuit_id):
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    history = _records(circuit_id)
    candidates = [
        _candidate(history[0], "candidate_a"),
        _candidate(history[1], "candidate_b"),
    ]
    prompts = []
    response = json.dumps({"predictions": [
        {"candidate_id": candidate["candidate_id"], "predicted_metrics": {
            "track_end_error_v": 1.2e-10 + index * 1e-12,
            "sampling_error_v": 4.5e-9 + index * 1e-11,
            "hold_isolation_error_v": 2.3e-14 + index * 1e-15,
        }}
        for index, candidate in enumerate(candidates)
    ]})

    def client(prompt):
        prompts.append(prompt)
        return response

    predictions = predictor.predict_candidate_performance(
        candidates, history, _circuit_info(circuit_id, runtime),
        _performance_template(), parameter_template=runtime.param_template,
        retrieval_k=3, committee_size=5, prompt_version="compact_v1",
        llm_client=client,
    )
    assert len(prompts) == 5
    assert all(set(item["prediction"]) == set(ACTIVE_METRICS)
               for item in predictions)
    assert all(item["committee_attempt_counts"] == [1, 1, 1, 1, 1]
               for item in predictions)
    selected, scored = select_next_candidate(
        predictions, _performance_template(), _targets(1.0e-8),
        return_scored=True,
    )
    assert selected["candidate_id"] in {"candidate_a", "candidate_b"}
    assert len(scored) == 2


def test_record_only_droop_is_predicted_only_when_runtime_activates_it():
    runtime = build_active_learning_circuit_config("SampleHold", "circuit_0259")
    history = _records("circuit_0259")
    candidate = _candidate(history[0], "candidate_a")
    template = _performance_template()
    template["performance_template"]["active_metrics"].append("hold_droop_v_per_s")
    response = json.dumps({"predictions": [{
        "candidate_id": "candidate_a",
        "predicted_metrics": {
            "track_end_error_v": 1e-10,
            "sampling_error_v": 1e-9,
            "hold_isolation_error_v": 1e-14,
            "hold_droop_v_per_s": 0.2,
        },
    }]})
    result = predictor.predict_candidate_performance(
        [candidate], history, _circuit_info("circuit_0259", runtime), template,
        parameter_template=runtime.param_template, prompt_version="compact_v1",
        committee_size=1, llm_client=lambda _: response,
    )
    assert set(result[0]["prediction"]) == set(ACTIVE_METRICS) | {"hold_droop_v_per_s"}


@pytest.mark.parametrize("circuit_id", IDS)
def test_mock_one_iteration_uses_existing_d0_and_structured_evaluator(
        tmp_path, circuit_id):
    runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
    records = _records(circuit_id)
    assert len(records) == 20
    expected_keys = list(runtime.param_template["optimization_space"]["variables"])
    nominal = {item["name"]: item["nominal_value"]
               for item in runtime.param_template["parameters"]}
    evaluations = []

    def fake_executor(arguments):
        evaluations.append(dict(arguments))
        return {
            "metrics": {
                "track_end_error_v": 9e-11,
                "sampling_error_v": 3e-9,
                "hold_isolation_error_v": 2e-14,
                "hold_droop_v_per_s": 0.25,
            },
            "parameter_valid": True,
            "simulation_valid": True,
            "measurement_valid": True,
            "functional_valid": True,
            "failure_reasons": [],
            "diagnostics": {"mock_only": True},
        }

    provider = SimpleNamespace(
        create_evaluator=lambda configured, testbench_overrides=None:
            SampleHoldEvaluatorAdapter(configured, fake_executor),
        create_functional_validator=lambda configured:
            SampleHoldFunctionalValidator(),
    )
    mock_runtime = replace(runtime, runtime_provider=provider)
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        if "Runtime target specifications" in prompt:
            return json.dumps({"x_seed": nominal})
        cases_line = next(
            line for line in prompt.splitlines()
            if line.startswith("Candidate cases with nearest-first Top-3 pool references:")
        )
        cases = json.loads(cases_line.split(": ", 1)[1])
        candidate_ids = [case["candidate_id"] for case in cases]
        assert candidate_ids
        return json.dumps({"predictions": [
            {"candidate_id": candidate_id, "predicted_metrics": {
                "track_end_error_v": 1.1e-10,
                "sampling_error_v": 4.0e-9,
                "hold_isolation_error_v": 3.0e-14,
            }} for candidate_id in candidate_ids
        ]})

    result = run_active_learning(
        records=records,
        circuit_info=_circuit_info(circuit_id, mock_runtime),
        parameter_template=mock_runtime.param_template,
        performance_template=_performance_template(),
        target_specs=_targets(),
        adapter=None,
        runtime=mock_runtime,
        max_iterations=1,
        stop_on_meets_spec=False,
        retrieval_k=3,
        committee_size=2,
        prediction_prompt_version="compact_v1",
        output_dir=tmp_path,
        llm_client=llm,
    )
    assert len(evaluations) == 1
    assert set(result["records"][-1]["params"]) == set(expected_keys)
    assert evaluations[0] == mock_runtime.candidate_builder.expand_candidate(
        result["records"][-1]["params"]
    )
    assert result["records"][-1]["status"]["functional_valid"] is True
    assert result["records"][-1]["diagnostics"]["mock_only"] is True
    assert result["iterations_completed"] == 1
    assert result["evaluation_counts"]["new_real_spice_evaluations"] == 1
    assert (tmp_path / "active_learning_records.jsonl").is_file()
    assert (tmp_path / "iteration_log.jsonl").is_file()
    persisted = [
        json.loads(line)
        for line in (tmp_path / "active_learning_records.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert len(persisted) == 21
    assert persisted[-1]["params"] == result["records"][-1]["params"]
    assert len(prompts) == 3  # one inverse request plus two committee rounds


def test_compact_v1_does_not_change_other_circuit_prompt_selection():
    with pytest.raises(ValueError, match="supports SampleHold only"):
        predictor.predict_candidate_performance(
            [], [], {"circuit_type": "CurrentMirror"}, {},
            prompt_version="compact_v1", llm_client=lambda _: None,
        )
