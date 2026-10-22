"""Batch 012 evidence-based AutoTarget V2 tests; provider and SPICE are mocked."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ActiveLearning import auto_target
from ActiveLearning.auto_target import (
    build_target_prompt,
    metric_contract,
    prepare_experiment,
    recommend_target,
    validate_target_recommendation,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "ActiveLearning/circuit_types/OpAmp/performance_template.yaml"
BATCH012 = Path("/home/ubuntu/batch012_qwen_manual_preparation_v1")


def _saved(circuit_id: str):
    root = BATCH012 / f"circuit_{circuit_id}"
    runs = list(root.glob("OpAmp_*/"))
    if len(runs) != 1:
        pytest.skip("frozen Batch 012 evidence is not mounted")
    return runs[0]


def _template():
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def _proposal(target_specs, support):
    contract = metric_contract(_template())
    return {
        "target_specs": {
            name: {**spec, "unit": contract[name]["unit"]}
            for name, spec in target_specs.items()
        },
        "supporting_sample_ids": [support],
        "reason": "One observed profiling row supports all three metrics",
        "confidence": "medium",
    }


def _profile(circuit_id: str):
    return json.loads((_saved(circuit_id) / "profiling.json").read_text(encoding="utf-8"))


def _target(circuit_id: str):
    return json.loads((_saved(circuit_id) / "target_generation.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("circuit_id,expected", [
    ("1671", "Anchor already meets the complete target"),
    ("1672", "No one functionally valid profiling sample meets the full target jointly"),
    ("1673", "Anchor already meets the complete target"),
    ("1674", "Anchor already meets the complete target"),
])
def test_batch012_frozen_failures_preserve_strict_joint_and_anchor_rules(circuit_id, expected):
    profile = _profile(circuit_id)
    target = _target(circuit_id)["validation"]
    support = (target["joint_supporting_sample_ids"] or
               [profile["valid_candidate_metrics"][0]["sample_id"]])[0]
    validation = validate_target_recommendation(
        _proposal(target["target_specs"], support), _template(),
        profile["anchor"], profile["valid_candidate_metrics"],
    )
    assert expected in validation["errors"]
    if circuit_id in {"1673", "1674"}:
        assert all(value is True for value in validation["anchor_satisfaction_by_metric"].values())
        assert validation["joint_supporting_sample_ids"] == []
    if circuit_id == "1672":
        assert validation["joint_supporting_sample_ids"] == []


def test_batch012_1679_real_joint_row_drives_structured_correction_and_round_audit():
    profile = _profile("1679")
    target = _target("1679")["validation"]["target_specs"]
    wrong = _proposal(target, "profile_local_0_0000")
    correct = _proposal(target, "profile_local_0_0007")
    runtime = type("Runtime", (), {
        "circuit_type": "OpAmp", "circuit_id": "circuit_1679",
        "operating_conditions": {},
    })()
    prompts = []

    def fake_llm(prompt, **kwargs):
        prompts.append(json.loads(prompt))
        kwargs["usage_log"].append({
            "stage": "auto_target", "round": len(prompts),
            "provider": kwargs["provider"], "model": kwargs["model"],
            "input_tokens": 100, "output_tokens": 25, "total_tokens": 125,
        })
        return json.dumps(wrong if len(prompts) == 1 else correct)

    outcome = recommend_target(
        runtime, _template(), profile, llm_client=fake_llm,
        provider="qwen", model="qwen-flash-2025-07-28",
    )
    assert outcome["status"] == "TARGET_VALIDATED"
    assert outcome["attempts"] == 2
    assert len(outcome["rounds"]) == 2
    first, second = outcome["rounds"]
    assert first["parsed_target_specs"] == wrong["target_specs"]
    assert first["declared_supporting_sample_ids"] == ["profile_local_0_0000"]
    assert first["actual_joint_supporting_sample_ids"] == ["profile_local_0_0007"]
    assert first["anchor_satisfaction_by_metric"] == {
        "low_frequency_open_loop_gain_db": False,
        "unity_gain_frequency_hz": True,
        "phase_margin_deg": True,
    }
    assert first["validation_errors"] == [
        "Declared supporting_sample_ids are not joint supporters"
    ]
    assert first["provider"] == second["provider"] == "qwen"
    assert first["model"] == second["model"] == "qwen-flash-2025-07-28"
    assert first["token_usage"][0]["total_tokens"] == 125
    correction = prompts[1]
    assert correction["previous_target_specs"] == wrong["target_specs"]
    assert correction["previous_declared_supporting_sample_ids"] == wrong["supporting_sample_ids"]
    assert correction["actual_joint_supporting_sample_ids"] == ["profile_local_0_0007"]
    assert correction["anchor_satisfaction_by_metric"] == first["anchor_satisfaction_by_metric"]
    assert correction["validation_errors"] == first["validation_errors"]
    witness = next(row for row in correction["relevant_profile_samples"]
                   if row["sample_id"] == "profile_local_0_0007")
    assert all(witness["target_satisfaction_by_metric"].values())
    assert second["status"] == "validated"
    assert second["declared_supporting_sample_ids"] == ["profile_local_0_0007"]


def test_batch012_1679_budget_one_keeps_invalid_round_without_correction():
    profile = _profile("1679")
    target = _target("1679")["validation"]["target_specs"]
    runtime = type("Runtime", (), {
        "circuit_type": "OpAmp", "circuit_id": "circuit_1679",
        "operating_conditions": {},
    })()
    calls = []

    def fake_llm(*args, **kwargs):
        calls.append(1)
        return json.dumps(_proposal(target, "profile_local_0_0000"))

    outcome = recommend_target(
        runtime, _template(), profile, llm_client=fake_llm,
        max_llm_calls=1,
    )
    assert outcome["status"] == "TARGET_VALIDATION_FAILED"
    assert outcome["attempts"] == len(calls) == len(outcome["rounds"]) == 1


def test_batch012_1679_prompt_uses_complete_valid_joint_rows():
    profile = _profile("1679")
    runtime = type("Runtime", (), {
        "circuit_type": "OpAmp", "circuit_id": "circuit_1679",
        "operating_conditions": {},
    })()
    prompt = json.loads(build_target_prompt(runtime, _template(), profile))
    assert len(prompt["profiling"]["joint_candidate_rows"]) == len(profile["valid_candidate_metrics"])
    assert any(row["sample_id"] == "profile_local_0_0007"
               for row in prompt["profiling"]["joint_candidate_rows"])
    rules = " ".join(prompt["rules"])
    assert "same row" in rules
    assert "anchor" in rules
    assert "insufficient_evidence" in rules


def test_explicit_insufficient_evidence_is_terminal_not_an_auto_target():
    profile = _profile("1679")
    runtime = type("Runtime", (), {
        "circuit_type": "OpAmp", "circuit_id": "circuit_1679",
        "operating_conditions": {},
    })()
    calls = []

    def fake_llm(*args, **kwargs):
        calls.append(1)
        return json.dumps({
            "insufficient_evidence": True,
            "reason": "No meaningful observed joint improvement is supported",
        })

    outcome = recommend_target(runtime, _template(), profile, llm_client=fake_llm)
    assert outcome["status"] == "TARGET_EVIDENCE_INSUFFICIENT"
    assert outcome["attempts"] == len(calls) == 1
    assert outcome["rounds"][0]["status"] == "insufficient_evidence"
    assert outcome["validation"]["valid"] is False


def test_batch012_1678_failed_anchor_never_reaches_llm(tmp_path, monkeypatch):
    saved = json.loads((_saved("1678") / "anchor.json").read_text(encoding="utf-8"))
    assert saved["status"] == "ANCHOR_BUDGET_EXHAUSTED"
    runtime = SimpleNamespace(
        circuit_type="OpAmp", circuit_id="circuit_1678",
        runtime_parameters=(SimpleNamespace(name="X", nominal=0.5),),
        performance_template_path=TEMPLATE,
    )
    monkeypatch.setattr(auto_target, "_local_samples", lambda *args: [
        {"sample_id": f"mock_local_{index}", "source": "local", "params": {"X": 0.5}}
        for index in range(args[2])
    ])
    monkeypatch.setattr(auto_target, "_global_samples", lambda *args: [
        {"sample_id": f"mock_global_{index}", "source": "global", "params": {"X": 0.5}}
        for index in range(args[1])
    ])
    records = iter(saved["records"])
    calls = []

    def fake_llm(*args, **kwargs):
        calls.append(1)
        raise AssertionError("failed anchor must not request a target")

    outcome = prepare_experiment(
        "OpAmp", "circuit_1678", runtime=runtime,
        point_evaluator=lambda sample: next(records),
        llm_client=fake_llm, output_root=tmp_path,
        max_anchor_evaluations=12, anchor_local_evaluations=6,
    )
    assert outcome["failed_stage"] == "anchor"
    assert outcome["status"] == "ANCHOR_BUDGET_EXHAUSTED"
    assert "target_generation" not in outcome["artifact_paths"]
    assert calls == []


def test_batch012_1679_failed_target_audit_persists_in_new_temp_output(tmp_path):
    profile_path = _saved("1679") / "profiling.json"
    target = _target("1679")["validation"]["target_specs"]
    runtime = SimpleNamespace(
        circuit_type="OpAmp", circuit_id="circuit_1679",
        performance_template_path=TEMPLATE, operating_conditions={},
    )
    outcome = prepare_experiment(
        "OpAmp", "circuit_1679", runtime=runtime,
        profile_artifact=profile_path,
        llm_client=lambda *args, **kwargs: json.dumps(
            _proposal(target, "profile_local_0_0000")
        ),
        output_root=tmp_path, provider="qwen", model="qwen-flash-2025-07-28",
    )
    assert outcome["status"] == "TARGET_VALIDATION_FAILED"
    assert outcome["costs"]["llm_calls"] == 2
    artifact = json.loads(Path(outcome["artifact_paths"]["target_generation"]).read_text())
    assert len(artifact["rounds"]) == 2
    assert artifact["rounds"][0]["parsed_target_specs"]["phase_margin_deg"]["unit"] == "degree"
    assert artifact["rounds"][0]["actual_joint_supporting_sample_ids"] == [
        "profile_local_0_0007"
    ]
    assert not list(tmp_path.rglob("active_learning_config.yaml"))
