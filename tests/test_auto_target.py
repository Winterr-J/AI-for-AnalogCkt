"""Offline tests for optional automatic experimental-target preparation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from copy import deepcopy

import pytest
import yaml

from ActiveLearning.auto_target import (
    _artifact_profile,
    build_target_prompt,
    discover_anchor,
    metric_contract,
    parse_target_response,
    prepare_experiment,
    recommend_target,
    run_bounded_profile,
    validate_target_recommendation,
)
from ActiveLearning.main import _resolve_experiment
from ActiveLearning.main import initialize_experiment_d0
from ActiveLearning.main import main as active_learning_main
from ActiveLearning.main import run_configured_active_learning
from ActiveLearning.d0_initialization import initialize_configured_d0
from ActiveLearning.InitializeD0.sampling.initial_sampler import (
    generate_v1_global_samples,
    generate_v2_local_global_samples,
    generate_v4_norm_bounded_local_global_samples,
)
from ActiveLearning.inverse_design.llm_client import LLMTransientError
from ActiveLearning.onboarding import AutoConfigError, build_active_learning_circuit_config


ROOT = Path(__file__).resolve().parents[1]
MIRROR_TEMPLATE = ROOT / "ActiveLearning/circuit_types/CurrentMirror/performance_template.yaml"
OPAMP_TEMPLATE = ROOT / "ActiveLearning/circuit_types/OpAmp/performance_template.yaml"


def _runtime(circuit_type="CurrentMirror", circuit_id="synthetic"):
    template = MIRROR_TEMPLATE if circuit_type == "CurrentMirror" else OPAMP_TEMPLATE
    parameters = tuple(
        SimpleNamespace(
            name=name, nominal=0.5, lower=0.1, upper=0.9,
            variable_type="continuous", scale="linear", transform=None,
            fixed_sign=None, unit=None,
        )
        for name in ("A", "B")
    )
    return SimpleNamespace(
        circuit_type=circuit_type, circuit_id=circuit_id,
        runtime_parameters=parameters, performance_template_path=template,
        operating_conditions={"VDD": 3.3},
    )


def _template():
    return yaml.safe_load(MIRROR_TEMPLATE.read_text(encoding="utf-8"))


def _point(sample):
    nominal = sample["source"] == "nominal"
    return {
        "sample_id": sample["sample_id"], "params": sample["params"],
        "source": sample["source"], "parameter_valid": True,
        "simulation_valid": True, "functional_valid": True,
        "current_error": 0.5 if nominal else 0.1,
        "output_resistance": 90.0 if nominal else 120.0,
    }


def _proposal(*, error=0.2, resistance=110.0, support="profile_global_0_0000"):
    return {
        "target_specs": {
            "current_error": {"operator": "<=", "value": error, "unit": "ratio"},
            "output_resistance": {"operator": ">=", "value": resistance, "unit": "ohm"},
        },
        "reason": "One measured point supports both thresholds",
        "supporting_sample_ids": [support], "confidence": "medium",
    }


def _profile_for_validation():
    anchor = _point({"sample_id": "anchor_nominal", "source": "nominal", "params": {"A": 0.5}})
    row = {"sample_id": "profile_global_0_0000",
           "metrics": {"current_error": 0.1, "output_resistance": 120.0}}
    return anchor, [row]


def test_anchor_requires_functional_validity_and_uses_local_search():
    runtime = _runtime()

    def evaluator(sample):
        record = _point(sample)
        if sample["source"] == "nominal":
            record["functional_valid"] = False
        return record

    found = discover_anchor(runtime, evaluator, ("current_error", "output_resistance"),
                            max_evaluations=5, local_evaluations=2, seed=42)
    assert found["status"] == "ANCHOR_FOUND"
    assert found["anchor"]["source"] == "local"
    assert found["evaluated_candidates"] == 3
    assert found["records"][0]["simulation_valid"] is True
    assert found["records"][0]["functional_valid"] is False


def test_anchor_failure_is_structured_and_unstable_evaluator_stops_early():
    runtime = _runtime()
    failed = discover_anchor(runtime, lambda sample: {
        **_point(sample), "functional_valid": False,
    }, ("current_error", "output_resistance"), max_evaluations=4, local_evaluations=1)
    assert failed["status"] == "NO_VALID_ANCHOR"
    assert failed["anchor"] is None
    calls = []

    def crashing(sample):
        calls.append(sample["sample_id"])
        raise RuntimeError("mock simulator failure")

    unstable = discover_anchor(runtime, crashing, ("current_error", "output_resistance"),
                               max_evaluations=12, local_evaluations=6)
    assert unstable["status"] == "ANCHOR_SIMULATION_FAILED"
    assert len(calls) == 3
    assert all(item["simulation_valid"] is False for item in unstable["records"])

    def mixed(sample):
        record = _point(sample)
        record["functional_valid"] = False
        if sample["source"] == "global":
            record["simulation_valid"] = False
        return record

    exhausted = discover_anchor(runtime, mixed, ("current_error", "output_resistance"),
                                max_evaluations=4, local_evaluations=1)
    assert exhausted["status"] == "ANCHOR_BUDGET_EXHAUSTED"
    invalid_params = discover_anchor(
        runtime,
        lambda sample: {**_point(sample), "parameter_valid": False,
                        "simulation_valid": False, "functional_valid": False},
        ("current_error", "output_resistance"),
        max_evaluations=2, local_evaluations=0,
    )
    assert invalid_params["status"] == "NO_VALID_ANCHOR"


def test_bounded_profile_counts_only_functional_rows_and_retains_failures():
    runtime = _runtime()
    anchor = discover_anchor(runtime, _point, ("current_error", "output_resistance"))

    def evaluator(sample):
        record = _point(sample)
        if sample["source"] == "global":
            record["functional_valid"] = False
            record["failure_reasons"] = ["mock_functional_failure"]
        return record

    profile = run_bounded_profile(
        runtime, anchor, evaluator, ("current_error", "output_resistance"),
        samples=4, max_evaluations=6, additional_samples=2,
        min_valid_samples=2, seed=42,
    )
    assert profile["total_samples"] == 4
    assert profile["simulation_valid_samples"] == 4
    assert profile["functional_valid_samples"] == 2
    assert profile["failure_distribution"]["mock_functional_failure"] == 2
    assert len(profile["valid_candidate_metrics"]) == 2
    assert all(row["sample_id"].startswith("profile_local")
               for row in profile["valid_candidate_metrics"])
    assert profile["anchor_spice_point_evaluations"] == 1


def test_prompt_is_joint_and_excludes_parameter_vectors():
    runtime = _runtime()
    anchor, rows = _profile_for_validation()
    profile = {
        "anchor": anchor, "total_samples": 1, "simulation_valid_samples": 1,
        "functional_valid_samples": 1, "performance": {},
        "valid_candidate_metrics": rows,
    }
    prompt = json.loads(build_target_prompt(runtime, _template(), profile))
    assert prompt["profiling"]["joint_candidate_rows"] == rows
    assert "params" not in json.dumps(prompt["profiling"]["joint_candidate_rows"])
    assert prompt["metric_contract"]["current_error"]["direction"] == "minimize"
    assert prompt["metric_contract"]["output_resistance"]["unit"] == "ohm"
    assert prompt["operating_conditions"] == {"VDD": 3.3}


def test_json_parser_and_schema_direction_unit_validation():
    anchor, rows = _profile_for_validation()
    payload = parse_target_response("```json\n" + json.dumps(_proposal()) + "\n```")
    valid = validate_target_recommendation(payload, _template(), anchor, rows)
    assert valid["valid"] is True
    assert valid["joint_supporting_sample_ids"] == [rows[0]["sample_id"]]
    with pytest.raises(ValueError, match="empty"):
        parse_target_response("   ")
    with pytest.raises(json.JSONDecodeError):
        parse_target_response("{bad JSON")

    wrong = _proposal()
    wrong["target_specs"]["current_error"]["operator"] = ">="
    wrong["target_specs"]["output_resistance"]["unit"] = "Hz"
    invalid = validate_target_recommendation(wrong, _template(), anchor, rows)
    assert invalid["valid"] is False
    assert any("operator" in item for item in invalid["errors"])
    assert any("unit" in item for item in invalid["errors"])


def test_joint_feasibility_nontriviality_and_support_ids():
    anchor, rows = _profile_for_validation()
    template = _template()
    assert not validate_target_recommendation(
        _proposal(error=0.05), template, anchor, rows
    )["valid"]
    assert not validate_target_recommendation(
        _proposal(error=0.6, resistance=80), template, anchor, rows
    )["valid"]
    assert not validate_target_recommendation(
        _proposal(support="nonexistent"), template, anchor, rows
    )["valid"]
    missing = _proposal()
    del missing["target_specs"]["output_resistance"]
    assert not validate_target_recommendation(missing, template, anchor, rows)["valid"]
    missing = _proposal()
    missing["target_specs"]["current_error"]["value"] = None
    assert not validate_target_recommendation(missing, template, anchor, rows)["valid"]


def test_invalid_llm_response_gets_one_correction_then_succeeds():
    runtime = _runtime()
    anchor, rows = _profile_for_validation()
    profile = {
        "anchor": anchor, "total_samples": 1, "simulation_valid_samples": 1,
        "functional_valid_samples": 1, "performance": {},
        "valid_candidate_metrics": rows,
    }
    prompts = []

    def llm(prompt, **kwargs):
        prompts.append(json.loads(prompt))
        if len(prompts) == 1:
            return "not JSON"
        return json.dumps(_proposal())

    result = recommend_target(runtime, _template(), profile, llm_client=llm,
                              provider="gemini", model="mock-model")
    assert result["status"] == "TARGET_VALIDATED"
    assert result["attempts"] == 2
    assert result["correction_attempts"] == 1
    assert prompts[1]["previous_validation_errors"]


def test_invalid_llm_exhausts_correction_and_one_call_budget():
    runtime = _runtime()
    anchor, rows = _profile_for_validation()
    profile = {"anchor": anchor, "total_samples": 1, "simulation_valid_samples": 1,
               "functional_valid_samples": 1, "performance": {},
               "valid_candidate_metrics": rows}
    failed = recommend_target(runtime, _template(), profile,
                              llm_client=lambda *args, **kwargs: "{}")
    assert failed["status"] == "TARGET_VALIDATION_FAILED"
    assert failed["attempts"] == 2
    single = recommend_target(runtime, _template(), profile,
                              llm_client=lambda *args, **kwargs: "{}",
                              max_llm_calls=1)
    assert single["attempts"] == 1
    malformed = recommend_target(runtime, _template(), profile,
                                 llm_client=lambda *args, **kwargs: "not JSON")
    assert malformed["status"] == "TARGET_GENERATION_FAILED"
    assert malformed["attempts"] == 2


def test_transient_llm_error_has_bounded_retry():
    runtime = _runtime()
    anchor, rows = _profile_for_validation()
    profile = {"anchor": anchor, "total_samples": 1, "simulation_valid_samples": 1,
               "functional_valid_samples": 1, "performance": {},
               "valid_candidate_metrics": rows}
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise LLMTransientError("mock timeout")
        return json.dumps(_proposal())

    result = recommend_target(runtime, _template(), profile, llm_client=flaky)
    assert result["status"] == "TARGET_VALIDATED"
    assert result["attempts"] == 2
    assert len(attempts) == 2


def test_generated_config_is_isolated_and_existing_main_can_parse_it(tmp_path):
    runtime = build_active_learning_circuit_config(
        "CurrentMirror", "circuit_0469", repository_root=ROOT
    )
    original = ROOT / "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/active_learning_config.yaml"
    d0 = ROOT / "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/d0/d0_v3_seed42.jsonl"
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, d0)}

    result = prepare_experiment(
        "CurrentMirror", "circuit_0469", runtime=runtime,
        point_evaluator=_point,
        llm_client=lambda *args, **kwargs: json.dumps(_proposal()),
        output_root=tmp_path, profile_samples=4, max_profile_evaluations=4,
        additional_samples=0, provider="gemini", model="mock-model",
    )
    assert result["status"] == "READY_FOR_D0"
    assert Path(result["artifact_paths"]["anchor"]).is_file()
    config_path = Path(result["artifact_paths"]["config"])
    assert config_path != original
    generated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    source = yaml.safe_load(original.read_text(encoding="utf-8"))
    assert generated["data"]["d0_mode"] == "independent"
    assert generated["data"]["d0_path_relative_to"] == "config"
    assert generated["data"]["d0_path"] == "d0/d0_v3_seed42.jsonl"
    assert generated["data"]["initialize_if_missing"] is True
    assert generated["data"]["initial_anchor"]["params"] == {
        item.name: item.nominal for item in runtime.runtime_parameters
    }
    assert generated["active_learning"] == source["active_learning"]
    assert generated["target_specs"]["current_error"]["value"] == 0.2
    assert generated["target_provenance"]["provider"] == "gemini"
    assert generated["target_provenance"]["llm_backend"] == "injected"
    assert generated["target_provenance"]["profiling_backend"] == "mock"
    assert generated["target_source"] == "auto_generated"
    assert generated["target_provenance"]["d0_mode"] == "independent"
    assert generated["target_provenance"]["target_recommendation"] == "target_generation.json"
    assert generated["target_provenance"]["generated_config"] == config_path.name
    assert not PureWindowsPath(generated["target_provenance"]["profiling_artifact"]).drive
    assert generated["data"]["d0_path"] == PurePosixPath(generated["data"]["d0_path"]).as_posix()
    _, parsed, target_specs, parsed_d0 = _resolve_experiment(
        "CurrentMirror", "circuit_0469", config_path, runtime
    )
    assert parsed == generated
    assert target_specs == generated["target_specs"]
    assert parsed_d0 == config_path.parent / "d0/d0_v3_seed42.jsonl"
    assert not parsed_d0.exists()
    inspected = active_learning_main([
        "--circuit-type", "CurrentMirror", "--circuit-id", "circuit_0469",
        "--config", str(config_path), "--config-only",
    ])
    assert inspected["d0_mode"] == "independent"
    assert Path(inspected["resolved_d0_path"]) == parsed_d0
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, d0)} == before
    assert result["costs"]["anchor_spice_point_evaluations"] == 1
    assert result["costs"]["profile_spice_point_evaluations"] == 4
    assert result["costs"]["llm_calls"] == 1
    assert result["d0"]["initialized"] is False


def test_opamp_generated_config_and_saved_profile_resume_without_spice(tmp_path):
    runtime = build_active_learning_circuit_config(
        "OpAmp", "circuit_0493", repository_root=ROOT
    )
    original = ROOT / "ActiveLearning/InitializeD0/examples/OpAmp/circuit_0493/active_learning_config.yaml"
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()

    def opamp_point(sample):
        nominal = sample["source"] == "nominal"
        return {"sample_id": sample["sample_id"], "params": sample["params"],
                "parameter_valid": True, "simulation_valid": True,
                "functional_valid": True,
                "low_frequency_open_loop_gain_db": 35.0 if nominal else 45.0,
                "unity_gain_frequency_hz": 3e6 if nominal else 5e6,
                "phase_margin_deg": 80.0 if nominal else 90.0}

    proposal = {
        "target_specs": {
            "low_frequency_open_loop_gain_db": {"operator": ">=", "value": 40.0, "unit": "dB"},
            "unity_gain_frequency_hz": {"operator": ">=", "value": 4e6, "unit": "Hz"},
            "phase_margin_deg": {"operator": ">=", "value": 87.8, "unit": "degree"},
        },
        "reason": "A single measured candidate supports all three metrics",
        "supporting_sample_ids": ["profile_global_0_0000"],
        "confidence": "medium",
    }
    first = prepare_experiment(
        "OpAmp", "circuit_0493", runtime=runtime,
        point_evaluator=opamp_point, output_root=tmp_path,
        profile_samples=2, max_profile_evaluations=2, additional_samples=0,
    )
    assert first["status"] == "AWAITING_LLM_APPROVAL"
    profile_path = Path(first["artifact_paths"]["profiling"])

    def forbidden_spice(sample):
        raise AssertionError("Saved profile must not evaluate another point")

    resumed = prepare_experiment(
        "OpAmp", "circuit_0493", runtime=runtime,
        profile_artifact=profile_path, point_evaluator=forbidden_spice,
        llm_client=lambda *args, **kwargs: json.dumps(proposal),
        output_root=tmp_path, provider="gemini", model="mock-model",
    )
    assert resumed["status"] == "READY_FOR_D0"
    assert resumed["costs"]["evaluation_backend"] == "artifact"
    assert resumed["costs"]["anchor_spice_point_evaluations"] == 0
    config_path = Path(resumed["artifact_paths"]["config"])
    generated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    source = yaml.safe_load(original.read_text(encoding="utf-8"))
    assert generated["data"]["d0_mode"] == "independent"
    assert generated["data"]["d0_path_relative_to"] == "config"
    assert generated["data"]["d0_path"] == "d0/d0_v4_seed42.jsonl"
    assert generated["data"]["initialize_if_missing"] is True
    for key in ("sampling_mode", "n_samples", "n_local", "n_global", "local_radius"):
        assert generated["data"][key] == source["data"][key]
    assert generated["target_roles"] == source["target_roles"]
    _, _, specs, d0_path = _resolve_experiment("OpAmp", "circuit_0493", config_path, runtime)
    assert specs["phase_margin_deg"]["value"] == 87.8
    assert d0_path == config_path.parent / "d0/d0_v4_seed42.jsonl"
    assert not d0_path.exists()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash


def test_explicit_reuse_preserves_frozen_d0_and_never_initializes(tmp_path):
    runtime = build_active_learning_circuit_config(
        "CurrentMirror", "circuit_0469", repository_root=ROOT
    )
    original = ROOT / "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/active_learning_config.yaml"
    frozen = ROOT / "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/d0/d0_v3_seed42.jsonl"
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, frozen)}
    result = prepare_experiment(
        "CurrentMirror", "circuit_0469", runtime=runtime,
        point_evaluator=_point,
        llm_client=lambda *args, **kwargs: json.dumps(_proposal()),
        d0_mode="reuse", output_root=tmp_path,
        profile_samples=2, max_profile_evaluations=2, additional_samples=0,
    )
    assert result["status"] == "READY_FOR_D0"
    config_path = Path(result["artifact_paths"]["config"])
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["data"]["d0_mode"] == "reuse"
    assert config["data"]["d0_path"] == "d0/d0_v3_seed42.jsonl"
    assert config["data"]["initialize_if_missing"] is False
    assert "initial_anchor" not in config["data"]
    assert Path(result["d0"]["path"]) == frozen
    assert _resolve_experiment("CurrentMirror", "circuit_0469", config_path, runtime)[3] == frozen
    assert initialize_experiment_d0(
        "CurrentMirror", "circuit_0469", config_path=config_path, runtime_config=runtime
    )["created"] is False
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, frozen)} == before


def test_independent_d0_uses_off_nominal_anchor_and_preserves_v3_composition(tmp_path):
    runtime = build_active_learning_circuit_config(
        "CurrentMirror", "circuit_0469", repository_root=ROOT
    )
    frozen = ROOT / "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/d0/d0_v3_seed42.jsonl"
    frozen_hash = hashlib.sha256(frozen.read_bytes()).hexdigest()

    def point(sample):
        record = _point(sample)
        if sample["sample_id"] == "anchor_nominal":
            record["functional_valid"] = False
        elif sample["sample_id"].startswith("anchor_"):
            record.update(current_error=0.5, output_resistance=90.0)
        return record

    result = prepare_experiment(
        "CurrentMirror", "circuit_0469", runtime=runtime,
        point_evaluator=point,
        llm_client=lambda *args, **kwargs: json.dumps(_proposal()),
        output_root=tmp_path, profile_samples=2, max_profile_evaluations=2,
        additional_samples=0, max_anchor_evaluations=3, anchor_local_evaluations=2,
    )
    assert result["status"] == "READY_FOR_D0"
    config_path = Path(result["artifact_paths"]["config"])
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    initial_anchor = config["data"]["initial_anchor"]
    assert initial_anchor["sample_id"].startswith("anchor_local_")
    assert initial_anchor["params"] != {
        item.name: item.nominal for item in runtime.runtime_parameters
    }
    evaluations = []

    def evaluator(params):
        evaluations.append(dict(params))
        return 0.1, 120.0, True

    def functional_validator(params, **kwargs):
        return {"functional_feasible": True,
                "compliance": {"relative_variation": 0.01},
                "reference_tracking": {"max_tracking_error": 0.01}}

    fake_runtime = SimpleNamespace(
        param_template=runtime.param_template,
        performance_template_path=runtime.performance_template_path,
        adapter=runtime.adapter,
        create_evaluator=lambda: evaluator,
        create_functional_validator=lambda: functional_validator,
    )
    d0 = initialize_experiment_d0(
        "CurrentMirror", "circuit_0469", config_path=config_path,
        runtime_config=fake_runtime,
    )
    assert d0["created"] is True
    assert d0["d0_mode"] == "independent"
    assert d0["anchor_sample_id"] == initial_anchor["sample_id"]
    assert len(d0["records"]) == 20
    assert [r["sampling_source"] for r in d0["records"]] == [
        "anchor", *(["local"] * 10), *(["global"] * 9)
    ]
    assert d0["records"][0]["params"] == initial_anchor["params"]
    assert d0["records"][0]["anchor_sample_id"] == initial_anchor["sample_id"]
    assert d0["artifacts"]["jsonl"] == config_path.parent / "d0/d0_v3_seed42.jsonl"
    manifest = json.loads(d0["artifacts"]["manifest"].read_text(encoding="utf-8"))
    assert manifest["d0_mode"] == "independent"
    assert manifest["anchor_sample_id"] == initial_anchor["sample_id"]
    assert manifest["seed"] == 42
    assert manifest["records"] == 20
    assert manifest["target_source"] == "auto_generated"
    assert len(evaluations) == 20
    assert len({tuple(r["params"].values()) for r in d0["records"]}) == 20
    for record in d0["records"][1:11]:
        for name, value in record["params"].items():
            ratio = abs(value / initial_anchor["params"][name])
            assert 1 / 1.25 <= ratio <= 1.25
    for record in d0["records"][11:]:
        assert all(0.0 <= value <= 1.0 for value in record["normalized_params"].values())
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == frozen_hash
    assert initialize_experiment_d0(
        "CurrentMirror", "circuit_0469", config_path=config_path,
        runtime_config=fake_runtime,
    )["created"] is False
    assert len(evaluations) == 20


def test_independent_path_rejects_escape_and_existing_frozen_output(tmp_path):
    runtime = build_active_learning_circuit_config(
        "CurrentMirror", "circuit_0469", repository_root=ROOT
    )
    frozen = ROOT / "ActiveLearning/InitializeD0/examples/CurrentMirror/circuit_0469/d0/d0_v3_seed42.jsonl"
    before = hashlib.sha256(frozen.read_bytes()).hexdigest()
    result = prepare_experiment(
        "CurrentMirror", "circuit_0469", runtime=runtime,
        point_evaluator=_point, llm_client=lambda *args, **kwargs: json.dumps(_proposal()),
        output_root=tmp_path, profile_samples=2, max_profile_evaluations=2,
        additional_samples=0,
    )
    config = yaml.safe_load(Path(result["artifact_paths"]["config"]).read_text(encoding="utf-8"))
    config_path = tmp_path / "unsafe.yaml"
    for unsafe in ("../escape.jsonl", "C:/historical/d0.jsonl", r"C:\historical\d0.jsonl"):
        changed = deepcopy(config)
        changed["data"]["d0_path"] = unsafe
        config_path.write_text(yaml.safe_dump(changed), encoding="utf-8")
        with pytest.raises(ValueError, match="independent D0"):
            _resolve_experiment("CurrentMirror", "circuit_0469", config_path, runtime)
    with pytest.raises(FileExistsError, match="already exists"):
        initialize_configured_d0(runtime, config, config["target_specs"], frozen)
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == before


def test_partial_independent_d0_is_not_loaded_or_overwritten(tmp_path):
    runtime = build_active_learning_circuit_config(
        "CurrentMirror", "circuit_0469", repository_root=ROOT
    )
    result = prepare_experiment(
        "CurrentMirror", "circuit_0469", runtime=runtime,
        point_evaluator=_point, llm_client=lambda *args, **kwargs: json.dumps(_proposal()),
        output_root=tmp_path, profile_samples=2, max_profile_evaluations=2,
        additional_samples=0,
    )
    config_path = Path(result["artifact_paths"]["config"])
    d0_path = Path(result["d0"]["path"])
    d0_path.parent.mkdir(parents=True)
    partial = b'{"sample_id":"partial"}\n'
    d0_path.write_bytes(partial)
    with pytest.raises(AutoConfigError, match="independent D0 is incomplete"):
        initialize_experiment_d0(
            "CurrentMirror", "circuit_0469", config_path=config_path,
            runtime_config=runtime,
        )
    with pytest.raises(AutoConfigError, match="independent D0 is incomplete"):
        run_configured_active_learning(
            "CurrentMirror", "circuit_0469", config_path=config_path,
            runtime_config=runtime, llm_client=object(),
        )
    assert d0_path.read_bytes() == partial


def test_v4_local_sampler_accepts_anchor_without_changing_global_policy():
    runtime = build_active_learning_circuit_config(
        "OpAmp", "circuit_0493", repository_root=ROOT
    )
    center = {item.name: item.nominal for item in runtime.runtime_parameters}
    center["R0"] *= 1.2
    first = generate_v4_norm_bounded_local_global_samples(
        runtime.param_template, local_radius=0.002, n_local=2, n_global=2,
        seed=42, center_params=center,
    )
    second = generate_v4_norm_bounded_local_global_samples(
        runtime.param_template, local_radius=0.002, n_local=2, n_global=2,
        seed=42, center_params=center,
    )
    assert first == second
    assert first[0]["params"] == center
    assert [item["sampling_source"] for item in first] == [
        "anchor", "local", "local", "global", "global"
    ]
    assert all(item["actual_normalized_l2_distance"] <= 0.002 for item in first[1:3])
    assert all(0.0 <= value <= 1.0 for item in first[3:] for value in item["normalized_params"].values())


def test_v1_v2_optional_anchor_keeps_original_global_counts():
    runtime = build_active_learning_circuit_config(
        "CurrentMirror", "circuit_0469", repository_root=ROOT
    )
    center = {item.name: item.nominal for item in runtime.runtime_parameters}
    center["R0"] *= 1.2
    v1 = generate_v1_global_samples(
        runtime.param_template, n_samples=4, seed=42, center_params=center
    )
    v2 = generate_v2_local_global_samples(
        runtime.param_template, n_local=2, n_global=1, seed=42,
        center_params=center,
    )
    assert v1[0]["params"] == v2[0]["params"] == center
    assert [item["sampling_source"] for item in v1] == [
        "anchor", "global", "global", "global"
    ]
    assert [item["sampling_source"] for item in v2] == [
        "anchor", "local", "local", "global"
    ]


def test_opamp_v4_independent_d0_uses_profile_anchor_with_mock_evaluation(tmp_path):
    runtime = build_active_learning_circuit_config(
        "OpAmp", "circuit_0493", repository_root=ROOT
    )
    original = ROOT / "ActiveLearning/InitializeD0/examples/OpAmp/circuit_0493/d0/d0_v4_seed42.jsonl"
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()

    def point(sample):
        nominal = sample["sample_id"] == "anchor_nominal"
        anchor_candidate = sample["sample_id"].startswith("anchor_")
        return {
            "sample_id": sample["sample_id"], "params": sample["params"],
            "parameter_valid": True, "simulation_valid": True,
            "functional_valid": not nominal,
            "low_frequency_open_loop_gain_db": 35.0 if anchor_candidate else 45.0,
            "unity_gain_frequency_hz": 3e6 if anchor_candidate else 5e6,
            "phase_margin_deg": 80.0 if anchor_candidate else 90.0,
        }

    proposal = {
        "target_specs": {
            "low_frequency_open_loop_gain_db": {"operator": ">=", "value": 40.0, "unit": "dB"},
            "unity_gain_frequency_hz": {"operator": ">=", "value": 4e6, "unit": "Hz"},
            "phase_margin_deg": {"operator": ">=", "value": 87.8, "unit": "degree"},
        },
        "reason": "Mock joint support", "supporting_sample_ids": ["profile_global_0_0000"],
    }
    prepared = prepare_experiment(
        "OpAmp", "circuit_0493", runtime=runtime,
        point_evaluator=point, llm_client=lambda *args, **kwargs: json.dumps(proposal),
        output_root=tmp_path, profile_samples=2, max_profile_evaluations=2,
        additional_samples=0, max_anchor_evaluations=3, anchor_local_evaluations=2,
    )
    assert prepared["status"] == "READY_FOR_D0"
    config_path = Path(prepared["artifact_paths"]["config"])
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    anchor = config["data"]["initial_anchor"]
    assert anchor["sample_id"].startswith("anchor_local_")
    assert len(anchor["params"]) == 60
    assert config["data"]["sampling_mode"] == "v4"
    assert config["data"]["local_radius"] == 0.002
    evaluated = []

    class Evaluator:
        def evaluate(self, params):
            evaluated.append(dict(params))
            return {"simulation_valid": True}

    class Validator:
        def validate(self, params, evaluation, *, target_specs):
            return {
                "status": {"parameter_valid": True, "simulation_valid": True,
                           "functional_valid": True, "functional_status": "valid",
                           "meets_spec": False},
                "metrics": {"low_frequency_open_loop_gain_db": 45.0,
                            "unity_gain_frequency_hz": 5e6, "phase_margin_deg": 90.0},
            }

    fake_runtime = SimpleNamespace(
        param_template=runtime.param_template,
        performance_template_path=runtime.performance_template_path,
        adapter=runtime.adapter,
        create_evaluator=Evaluator,
        create_functional_validator=Validator,
    )
    initialized = initialize_experiment_d0(
        "OpAmp", "circuit_0493", config_path=config_path, runtime_config=fake_runtime
    )
    assert initialized["created"] is True
    assert len(initialized["records"]) == 20
    assert [r["sampling_source"] for r in initialized["records"]] == [
        "anchor", *(["local"] * 10), *(["global"] * 9)
    ]
    assert initialized["records"][0]["params"] == anchor["params"]
    assert initialized["records"][0]["anchor_sample_id"] == anchor["sample_id"]
    assert all(
        r["actual_normalized_l2_distance"] <= 0.002
        for r in initialized["records"][1:11]
    )
    assert len(evaluated) <= 20
    assert initialized["artifacts"]["jsonl"] == config_path.parent / "d0/d0_v4_seed42.jsonl"
    manifest = json.loads(initialized["artifacts"]["manifest"].read_text(encoding="utf-8"))
    assert manifest["anchor_sample_id"] == anchor["sample_id"]
    assert manifest["sampling"]["sampling_mode"] == "v4"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash


def test_artifact_rejects_uncertified_joint_rows(tmp_path):
    runtime = _runtime()
    anchor, rows = _profile_for_validation()
    profile = {"circuit_type": runtime.circuit_type, "circuit_id": runtime.circuit_id,
               "anchor": anchor, "records": [{**_point({"sample_id": rows[0]["sample_id"],
                                                       "source": "global", "params": {"A": 0.5}}),
                                               "functional_valid": False}],
               "valid_candidate_metrics": rows, "functional_valid_samples": 1,
               "total_samples": 1}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="Joint rows"):
        _artifact_profile(path, runtime, ("current_error", "output_resistance"))


def test_no_implicit_spice_or_llm_calls(tmp_path):
    runtime = _runtime()
    no_spice = prepare_experiment(runtime.circuit_type, runtime.circuit_id,
                                  runtime=runtime, output_root=tmp_path)
    assert no_spice["status"] == "PROFILING_FAILED"
    assert no_spice["costs"]["anchor_spice_point_evaluations"] == 0
    no_llm = prepare_experiment(
        runtime.circuit_type, runtime.circuit_id, runtime=runtime,
        point_evaluator=_point, output_root=tmp_path,
        profile_samples=2, max_profile_evaluations=2, additional_samples=0,
    )
    assert no_llm["status"] == "AWAITING_LLM_APPROVAL"
    assert no_llm["costs"]["llm_calls"] == 0


def test_structured_autoconfig_anchor_and_config_failures(tmp_path):
    missing = prepare_experiment("CurrentMirror", "circuit_missing_phase3",
                                 output_root=tmp_path)
    assert missing["status"] == "AUTOCONFIG_FAILED"
    assert missing["failed_stage"] == "autoconfig"
    assert Path(missing["artifact_paths"]["result"]).is_file()

    runtime = _runtime()
    no_anchor = prepare_experiment(
        runtime.circuit_type, runtime.circuit_id, runtime=runtime,
        point_evaluator=lambda sample: {**_point(sample), "functional_valid": False},
        output_root=tmp_path, max_anchor_evaluations=3,
    )
    assert no_anchor["status"] == "NO_VALID_ANCHOR"
    assert no_anchor["failed_stage"] == "anchor"
    assert no_anchor["costs"]["anchor_spice_point_evaluations"] == 3
    assert "anchor" in no_anchor["artifact_paths"]

    bad_base = prepare_experiment(
        runtime.circuit_type, runtime.circuit_id, runtime=runtime,
        base_config_path=tmp_path / "missing.yaml", output_root=tmp_path,
    )
    assert bad_base["status"] == "CONFIG_GENERATION_FAILED"
    assert bad_base["failed_stage"] == "config"


def test_insufficient_profile_validity_is_not_sent_to_llm(tmp_path):
    runtime = _runtime()
    calls = []

    def point(sample):
        record = _point(sample)
        if sample["source"] != "nominal":
            record["functional_valid"] = False
        return record

    def llm(*args, **kwargs):
        calls.append(1)
        return json.dumps(_proposal())

    result = prepare_experiment(
        runtime.circuit_type, runtime.circuit_id, runtime=runtime,
        point_evaluator=point, llm_client=llm, output_root=tmp_path,
        profile_samples=2, max_profile_evaluations=3,
        additional_samples=1, min_valid_samples=2,
    )
    assert result["status"] == "INSUFFICIENT_VALID_SAMPLES"
    assert result["profile_counts"]["functional_valid_samples"] == 0
    assert result["costs"]["profile_spice_point_evaluations"] == 3
    assert calls == []


def test_invalid_llm_budget_returns_structured_failure(tmp_path):
    runtime = _runtime()
    result = prepare_experiment(
        runtime.circuit_type, runtime.circuit_id, runtime=runtime,
        point_evaluator=_point,
        llm_client=lambda *args, **kwargs: json.dumps(_proposal()),
        output_root=tmp_path, profile_samples=2, max_profile_evaluations=2,
        additional_samples=0, max_llm_calls=0,
    )
    assert result["status"] == "TARGET_GENERATION_FAILED"
    assert result["error_class"] == "ValueError"
    assert result["costs"]["llm_calls"] == 0


@pytest.mark.parametrize("circuit_type,circuit_id,expected", [
    ("CurrentMirror", "circuit_0469", 2),
    ("OpAmp", "circuit_0493", 3),
])
def test_existing_autoconfig_and_metric_templates_load_without_simulation(
    circuit_type, circuit_id, expected,
):
    runtime = build_active_learning_circuit_config(
        circuit_type, circuit_id, repository_root=ROOT
    )
    contract = metric_contract(
        yaml.safe_load(runtime.performance_template_path.read_text(encoding="utf-8"))
    )
    assert runtime.unresolved == ()
    assert len(contract) == expected
    assert runtime.auto_bound == len(runtime.runtime_parameters)
    assert runtime.create_evaluator() is not None
    assert runtime.create_functional_validator() is not None
