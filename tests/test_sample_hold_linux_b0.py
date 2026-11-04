"""Offline-only Linux B0 contract tests. No PySpice or ngspice import."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from ActiveLearning import sample_hold_linux_canary as canary
from ActiveLearning import sc_0259_canary as legacy_canary
from ActiveLearning import sample_hold_posix as posix
from ActiveLearning.onboarding import sample_hold_batch as batch
from ActiveLearning.onboarding import sample_hold_ubuntu as ubuntu
from ActiveLearning.onboarding.errors import AutoConfigError
from ActiveLearning.onboarding import build_active_learning_circuit_config


def _draft(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    fake_environment = {
        "python_executable": "/usr/bin/python3.12", "python_version": "3.12.3",
        "packages": {"PySpice": "1.5", "numpy": "2.5.3", "scipy": "1.18.1",
                     "PyYAML": "6.0", "cffi": "1.17"},
        "ngspice_library": {"requested_path": str(tmp_path / "libngspice.so.0"),
                            "path": str(tmp_path / "libngspice.so.0"), "sha256": "a" * 64},
        "spice_lib_dir": None,
    }
    monkeypatch.setattr(canary, "_linux_environment", lambda path: fake_environment)
    closure_path = tmp_path / "closure.json"
    output_root = tmp_path / "runs"
    canary.capture_closure(library=tmp_path / "libngspice.so.0",
                           output_root=output_root, output=closure_path)
    return closure_path, output_root


def test_frozen_0259_fixture_and_legacy_windows_guard():
    plan = canary._frozen_fixture()
    assert plan["data"]["circuit_id"] == "circuit_0259"
    assert plan["data"]["budget"] == canary.BUDGET
    assert plan["fixture"].fixture_hash == plan["data"]["abstract_fixture_sha256"]
    assert canary.EXPERIMENT_ID != plan["data"]["experiment_id"]


def test_linux_library_alias_resolves_to_exact_real_file(tmp_path, monkeypatch):
    actual = tmp_path / "libngspice.so.0.0.0"
    alias = tmp_path / "libngspice.so.0"
    actual.write_bytes(b"mock shared library identity")
    try:
        alias.symlink_to(actual)
    except OSError:
        # Windows sandbox can deny symlink creation; still test exact bytes.
        alias.write_bytes(actual.read_bytes())
        actual = alias
    monkeypatch.setattr(canary, "_is_linux", lambda: True)
    monkeypatch.setattr(canary.ctypes, "CDLL", lambda path: object())
    monkeypatch.setattr(canary.importlib.metadata, "version", lambda package: "test-version")
    monkeypatch.setattr(canary.importlib, "import_module", lambda module: object())
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("SPICE_LIB_DIR", raising=False)
    monkeypatch.delenv("NGSPICE_LIBRARY_PATH", raising=False)
    environment = canary._linux_environment(alias)
    assert environment["ngspice_library"] == {
        "requested_path": str(alias), "path": str(actual.resolve()),
        "sha256": canary._sha(actual)}
    actual.write_bytes(b"changed")
    assert canary._linux_environment(alias)["ngspice_library"]["sha256"] != environment["ngspice_library"]["sha256"]


def test_linux_closure_draft_is_not_executable(tmp_path, monkeypatch):
    closure_path, _ = _draft(tmp_path, monkeypatch)
    assert canary.verify_closure(closure_path)["status"] == "draft_unapproved"
    with pytest.raises(canary.AdmissionError, match="closure identity"):
        canary.verify_closure(closure_path, require_frozen=True)
    with pytest.raises(canary.AdmissionError, match="acknowledgement"):
        canary.execute(closure_path=closure_path, approval_path=tmp_path / "none.json",
                       acknowledgement=False)


def test_closure_rejects_missing_or_changed_source_and_environment(tmp_path, monkeypatch):
    closure_path, _ = _draft(tmp_path, monkeypatch)
    closure = json.loads(closure_path.read_text(encoding="utf-8"))
    closure["source_files"][0]["sha256"] = "0" * 64
    closure_path.write_text(json.dumps(closure), encoding="utf-8")
    with pytest.raises(canary.AdmissionError, match="source bytes"):
        canary.verify_closure(closure_path)
    closure["source_files"][0]["sha256"] = canary._sha(canary.ROOT / closure["source_files"][0]["path"])
    closure_path.write_text(json.dumps(closure), encoding="utf-8")
    monkeypatch.setattr(canary, "_linux_environment", lambda path: {"python_version": "changed"})
    with pytest.raises(canary.AdmissionError, match="environment differs"):
        canary.verify_closure(closure_path)


def test_authorization_and_one_attempt_persistent_ledger(tmp_path, monkeypatch):
    closure_path, output_root = _draft(tmp_path, monkeypatch)
    closure = json.loads(closure_path.read_text(encoding="utf-8"))
    closure["status"] = "frozen_verified"  # Simulate separate human review/freeze.
    closure_path.write_text(json.dumps(closure), encoding="utf-8")
    approval_path = tmp_path / "approval.json"
    approval = {
        "schema": "sample_hold_0259_linux_operator_approval_v1",
        "intent": "one_real_transient", "circuit_id": "circuit_0259",
        "experiment_id": canary.EXPERIMENT_ID, "source_closure_sha256": canary._sha(closure_path),
        "fixture_file_sha256": closure["fixture_file_sha256"],
        "fixture_semantic_sha256": canary.FIXTURE_SEMANTIC_SHA256,
        "budget": canary.BUDGET, "allow_replay": False,
        "approved_by": "test_operator", "approved_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(output_root.resolve()),
    }
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    calls = []

    def failed_child(command, *, cwd, timeout, environment):
        calls.append((command, timeout, environment))
        ledger = output_root / canary.EXPERIMENT_ID / "physical_attempt_ledger.jsonl"
        assert json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])["event"] == "native_attempt_reserved"
        return subprocess.CompletedProcess(command, 2, "", "mock child failure")

    with pytest.raises(canary.AdmissionError, match="native child exited"):
        canary.execute(closure_path=closure_path, approval_path=approval_path,
                       acknowledgement=True, launcher=failed_child)
    ledger = output_root / canary.EXPERIMENT_ID / "physical_attempt_ledger.jsonl"
    assert [json.loads(line)["event"] for line in ledger.read_text(encoding="utf-8").splitlines()] == [
        "native_attempt_reserved", "native_attempt_finished"]
    with pytest.raises(canary.AdmissionError, match="no replay"):
        canary.execute(closure_path=closure_path, approval_path=approval_path,
                       acknowledgement=True, launcher=failed_child)
    assert len(calls) == 1


def test_posix_timeout_kills_grandchild_group_and_reaps(monkeypatch):
    process = Mock()
    process.pid = 4321
    process.communicate.side_effect = [subprocess.TimeoutExpired("child", 1), ("", "")]
    popen = Mock(return_value=process)
    kills = []

    def killpg(pgid, sig):
        kills.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(posix.subprocess, "Popen", popen)
    monkeypatch.setattr(posix, "os", SimpleNamespace(name="posix", killpg=killpg))
    monkeypatch.setattr(posix, "signal", SimpleNamespace(SIGKILL=9))
    with pytest.raises(subprocess.TimeoutExpired):
        posix.run_isolated(["mock-child-with-grandchild"], cwd=Path("."), timeout=1,
                           environment={})
    assert (4321, 9) in kills
    assert (4321, 0) in kills
    assert popen.call_args.kwargs["start_new_session"] is True
    assert process.communicate.call_count == 2


def test_posix_uncertain_group_is_fail_closed(monkeypatch):
    process = Mock()
    process.pid = 4321
    process.communicate.side_effect = [subprocess.TimeoutExpired("child", 1), ("", "")]
    monkeypatch.setattr(posix.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(posix, "os", SimpleNamespace(name="posix", killpg=Mock()))
    monkeypatch.setattr(posix, "signal", SimpleNamespace(SIGKILL=9))
    with pytest.raises(posix.ProcessGroupUncertain, match="still exists"):
        posix.run_isolated(["mock"], cwd=Path("."), timeout=1, environment={})


def test_posix_rejects_surviving_grandchild_after_leader_exits(monkeypatch):
    process = Mock()
    process.pid = 4321
    process.returncode = 0
    process.communicate.return_value = ("", "")
    calls = []

    def killpg(pgid, sig):
        calls.append((pgid, sig))
        if sig == 0 and (pgid, 9) in calls:
            raise ProcessLookupError

    monkeypatch.setattr(posix.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(posix, "os", SimpleNamespace(name="posix", killpg=killpg))
    monkeypatch.setattr(posix, "signal", SimpleNamespace(SIGKILL=9))
    with pytest.raises(posix.ProcessGroupUncertain, match="live descendant"):
        posix.run_isolated(["mock-parent-exits-grandchild-lives"], cwd=Path("."),
                           timeout=1, environment={})
    assert (4321, 9) in calls


def test_posix_success_returns_containment_evidence(monkeypatch):
    process = Mock()
    process.pid = 4321
    process.returncode = 0
    process.communicate.return_value = ("out", "err")

    def killpg(_pgid, sig):
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(posix.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(
        posix, "os", SimpleNamespace(name="posix", killpg=killpg),
    )
    result = posix.run_isolated(
        ["mock"], cwd=Path("."), timeout=1, environment={},
    )
    assert result.returncode == 0
    assert result.job_evidence == {
        "contained_before_resume": True,
        "active_processes_at_return": 0,
    }


def test_parameterized_launcher_maps_uncertain_posix_cleanup_fail_closed(
        monkeypatch):
    monkeypatch.setattr(batch, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        posix, "run_isolated",
        Mock(side_effect=posix.ProcessGroupUncertain("uncertain group")),
    )
    with pytest.raises(batch.UncertainProcessError, match="reaping unconfirmed"):
        batch._parameterized_native_launcher(
            ["mock"], cwd=Path("."), timeout=1, environment={},
        )


def test_posix_executor_requires_runtime_preflight_and_frozen_d0(tmp_path, monkeypatch):
    config = tmp_path / "pilot.yaml"
    config.write_text("pilot: reviewed\n", encoding="utf-8")
    pilot = {
        "enabled": True,
        "execution_id": "sc0259_al_pilot_test",
        "output_root": str((tmp_path / "physical").resolve()),
        "budget": {
            "physical_attempts_max": 1,
            "single_attempt_timeout_s": 1,
            "overall_timeout_s": 1,
            "automatic_retries": 0,
        },
    }
    monkeypatch.setattr(batch, "os", SimpleNamespace(name="posix"))
    with pytest.raises(AutoConfigError, match="requires frozen D0"):
        batch.SampleHoldALExecutor(
            SimpleNamespace(circuit_id="circuit_0259"), config, pilot,
        )


def test_linux_al_plan_ignores_windows_closure_but_verifies_real_assets(
        tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(legacy_canary, "load_plan",
                        Mock(side_effect=AssertionError("legacy Windows closure used")))
    plan = batch._al_plan(SimpleNamespace(circuit_id="circuit_0259"))
    assert plan["fixture"].fixture_hash == plan["config"]["abstract_fixture_sha256"]
    assert "closure" not in plan
    assert batch._al_plan(SimpleNamespace(circuit_id="circuit_0263"))["config"]["circuit_id"] == "circuit_0263"
    with pytest.raises(AutoConfigError, match="only circuit_0259/circuit_0263"):
        batch._al_plan(SimpleNamespace(circuit_id="circuit_0605"))
    asset = tmp_path / "topology.py"
    asset.write_bytes(b"reviewed")
    monkeypatch.setattr(ubuntu, "ROOT", tmp_path)
    with pytest.raises(AutoConfigError, match="hash mismatch"):
        ubuntu._asset({"path": "topology.py", "sha256": "0" * 64})
    asset.unlink()
    with pytest.raises(FileNotFoundError):
        ubuntu._asset({"path": "topology.py", "sha256": "0" * 64})


def test_0259_linux_mock_preflight_initializes_parameterized_al_executor(
        tmp_path, monkeypatch):
    runtime = build_active_learning_circuit_config("SampleHold", "circuit_0259")
    config = ubuntu.ROOT / "ActiveLearning/experiments/SampleHold/circuit_0259/one_iteration_pilot_v1.yaml"
    pilot = yaml.safe_load(config.read_text(encoding="utf-8"))["sample_hold_pilot"]
    d0 = ubuntu.ROOT / "ActiveLearning/InitializeD0/examples/SampleHold/circuit_0259/d0/d0_v3_seed42.jsonl"
    monkeypatch.setenv(ubuntu.OUTPUT_ROOT_ENV, str(tmp_path / "physical"))
    monkeypatch.setenv(ubuntu.LIBRARY_ENV, "/usr/lib/libngspice.so.0")
    monkeypatch.setattr(batch, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(ubuntu, "os", SimpleNamespace(
        name="posix", getenv=os.getenv, access=os.access, W_OK=os.W_OK,
    ))
    monkeypatch.setattr(ubuntu, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.setattr(ubuntu, "_linux_environment", lambda path: {
        "python_executable": sys.executable, "ngspice_library": {
            "path": str(path), "sha256": "a" * 64,
        },
    })
    evidence = ubuntu.runtime_preflight(runtime, config, d0, pilot)
    effective = {**pilot, "output_root": evidence["output_root"]}
    executor = batch.SampleHoldALExecutor(
        runtime, config, effective, runtime_preflight=evidence, d0_path=d0,
    )
    from ActiveLearning.main import main as cli_main
    cli_result = cli_main([
        "--circuit-type", "SampleHold", "--circuit-id", "circuit_0259",
        "--config", str(config), "--preflight-only",
    ])
    assert cli_result == evidence
    assert not (tmp_path / "physical").exists()
    assert executor._child_command(tmp_path / "candidate", "token")[2] == (
        "ActiveLearning.onboarding.sample_hold_batch_child"
    )
    assert evidence["evidence_sha256"] == batch._canonical_sha({
        key: value for key, value in evidence.items() if key != "evidence_sha256"
    })
    d0_copy = tmp_path / "changed_d0.jsonl"
    d0_copy.write_bytes(d0.read_bytes() + b"changed")
    executor.d0_path = d0_copy
    with pytest.raises(AutoConfigError, match="preflight changed"):
        executor._assert_identity()


def test_ubuntu_preflight_rejects_duplicate_physical_reservation(tmp_path):
    experiment = tmp_path / "sc0259_al_pilot_v1"
    experiment.mkdir()
    identity = {"execution_contract_sha256": "c" * 64,
                "circuit_id": "circuit_0259",
                "experiment_id": "sc0259_al_pilot_v1"}
    (experiment / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
    intent = {"event": "native_attempt_reserved", "attempt": 1,
              "vector_sha256": "v" * 64, "token": "token"}
    ledger = experiment / "physical_attempt_ledger.jsonl"
    ledger.write_text(json.dumps(intent) + "\n", encoding="utf-8")
    ubuntu._check_attempt_ledger(experiment, "c" * 64,
                                 "circuit_0259", "sc0259_al_pilot_v1", 5)
    ledger.write_text(json.dumps(intent) + "\n" + json.dumps({
        **intent, "attempt": 2, "token": "another",
    }) + "\n", encoding="utf-8")
    with pytest.raises(AutoConfigError, match="duplicate reservation"):
        ubuntu._check_attempt_ledger(experiment, "c" * 64,
                                     "circuit_0259", "sc0259_al_pilot_v1", 5)
