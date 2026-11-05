"""Simulator-free contract tests for the two SampleHold bulk manifests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ActiveLearning.batch_runner_v2.sample_hold_bulk_production import (
    EXPECTED, inspect_manifest,
)
from ActiveLearning.onboarding import build_active_learning_circuit_config
from ActiveLearning.onboarding.errors import AutoConfigError
from ActiveLearning.onboarding.sample_hold_bulk_core import BULK_IDS, ROOT, derive_plan


@pytest.mark.parametrize("batch", ("MTV2_SH_002", "MTV2_SH_003"))
def test_manifest_exact_selection_and_isolated_roots(batch):
    spec = inspect_manifest(ROOT / "ActiveLearning/batch_runner_v2" / f"{batch}.json")
    assert tuple(row["circuit_id"][-4:] for row in spec["jobs"]) == EXPECTED[batch]
    assert all(row["max_iterations"] == 5 for row in spec["jobs"])
    assert "0259" not in EXPECTED[batch] and "0263" not in EXPECTED[batch]
    assert spec["output_root"] != "/home/ubuntu/sc_al_pilots/sc0259_al_pilot_v1"
    assert spec["preparation_root"] != spec["output_root"]


def test_source_bound_parameterized_runtime_for_every_bulk_id():
    blocked = {"circuit_0614", "circuit_0615", "circuit_0618"}
    assert len(BULK_IDS) == 15
    for circuit_id in sorted(BULK_IDS - blocked):
        plan = derive_plan(circuit_id)
        runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
        assert runtime.candidate_builder is not None
        assert runtime.runtime_parameters
        assert set(plan["source_sha256"]) >= {
            f"dataset/{circuit_id}/topology/{circuit_id}.py",
            f"dataset/{circuit_id}/topology/{circuit_id}.cir",
            f"dataset/{circuit_id}/parameters/tunable_parameters.yaml",
            f"dataset/{circuit_id}/parameters/ideal_design_space.yaml",
        }
        nominal = {row.name: row.nominal for row in runtime.runtime_parameters}
        expanded = runtime.candidate_builder.expand_candidate(nominal)
        assert set(expanded) == (plan["constructor_names"] -
                                 set(plan["fixture"].mapping.inputs) -
                                 {plan["fixture"].mapping.clock, *plan["complement_clocks"]})
        assert plan["fixture_sha256"] and plan["stimulus_sha256"]
    for circuit_id in blocked:
        with pytest.raises(AutoConfigError):
            derive_plan(circuit_id)
    with pytest.raises(AutoConfigError):
        derive_plan("circuit_0259")


def test_missing_topology_and_model_fail_closed(tmp_path):
    original = ROOT / "dataset/circuit_0617"
    copy = tmp_path / "dataset/circuit_0617"
    (copy / "topology").mkdir(parents=True)
    (copy / "simulation/parameter").mkdir(parents=True)
    (copy / "parameters").mkdir(parents=True)
    for relative in (
        "topology/circuit_0617.py", "topology/circuit_0617.cir",
        "simulation/parameter/ideal_param.yaml", "parameters/tunable_parameters.yaml",
        "parameters/ideal_design_space.yaml",
    ):
        (copy / relative).write_bytes((original / relative).read_bytes())
    with pytest.raises(AutoConfigError, match="missing"):
        derive_plan("circuit_0617", root=tmp_path / "absent")
    plan = derive_plan("circuit_0617", root=tmp_path)
    assert plan["models"]["DIODE_IDEAL"]["type"] == "D"
    archive = copy / "simulation/parameter/ideal_param.yaml"
    archive.write_text(archive.read_text(encoding="utf-8").replace(
        "DIODE_IDEAL", "UNKNOWN_DIODE_MODEL"), encoding="utf-8")
    with pytest.raises(AutoConfigError, match="ideal model definition missing"):
        derive_plan("circuit_0617", root=tmp_path)


def test_native_attempt_budget_cache_and_no_retry(tmp_path, monkeypatch):
    from ActiveLearning.onboarding import sample_hold_bulk_native as native
    from ActiveLearning import sample_hold_linux_canary

    fake_os = SimpleNamespace(name="posix", getenv=lambda name: "/lib/libngspice.so.0",
                              environ={}, fsync=os.fsync)
    monkeypatch.setattr(native, "os", fake_os)
    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="linux", executable="/usr/bin/python3"))
    monkeypatch.setattr(sample_hold_linux_canary, "_linux_environment",
                        lambda library: {"library": str(library), "mock": True})
    runtime = build_active_learning_circuit_config("SampleHold", "circuit_0260")
    calls = []

    def fake_isolated(command, *, cwd, timeout, environment):
        candidate = Path(command[command.index("--candidate") + 1])
        request = json.loads((candidate / "request.json").read_text(encoding="utf-8"))
        native._durable_json(candidate / "native_result.json", {
            "mock": False, "identity_sha256": request["identity_sha256"],
            "vector_sha256": request["vector_sha256"], "evaluation": {"value": len(calls)},
        })
        calls.append(candidate)
        return SimpleNamespace(returncode=0, stdout="", stderr="",
                               job_evidence={"contained_before_resume": True,
                                             "active_processes_at_return": 0})

    monkeypatch.setattr(native, "run_isolated", fake_isolated)
    executor = native.BulkPhysicalExecutor(runtime, tmp_path / "physical", stage="al", limit=1)
    nominal = {row.name: row.nominal for row in runtime.runtime_parameters}
    arguments = runtime.candidate_builder.expand_candidate(nominal)
    assert executor(arguments) == {"value": 0}
    assert executor(arguments) == {"value": 0}
    assert len(calls) == 1
    changed = dict(arguments)
    key = next(name for name in changed if name.startswith("W_"))
    changed[key] += 1e-9
    with pytest.raises(AutoConfigError, match="budget exhausted"):
        executor(changed)
    assert executor.execution_statistics()["physical_attempts"] == 1


def test_auto_target_uses_parameterized_samplehold_executor_without_spice():
    from ActiveLearning.auto_target import _real_point_evaluator

    runtime = build_active_learning_circuit_config("SampleHold", "circuit_0262")
    seen = []

    def fake_executor(arguments):
        seen.append(dict(arguments))
        return {"parameter_valid": True, "simulation_valid": True,
                "measurement_valid": True, "functional_valid": True,
                "metrics": {"track_end_error_v": 0.01, "sampling_error_v": 0.01,
                            "hold_isolation_error_v": 0.01,
                            "hold_droop_v_per_s": 100.0},
                "failure_reasons": [], "diagnostics": {}}

    runtime.testbench_config["_bulk_executor"] = fake_executor
    params = {row.name: row.nominal for row in runtime.runtime_parameters}
    evaluated = _real_point_evaluator(runtime)({"sample_id": "offline", "params": params})
    assert len(seen) == 1
    assert evaluated["functional_valid"] is True
    assert evaluated["sampling_error_v"] == 0.01


def test_existing_public_type_registry_unchanged():
    from ActiveLearning.onboarding.runtime_provider import resolve_runtime_provider

    for circuit_type in ("OpAmp", "CurrentMirror"):
        assert resolve_runtime_provider(circuit_type).circuit_type == circuit_type


def test_bulk_run_explicitly_abandons_sc0605_and_reuses_existing_batch(
        tmp_path, monkeypatch, capsys):
    from ActiveLearning.batch_runner_v2 import runner
    from ActiveLearning.batch_runner_v2 import sample_hold_bulk_production as bulk
    from ActiveLearning.batch_runner_v2.storage import atomic_json

    job_ids = ("sc0262", "sc0605", "sc0607", "sc0608", "sc0609")
    jobs = [{
        "job_id": job_id, "circuit_type": "SampleHold",
        "circuit_id": "circuit_" + job_id[2:], "configuration_id": job_id,
        "config_sha256": "a" * 64, "d0_sha256": "b" * 64,
        "source_sha256": "c" * 64, "max_iterations": 5, "seed": 42,
    } for job_id in job_ids]
    closure_paths = tuple(bulk._RECOVERY_SOURCE_DRIFT)
    old_manifest = {
        "schema_version": "sample_hold_bulk_runtime_v1",
        "batch_id": "MTV2_SH_002", "manifest_sha256": "d" * 64,
        "provider": "openai", "model": "offline-model",
        "credential_scope": "bytecat_ubuntu", "retry_policy": {},
        "jobs": jobs, "source_closure": {name: "1" * 64 for name in closure_paths},
        "manifest_path": str(tmp_path / "MTV2_SH_002.json"),
    }
    monkeypatch.setattr(runner, "verify_job", lambda manifest, job: None)

    def interrupted(job, _attempt):
        if job["job_id"] == "sc0605":
            raise runner.UnverifiedOrphanError("offline orphan fixture")
        assert job["job_id"] == "sc0262"
        return {"stop_reason": "max_iterations", "meets_spec": False,
                "parameter_valid": True, "simulation_valid": True,
                "functional_valid": True, "iterations_completed": 5}

    first = runner._run_batch(
        old_manifest, tmp_path, interrupted, offline_only=False,
        execution_scope="sample_hold_production")
    assert first["completed"] == 1 and first["interrupted_uncertain"] == 1
    batch_dir = tmp_path / "MTV2_SH_002"
    physical_ledger = batch_dir / "native/sc0605/physical_attempt_ledger.jsonl"
    physical_ledger.parent.mkdir(parents=True)
    physical_ledger.write_bytes(b'{"event":"reserved","attempt":1}\n')
    ledger_before = physical_ledger.read_bytes()
    state_before = json.loads((batch_dir / "status.json").read_text(encoding="utf-8"))

    current_manifest = json.loads(json.dumps(old_manifest))
    current_manifest["manifest_sha256"] = "e" * 64
    current_manifest["source_closure"] = {name: "2" * 64 for name in closure_paths}
    monkeypatch.setattr(bulk, "preflight", lambda _path: (current_manifest, {}))
    monkeypatch.setattr(bulk, "inspect_manifest", lambda _path: {
        "batch_id": "MTV2_SH_002", "output_root": str(tmp_path),
    })
    calls = []

    def completed(_manifest, job, _attempt):
        calls.append(job["job_id"])
        return {"stop_reason": "max_iterations", "meets_spec": False,
                "parameter_valid": True, "simulation_valid": True,
                "functional_valid": True, "iterations_completed": 5}

    monkeypatch.setattr(bulk, "_worker", completed)
    command = ["run", "--manifest", str(tmp_path / "MTV2_SH_002.json")]
    with pytest.raises(ValueError, match="batch identity"):
        bulk.main(command)

    # Even with sc0605 reviewed, a different orphan remains fail-closed.
    extra_orphan = json.loads(json.dumps(state_before))
    extra_orphan["jobs"]["sc0608"]["state"] = "INTERRUPTED_UNCERTAIN"
    extra_orphan["jobs"]["sc0608"]["error_code"] = "ORPHAN_UNVERIFIED"
    atomic_json(batch_dir / "status.json", extra_orphan)
    with pytest.raises(runner.UnverifiedOrphanError):
        bulk.main([*command, "--abandoned-orphan-job-id", "sc0605"])
    atomic_json(batch_dir / "status.json", state_before)

    assert bulk.main([*command, "--abandoned-orphan-job-id", "sc0605"]) == 0
    capsys.readouterr()
    assert calls == ["sc0607", "sc0608", "sc0609"]
    state_after = json.loads((batch_dir / "status.json").read_text(encoding="utf-8"))
    assert state_after["orphan_unverified"] is True
    assert state_after["jobs"]["sc0262"]["attempts"] == state_before["jobs"]["sc0262"]["attempts"]
    assert state_after["jobs"]["sc0605"]["attempts"] == state_before["jobs"]["sc0605"]["attempts"]
    assert state_after["jobs"]["sc0605"]["state"] == "INTERRUPTED_UNCERTAIN"
    assert state_after["jobs"]["sc0605"]["error_code"] == "ORPHAN_UNVERIFIED"
    assert physical_ledger.read_bytes() == ledger_before
    assert json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8")) == old_manifest

    assert bulk.main([*command, "--abandoned-orphan-job-id", "sc0605"]) == 0
    capsys.readouterr()
    assert calls == ["sc0607", "sc0608", "sc0609"]
    report = runner.report_batch("MTV2_SH_002", tmp_path)
    assert report["jobs"]["sc0605"]["error_code"] == "ORPHAN_UNVERIFIED"
    assert (report["completed"], report["interrupted_uncertain"]) == (4, 1)


def test_bulk_worker_forwards_reviewed_resume_identity(tmp_path, monkeypatch):
    from ActiveLearning.batch_runner_v2 import sample_hold_bulk_production as bulk
    from ActiveLearning import sample_hold_posix

    attempt = tmp_path / "MTV2_SH_002/jobs/sc0607/attempts/attempt_1"
    attempt.mkdir(parents=True)
    calls = []

    def fake_isolated(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=7, stdout="", stderr="offline fixture")

    monkeypatch.setattr(sample_hold_posix, "run_isolated", fake_isolated)
    digest = "d" * 64
    outcome = bulk._worker(
        {"manifest_sha256": digest, "_resume_manifest_sha256": digest,
         "manifest_path": str(tmp_path / "MTV2_SH_002.json")},
        {"job_id": "sc0607"}, attempt)
    assert outcome == {"state": "FAILED_TERMINAL", "error_code": "CHILD_EXIT_7"}
    assert calls[0][calls[0].index("--resume-manifest-sha256") + 1] == digest
