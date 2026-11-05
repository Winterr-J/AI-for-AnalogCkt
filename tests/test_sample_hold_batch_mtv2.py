"""MTV2_SH_001 offline checks; no SPICE, D0 generation or LLM requests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ActiveLearning.batch_runner_v2 import runner
from ActiveLearning.batch_runner_v2 import sample_hold_production as batch
from ActiveLearning.onboarding.sample_hold_ubuntu import OUTPUT_ROOT_ENV


def test_manifest_selects_only_reviewed_five_iteration_jobs(tmp_path):
    spec = batch.inspect_manifest()
    assert spec["batch_id"] == "MTV2_SH_001"
    assert [job["circuit_id"] for job in spec["jobs"]] == ["circuit_0259", "circuit_0263"]
    assert all(job["max_iterations"] == 5 for job in spec["jobs"])
    assert spec["continue_on_verified_failure"] is True
    assert Path(spec["output_root"]) / spec["batch_id"] != Path(
        "/home/ubuntu/sc_al_pilots/sc0259_al_pilot_v1")
    bad = {**spec, "jobs": [*spec["jobs"]]}
    bad["jobs"][-1] = {**bad["jobs"][-1], "circuit_id": "circuit_0605"}
    path = tmp_path / "blocked.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="selection"):
        batch.inspect_manifest(path)


def test_automatic_preflight_checks_both_independent_identities(monkeypatch):
    calls = []
    monkeypatch.setenv(OUTPUT_ROOT_ENV, os.getenv(OUTPUT_ROOT_ENV, ""))
    monkeypatch.setattr(batch, "os", SimpleNamespace(
        name="posix", environ=os.environ, getenv=os.getenv))
    monkeypatch.setattr(batch, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(batch, "_provider_settings", lambda *args: (
        "openai", "secret-not-recorded", "bytecat-model", "https://example.invalid/v1"))
    monkeypatch.setattr(batch, "build_active_learning_circuit_config",
                        lambda _, circuit_id: SimpleNamespace(circuit_id=circuit_id))

    def preflight(config, runtime, config_path, d0_path, targets, performance):
        calls.append(runtime.circuit_id)
        return SimpleNamespace(
            runtime_preflight={"evidence_sha256": runtime.circuit_id,
                               "execution_contract_sha256": "a" * 64},
            experiment_id=f"sc{runtime.circuit_id[-4:]}_al_pilot_five_iteration_v1",
            budget={"physical_attempts_max": 5},
        )

    monkeypatch.setattr(batch.al_main, "_validate_sample_hold_pilot", preflight)
    monkeypatch.setattr(batch, "al_pilot_contract", lambda *args: {"source_hashes": {}})
    manifest, evidence = batch.prepare()
    assert calls == ["circuit_0259", "circuit_0263"]
    assert set(evidence) == {"sc0259", "sc0263"}
    assert len({job["sample_hold_execution_id"] for job in manifest["jobs"]}) == 2
    assert all(job["max_iterations"] == 5 for job in manifest["jobs"])
    assert "secret-not-recorded" not in json.dumps(manifest)


def test_v2_scheduler_logs_failure_continues_and_resumes_without_reexecution(
        tmp_path, monkeypatch):
    jobs = [
        {"job_id": name, "circuit_type": "SampleHold", "circuit_id": cid,
         "configuration_id": name, "config_sha256": "a" * 64,
         "d0_sha256": "b" * 64, "source_sha256": "c" * 64,
         "max_iterations": 5, "seed": 42}
        for name, cid in (("sc0259", "circuit_0259"), ("sc0263", "circuit_0263"))
    ]
    manifest = {"batch_id": "MTV2_SH_001", "manifest_sha256": "d" * 64,
                "provider": "openai", "model": "bytecat-model",
                "credential_scope": "bytecat_ubuntu", "retry_policy": {}, "jobs": jobs}
    monkeypatch.setattr(runner, "verify_job", lambda manifest, job: None)
    calls = []

    def worker(job, attempt):
        calls.append(job["job_id"])
        if job["job_id"] == "sc0259":
            return {"state": "FAILED_TERMINAL", "error_code": "CHILD_EXIT_1"}
        return {"stop_reason": "max_iterations", "meets_spec": False,
                "parameter_valid": True, "simulation_valid": True,
                "functional_valid": True, "iterations_completed": 5,
                "llm_calls": 30,
                "total_tokens": {"input_tokens": 100, "output_tokens": 20,
                                 "total_tokens": 120},
                "total_timing": {"total_seconds": 12.0},
                "physical_ledger_path": str(tmp_path / "sc0263_ledger.jsonl")}

    first = runner._run_batch(manifest, tmp_path, worker, offline_only=False,
                              execution_scope="sample_hold_production")
    second = runner._run_batch(manifest, tmp_path, worker, offline_only=False,
                               execution_scope="sample_hold_production")
    assert calls == ["sc0259", "sc0263"]
    assert first["failed_terminal"] == 1 and first["completed"] == 1
    assert second["jobs"]["sc0263"]["llm_calls"] == 30
    assert second["sample_hold_usage"]["reported_total_tokens"] == 120
    assert second["sample_hold_usage"]["usage_unknown"] is True
    assert (tmp_path / "MTV2_SH_001" / "events.jsonl").is_file()


def test_job_worker_records_confirmed_child_failure_without_retry(tmp_path, monkeypatch):
    attempt = tmp_path / "MTV2_SH_001/jobs/sc0259/attempts/attempt_1"
    attempt.mkdir(parents=True)
    calls = []

    def mock_isolated(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=7, stdout="", stderr="mock failure")

    monkeypatch.setattr(batch, "run_isolated", mock_isolated)
    manifest = {"manifest_sha256": "a" * 64, "manifest_path": str(batch.DEFAULT_MANIFEST)}
    outcome = batch._worker(manifest, {"job_id": "sc0259"}, attempt)
    assert outcome == {"state": "FAILED_TERMINAL", "error_code": "CHILD_EXIT_7"}
    assert len(calls) == 1 and "child" in calls[0]
    assert json.loads((attempt / "child_reaped.json").read_text()) == {
        "attempt_id": "attempt_1", "exit_confirmed": True, "exit_code": 7,
    }
