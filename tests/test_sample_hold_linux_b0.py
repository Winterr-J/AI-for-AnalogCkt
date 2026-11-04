"""Offline-only Linux B0 contract tests. No PySpice or ngspice import."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from ActiveLearning import sample_hold_linux_canary as canary
from ActiveLearning import sample_hold_posix as posix
from ActiveLearning.onboarding import sample_hold_batch as batch
from ActiveLearning.onboarding import sample_hold_ubuntu as ubuntu
from ActiveLearning.onboarding.errors import AutoConfigError


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


def test_posix_executor_cannot_bypass_ubuntu_acceptance(tmp_path, monkeypatch):
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
    with pytest.raises(AutoConfigError, match="requires exact Ubuntu acceptance"):
        batch.SampleHoldALExecutor(
            SimpleNamespace(circuit_id="circuit_0259"), config, pilot,
        )


def test_ubuntu_acceptance_binds_exact_config_d0_and_output(
        tmp_path, monkeypatch):
    config = tmp_path / "pilot.yaml"
    d0 = tmp_path / "d0.jsonl"
    config.write_text("pilot: reviewed\n", encoding="utf-8")
    d0.write_text('{"sample_id":"d0_0"}\n', encoding="utf-8")
    acceptance = tmp_path / "acceptance.json"
    payload = {
        "schema": ubuntu.SCHEMA,
        "status": "ubuntu_physical_accepted",
        "circuit_id": "circuit_0259",
        "config_sha256": canary._sha(config),
        "d0_sha256": canary._sha(d0),
        "repository_root": str(Path(ubuntu.__file__).resolve().parents[2]),
        "python_executable": str(Path(ubuntu.sys.executable).resolve()),
        "output_root": str((tmp_path / "physical").resolve()),
        "accepted_by": "offline-test-operator",
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }
    acceptance.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ubuntu, "_is_ubuntu", lambda: True)
    monkeypatch.setenv(ubuntu.ENVIRONMENT_VARIABLE, str(acceptance.resolve()))
    runtime = SimpleNamespace(circuit_id="circuit_0259")
    loaded = ubuntu.load_ubuntu_acceptance(runtime, config, d0)
    assert loaded["acceptance_sha256"] == canary._sha(acceptance)
    d0.write_text('{"sample_id":"changed"}\n', encoding="utf-8")
    with pytest.raises(AutoConfigError, match="exact runtime"):
        ubuntu.load_ubuntu_acceptance(runtime, config, d0)
