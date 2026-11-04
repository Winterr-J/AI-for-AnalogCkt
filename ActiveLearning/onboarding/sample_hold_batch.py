"""Guard one parameterized SampleHold evaluation within the existing D0 loop.

This is an internal evaluator callback, not a sampling algorithm or public CLI.
No native library is imported in the parent process.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Callable, Mapping
import uuid

import yaml

from ActiveLearning.sc_0259_canary import UncertainProcessError, _append_ledger, _native_launcher, _write_json
from ActiveLearning.sc_sample_hold_generic import ROOT, sha256_file
from ActiveLearning.onboarding.errors import AutoConfigError
from ActiveLearning.onboarding.d0_execution_windows import (
    WINDOW_SCHEMA, extension_binding, read_window, validate_extension,
)


_SCHEMA = "sc_independent_d0_batch_approval_v1"
_OPERATOR_SCHEMA = "sc_independent_d0_operator_full_v1"
_AL_OPERATOR_SCHEMA = "sc_sample_hold_al_pilot_v1"
_SOURCE_PATHS = (
    "ActiveLearning/onboarding/sample_hold.py",
    "ActiveLearning/onboarding/sample_hold_batch.py",
    "ActiveLearning/onboarding/sample_hold_batch_child.py",
    "ActiveLearning/onboarding/sample_hold_phase_a.py",
    "ActiveLearning/onboarding/sample_hold_ubuntu.py",
    "ActiveLearning/onboarding/models.py",
    "ActiveLearning/onboarding/runtime_provider.py",
    "ActiveLearning/onboarding/d0_execution_windows.py",
    "ActiveLearning/sc_sample_hold_generic.py",
    "ActiveLearning/sc_sample_hold_canary_child.py",
    "ActiveLearning/sample_hold_offline.py",
    "ActiveLearning/sample_hold_posix.py",
    "ActiveLearning/windows_job_isolation.py",
    "ActiveLearning/sc_sample_hold_canary.py",
    "ActiveLearning/sc_sample_hold_canary_child.py",
    "ActiveLearning/sc_0259_canary.py",
    "ActiveLearning/main.py",
    "ActiveLearning/active_learning.py",
    "ActiveLearning/experiment_logging.py",
    "ActiveLearning/real_evaluation/real_evaluator.py",
    "ActiveLearning/d0_initialization.py",
    "ActiveLearning/onboarding/auto_config.py",
    "ActiveLearning/onboarding/runtime_provider.py",
    "ActiveLearning/onboarding/models.py",
    "ActiveLearning/InitializeD0/validation/parameter_validator.py",
    "ActiveLearning/InitializeD0/validation/spec_validator.py",
    "ActiveLearning/InitializeD0/sampling/initial_sampler.py",
    "ActiveLearning/InitializeD0/d0_builder.py",
    "ActiveLearning/circuit_types/SampleHold/performance_template.yaml",
    "run/single_stage_amplifier/run_ideal_dataset_simulations.py",
)
_AL_SOURCE_PATHS = tuple(dict.fromkeys((
    "ActiveLearning/onboarding/sample_hold.py",
    "ActiveLearning/onboarding/sample_hold_batch.py",
    "ActiveLearning/onboarding/sample_hold_batch_child.py",
    "ActiveLearning/onboarding/sample_hold_phase_a.py",
    "ActiveLearning/onboarding/sample_hold_ubuntu.py",
    "ActiveLearning/onboarding/models.py",
    "ActiveLearning/onboarding/runtime_provider.py",
    "ActiveLearning/sc_sample_hold_generic.py",
    "ActiveLearning/sc_sample_hold_canary_child.py",
    "ActiveLearning/sc_0259_canary.py",
    "ActiveLearning/sample_hold_linux_canary.py",
    "ActiveLearning/sample_hold_offline.py",
    "ActiveLearning/sample_hold_posix.py",
    "ActiveLearning/main.py",
    "ActiveLearning/active_learning.py",
    "ActiveLearning/experiment_logging.py",
    "ActiveLearning/real_evaluation/real_evaluator.py",
    "ActiveLearning/d0_initialization.py",
    "ActiveLearning/onboarding/auto_config.py",
    "ActiveLearning/InitializeD0/validation/parameter_validator.py",
    "ActiveLearning/InitializeD0/validation/spec_validator.py",
    "ActiveLearning/InitializeD0/sampling/initial_sampler.py",
    "ActiveLearning/InitializeD0/d0_builder.py",
    "ActiveLearning/circuit_types/SampleHold/performance_template.yaml",
    "run/__init__.py",
    "run/single_stage_amplifier/__init__.py",
    "run/single_stage_amplifier/run_ideal_dataset_simulations.py",
    "conversion/__init__.py",
    "conversion/cadence_to_pyspice.py",
    "simulation_metrics/__init__.py",
    "simulation_metrics/ac.py",
    "simulation_metrics/dc.py",
    "simulation_metrics/loop_gain.py",
    "simulation_metrics/op.py",
    "simulation_metrics/transient.py",
)))


def _parameterized_native_launcher(command, *, cwd, timeout, environment):
    """Use the existing platform isolation while keeping one worker contract."""
    if os.name != "posix":
        return _native_launcher(command, cwd=cwd, timeout=timeout,
                                environment=environment)
    from ActiveLearning.sample_hold_posix import (
        ProcessGroupUncertain,
        run_isolated,
    )

    try:
        return run_isolated(command, cwd=cwd, timeout=timeout,
                            environment=environment)
    except ProcessGroupUncertain as error:
        raise UncertainProcessError(
            "SampleHold POSIX child/descendant reaping unconfirmed"
        ) from error


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def verify_frozen_candidate_plan(runtime: Any, config_path: Path,
                                 data: Mapping[str, Any]) -> list[dict]:
    """Regenerate V3 without evaluation and require the reviewed ordered vectors."""
    from ActiveLearning.InitializeD0.d0_builder import _generate_samples

    if data.get("sampling_mode") != "v3":
        raise AutoConfigError("SampleHold frozen D0 requires the V3 sampler")
    plan_name = data.get("candidate_plan_path")
    expected_sha = data.get("candidate_plan_sha256")
    if not isinstance(plan_name, str) or not isinstance(expected_sha, str):
        raise AutoConfigError("SampleHold frozen candidate plan identity is required")
    plan_path = (Path(config_path).resolve().parent / plan_name).resolve(strict=True)
    plan_root = ROOT / "ActiveLearning/docs/reference_audit"
    if not plan_path.is_relative_to(plan_root):
        raise AutoConfigError("SampleHold candidate plan escaped the reviewed artifact directory")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("circuit_id") != runtime.circuit_id or not isinstance(plan.get("candidates"), list):
        raise AutoConfigError("SampleHold candidate plan circuit/schema mismatch")
    samples = _generate_samples(
        runtime.param_template, sampling_mode="v3", seed=int(data.get("seed", 42)),
        n_samples=int(data.get("n_samples", 20)), n_local=int(data.get("n_local", 10)),
        n_global=int(data.get("n_global", 9)), local_radius=data.get("local_radius"),
        local_factor=float(data.get("local_factor", 1.25)),
        center_params=(data.get("initial_anchor") or {}).get("params"),
    )
    actual = [{"sample_id": sample["sample_id"], "params": sample["params"]}
              for sample in samples]
    identities = [_canonical_sha(row["params"]) for row in actual]
    if (len(actual) != 20 or len(set(identities)) != len(actual)
            or plan["candidates"] != actual or _canonical_sha(actual) != expected_sha):
        raise AutoConfigError("SampleHold frozen candidate plan differs from V3 runtime")
    for row in actual:
        runtime.candidate_builder.expand_candidate(row["params"])
    return actual


def _frozen_plan(runtime: Any) -> dict:
    if runtime.circuit_id == "circuit_0259":
        from ActiveLearning.sc_0259_canary import load_plan
        path = ROOT / "ActiveLearning/docs/reference_audit/SC_0259_TRANSIENT_FIXTURE_V1_SC4B3C.json"
        return load_plan(path)
    from ActiveLearning.sc_sample_hold_canary import load_plan
    from ActiveLearning.onboarding.sample_hold import _ASSETS
    path = ROOT / "ActiveLearning/docs/reference_audit" / _ASSETS[runtime.circuit_id]
    return load_plan(path)


def _al_plan(runtime: Any) -> dict:
    if os.name == "posix":
        from ActiveLearning.onboarding.sample_hold_ubuntu import load_al_plan
        return load_al_plan(runtime.circuit_id)
    return _frozen_plan(runtime)


def _source_hashes(runtime: Any) -> dict[str, str]:
    dataset = f"dataset/{runtime.circuit_id}"
    paths = (*_SOURCE_PATHS, f"{dataset}/parameters/tunable_parameters.yaml",
             f"{dataset}/parameters/ideal_design_space.yaml",
             f"{dataset}/topology/{runtime.circuit_id}.py",
             f"{dataset}/topology/{runtime.circuit_id}.cir",
             f"{dataset}/simulation/parameter/ideal_param.yaml")
    return {name: sha256_file(ROOT / name) for name in paths}


def approval_binding(runtime: Any, config_path: Path, output_root: Path,
                     experiment_id: str, budget: Mapping[str, Any]) -> dict:
    """Exact non-secret fields an operator would bind in a future approval."""
    plan = _frozen_plan(runtime)
    import yaml
    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))["data"]
    verify_frozen_candidate_plan(runtime, config_path, data)
    return {
        "schema": _SCHEMA,
        "circuit_id": runtime.circuit_id,
        "experiment_id": experiment_id,
        "config_sha256": sha256_file(config_path),
        "parameter_space_sha256": _canonical_sha(runtime.param_template),
        "candidate_plan_sha256": data["candidate_plan_sha256"],
        "fixture_file_sha256": plan["file_sha256"],
        "fixture_sha256": plan["fixture"].fixture_hash,
        "stimulus_sha256": plan["config"]["stimulus_sha256"],
        "source_hashes": _source_hashes(runtime),
        "output_root": str(output_root),
        "python_executable": str(Path(sys.executable).resolve()),
        "budget": dict(budget),
    }


def operator_contract(runtime: Any, config_path: Path, output_root: Path,
                      experiment_id: str, budget: Mapping[str, Any]) -> dict:
    """Bind an explicitly configured, operator-started full batch to frozen assets."""
    return {**approval_binding(runtime, config_path, output_root, experiment_id, budget),
            "schema": _OPERATOR_SCHEMA}


def al_pilot_contract(runtime: Any, config_path: Path, output_root: Path,
                      execution_id: str, budget: Mapping[str, Any]) -> dict:
    """Bind one explicitly configured AL pilot to the reviewed native worker."""
    plan = _al_plan(runtime)
    al_paths = (
        "ActiveLearning/active_learning.py",
        "ActiveLearning/experiment_logging.py",
        "ActiveLearning/real_evaluation/real_evaluator.py",
        "ActiveLearning/inverse_design/inverse_designer.py",
        "ActiveLearning/inverse_design/prompt_builder.py",
        "ActiveLearning/inverse_design/prompts/circuit_types/sample_hold_v1.txt",
        "ActiveLearning/performance_prediction/performance_predictor.py",
        "ActiveLearning/performance_prediction/prompts/sample_hold_compact_v1.txt",
        "ActiveLearning/acquisition/acquisition.py",
        "ActiveLearning/data_expansion/data_expander.py",
    )
    return {
        "schema": _AL_OPERATOR_SCHEMA,
        "circuit_id": runtime.circuit_id,
        "execution_id": execution_id,
        "config_sha256": sha256_file(config_path),
        "parameter_space_sha256": _canonical_sha(runtime.param_template),
        "fixture_file_sha256": plan["file_sha256"],
        "fixture_sha256": plan["fixture"].fixture_hash,
        "stimulus_sha256": plan["config"]["stimulus_sha256"],
        "source_hashes": {
            **({name: sha256_file(ROOT / name) for name in (
                *_AL_SOURCE_PATHS,
                f"dataset/{runtime.circuit_id}/parameters/tunable_parameters.yaml",
                f"dataset/{runtime.circuit_id}/parameters/ideal_design_space.yaml",
                f"dataset/{runtime.circuit_id}/topology/{runtime.circuit_id}.py",
                f"dataset/{runtime.circuit_id}/topology/{runtime.circuit_id}.cir",
                f"dataset/{runtime.circuit_id}/simulation/parameter/ideal_param.yaml",
            )} if os.name == "posix" else _source_hashes(runtime)),
            **{name: sha256_file(ROOT / name) for name in al_paths},
        },
        "output_root": str(output_root),
        "python_executable": str(Path(sys.executable).resolve()),
        "budget": dict(budget),
    }


def validate_approval(runtime: Any, config_path: Path, output_root: Path,
                      experiment_id: str, budget: Mapping[str, Any],
                      approval_path: Path, *, extension_path: Path | None = None,
                      experiment_path: Path | None = None,
                      checkpoint_path: Path | None = None,
                      review_path: Path | None = None) -> dict:
    path = approval_path.resolve(strict=True)
    if path.is_relative_to(ROOT):
        raise AutoConfigError("D0 approval must remain outside the repository")
    actual = json.loads(path.read_text(encoding="utf-8"))
    expected = approval_binding(runtime, config_path, output_root, experiment_id, budget)
    if (set(actual) != set(expected) | {"approved_by", "approved_at"}
            or any(actual.get(key) != value for key, value in expected.items()
                   if key != "source_hashes" or extension_path is None)):
        raise AutoConfigError("D0 approval does not bind exact assets, config and budget")
    if not isinstance(actual["approved_by"], str) or not actual["approved_by"].strip():
        raise AutoConfigError("D0 approval requires operator identity")
    try:
        signed = datetime.fromisoformat(actual["approved_at"])
    except (TypeError, ValueError) as error:
        raise AutoConfigError("D0 approval timestamp invalid") from error
    if signed.tzinfo is None or signed.utcoffset() is None:
        raise AutoConfigError("D0 approval timestamp requires timezone")
    if extension_path is not None:
        extension_path = extension_path.resolve(strict=True)
        if (extension_path.is_relative_to(ROOT)
                or extension_path != path.with_name(experiment_id + "_deadline_extension_v1.json")
                or experiment_path is None or checkpoint_path is None or review_path is None):
            raise AutoConfigError("D0 extension path or evidence roots invalid")
        experiment_path = Path(experiment_path).resolve()
        if experiment_path != Path(output_root).resolve() / experiment_id:
            raise AutoConfigError("D0 extension experiment root mismatch")
        proposed = json.loads(extension_path.read_text(encoding="utf-8"))
        ledger_size = (proposed.get("ledger_snapshot") or {}).get("bytes")
        checkpoint_size = (proposed.get("checkpoint_snapshot") or {}).get("bytes")
        if not (experiment_path / "execution_window_v1.json").exists():
            # At first activation the approval must describe the *current*
            # evidence, not a selected older prefix that hides spent attempts.
            if (ledger_size != (experiment_path / "physical_attempt_ledger.jsonl").stat().st_size
                    or checkpoint_size != Path(checkpoint_path).stat().st_size):
                raise AutoConfigError("D0 extension omits current ledger or checkpoint evidence")
        binding = extension_binding(
            circuit_id=runtime.circuit_id, experiment_id=experiment_id,
            approval_path=path, original_approval=actual,
            source_hashes=expected["source_hashes"],
            identity_path=experiment_path / "identity.json",
            ledger_path=experiment_path / "physical_attempt_ledger.jsonl",
            checkpoint_path=Path(checkpoint_path), review_path=Path(review_path),
            candidate_plan_path=(Path(config_path).resolve().parent /
                                 yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
                                 ["data"]["candidate_plan_path"]).resolve(strict=True),
            candidate_plan_sha256=expected["candidate_plan_sha256"],
            budget=budget, ledger_bytes=ledger_size,
            checkpoint_bytes=checkpoint_size,
        )
        validate_extension(extension_path, expected=binding, now=time.time())
    return actual


def verify_completed_d0(config: Mapping[str, Any], d0_path: Path) -> None:
    """Read-only batch completion check; no approval or native import required."""
    data = config["data"]
    completion = (Path(data["batch_output_root"]).resolve()
                  / data["batch_experiment_id"] / "batch_completion.json")
    if not completion.is_file():
        raise AutoConfigError("SampleHold independent D0 batch completion evidence missing")
    record = json.loads(completion.read_text(encoding="utf-8"))
    csv_path = d0_path.with_suffix(".csv")
    initialization = d0_path.with_name(d0_path.stem + "_initialization.json")
    contract_key = ("execution_contract_sha256" if data.get("batch_execution_mode") == "operator_full"
                    else "approval_sha256")
    if (record.get("status") != "complete"
            or record.get("experiment_id") != data["batch_experiment_id"]
            or record.get("records") != data["n_samples"]
             or record.get("physical_attempts") > data["batch_budget"]["physical_attempts_max"]
             or not isinstance(record.get(contract_key), str)
            or record.get("d0_jsonl_sha256") != sha256_file(d0_path)
            or record.get("d0_csv_sha256") != sha256_file(csv_path)
            or record.get("d0_initialization_sha256") != sha256_file(initialization)):
        raise AutoConfigError("SampleHold independent D0 batch completion mismatch")


class SampleHoldBatchExecutor:
    """Per-vector physical ledger and idempotent cache under one D0 identity."""

    def __init__(self, runtime: Any, config_path: Path, data: Mapping[str, Any],
                 *, launcher: Callable = _native_launcher):
        self.runtime = runtime
        self.config_path = Path(config_path).resolve(strict=True)
        self.launcher = launcher
        experiment_id = data.get("batch_experiment_id")
        root = Path(data.get("batch_output_root") or "")
        budget = data.get("batch_budget")
        review_gate = data.get("batch_review_gate")
        approval_name = data.get("batch_approval_file")
        self.operator_full = data.get("batch_execution_mode") == "operator_full"
        if data.get("batch_execution_mode") not in (None, "approval_review", "operator_full"):
            raise AutoConfigError("unsupported SampleHold D0 execution mode")
        if (not isinstance(experiment_id, str)
                or re.fullmatch(r"sc[0-9]{4}_independent_d0_[a-z0-9_]+", experiment_id) is None
                or not root.is_absolute() or root.resolve().is_relative_to(ROOT)
                or not isinstance(budget, dict)
                or set(budget) != {"physical_attempts_max", "single_attempt_timeout_s",
                                   "overall_timeout_s", "automatic_retries"}
                or type(budget["physical_attempts_max"]) is not int
                or budget["physical_attempts_max"] < 1
                or budget["physical_attempts_max"] > data.get("n_samples", 20)
                or type(budget["single_attempt_timeout_s"]) not in (int, float)
                or type(budget["overall_timeout_s"]) not in (int, float)
                or not math.isfinite(budget["single_attempt_timeout_s"])
                or not math.isfinite(budget["overall_timeout_s"])
                or budget["single_attempt_timeout_s"] <= 0
                or budget["overall_timeout_s"] <= 0
                  or type(budget["automatic_retries"]) is not int
                  or budget["automatic_retries"] != 0):
            raise AutoConfigError("invalid independent SampleHold D0 batch identity/budget")
        if experiment_id[2:6] != runtime.circuit_id[-4:]:
            raise AutoConfigError("D0 batch identity differs from circuit")
        if self.operator_full:
            if approval_name is not None or review_gate is not None:
                raise AutoConfigError("operator-full D0 cannot carry approval/review files")
            if (budget["physical_attempts_max"] != data.get("n_samples")
                    or data.get("n_samples") != 20):
                raise AutoConfigError("operator-full D0 requires exactly 20 budgeted candidates")
        elif (not approval_name or not isinstance(review_gate, dict)
              or set(review_gate) != {"pilot_candidates", "continuation_review_file"}
              or review_gate["pilot_candidates"] != 1
              or not isinstance(review_gate["continuation_review_file"], str)):
            raise AutoConfigError("separate D0 batch approval/review required")
        self.root = root.resolve()
        self.experiment_id = experiment_id
        self.budget = budget
        self.contract_key = ("execution_contract_sha256" if self.operator_full
                             else "approval_sha256")
        self.pilot_candidates = 0
        self.continuation_review_path = None
        if not self.operator_full:
            self.pilot_candidates = review_gate["pilot_candidates"]
            configured_review_path = Path(review_gate["continuation_review_file"])
            self.continuation_review_path = configured_review_path.resolve()
            if (not configured_review_path.is_absolute()
                    or self.continuation_review_path.is_relative_to(ROOT)):
                raise AutoConfigError("D0 continuation review must remain outside the repository")
        self.continuation_review_sha: str | None = None
        self.candidate_plan_sha = data.get("candidate_plan_sha256")
        self.candidate_plan_path = (
            self.config_path.parent / data["candidate_plan_path"]).resolve(strict=True)
        self.expected_records = int(data.get("n_samples", 20))
        if self.operator_full:
            self.pilot_candidates = self.expected_records
        self.checkpoint_path = ((self.config_path.parent / data["d0_path"]).resolve()
                                if isinstance(data.get("d0_path"), str) else None)
        self.approval_path = None
        self.extension_path = None
        self.experiment = self.root / experiment_id
        self.ledger = self.experiment / "physical_attempt_ledger.jsonl"
        self.window_path = self.experiment / "execution_window_v1.json"
        if self.operator_full:
            self.approval = None
            self.approval_sha = _canonical_sha(operator_contract(
                runtime, self.config_path, self.root, experiment_id, budget))
            self.active_extension = False
            self.extension_sha = None
            self.extension = None
        else:
            configured_approval_path = Path(approval_name)
            if not configured_approval_path.is_file():
                raise AutoConfigError("separate D0 batch approval file required")
            self.approval_path = configured_approval_path.resolve(strict=True)
            self.extension_path = self.approval_path.with_name(
                experiment_id + "_deadline_extension_v1.json")
            self.active_extension = self.extension_path.is_file()
            self.approval = validate_approval(runtime, self.config_path, self.root,
                                              experiment_id, budget, self.approval_path,
                                              **self._extension_validation_kwargs())
            self.approval_sha = sha256_file(self.approval_path)
            self.extension_sha = (sha256_file(self.extension_path)
                                  if self.active_extension else None)
            self.extension = (json.loads(self.extension_path.read_text(encoding="utf-8"))
                              if self.active_extension else None)
        self.lock = self.root / (experiment_id + ".lock")

    def _extension_validation_kwargs(self) -> dict:
        if not self.active_extension:
            return {}
        if self.checkpoint_path is None:
            raise AutoConfigError("D0 extension requires an explicit checkpoint path")
        return {
            "extension_path": self.extension_path,
            "experiment_path": self.experiment,
            "checkpoint_path": self.checkpoint_path,
            "review_path": self.continuation_review_path,
        }

    def _assert_identity(self) -> None:
        if self.operator_full:
            if _canonical_sha(operator_contract(
                    self.runtime, self.config_path, self.root, self.experiment_id,
                    self.budget)) != self.approval_sha:
                raise AutoConfigError("D0 operator execution contract changed")
        else:
            validate_approval(self.runtime, self.config_path, self.root, self.experiment_id,
                              self.budget, self.approval_path,
                              **self._extension_validation_kwargs())
            if sha256_file(self.approval_path) != self.approval_sha:
                raise AutoConfigError("D0 approval changed during execution")
        if (self.active_extension
                and sha256_file(self.extension_path) != self.extension_sha):
            raise AutoConfigError("D0 execution-window extension changed")
        if (not self.operator_full and not self.active_extension and self.extension_path.exists()):
            raise AutoConfigError("D0 extension appeared after executor construction")
        if (self.continuation_review_sha is not None
                and (not self.continuation_review_path.is_file()
                     or sha256_file(self.continuation_review_path)
                     != self.continuation_review_sha)):
            raise AutoConfigError("D0 continuation review changed during execution")
        if self.launcher is _native_launcher and self.runtime.circuit_id != "circuit_0259":
            from ActiveLearning.sc_sample_hold_canary import _native_environment_verified

            if not _native_environment_verified(_frozen_plan(self.runtime)["closure"]):
                raise AutoConfigError("D0 native environment differs from frozen closure")

    def _initialize_or_verify(self) -> dict:
        self._assert_identity()
        if not self.experiment.exists():
            self.experiment.mkdir()
            identity = {self.contract_key: self.approval_sha,
                        "experiment_id": self.experiment_id,
                        "circuit_id": self.runtime.circuit_id,
                        "started_unix_s": time.time()}
            _write_json(self.experiment / "identity.json", identity)
            return identity
        identity = json.loads((self.experiment / "identity.json").read_text(encoding="utf-8"))
        if (identity.get(self.contract_key) != self.approval_sha
                or identity.get("experiment_id") != self.experiment_id
                or identity.get("circuit_id") != self.runtime.circuit_id):
            raise AutoConfigError("existing D0 experiment identity mismatch")
        return identity

    def _recover_reaped_success(self, candidate: Path, arguments: Mapping[str, Any],
                                vector_sha: str, events: list[dict]) -> dict:
        """Complete a parent-crash gap only when durable reaping proof exists."""
        status_path = candidate / "process_status.json"
        result_path = candidate / "native_result.json"
        if not status_path.is_file() or not result_path.is_file():
            raise AutoConfigError("candidate already attempted; no automatic retry")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        evidence = status.get("job_evidence") or {}
        if (status.get("returncode") != 0
                or evidence.get("contained_before_resume") is not True
                or evidence.get("active_processes_at_return") != 0):
            raise AutoConfigError("candidate process reaping not proven")
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        plan = (_al_plan(self.runtime) if self.contract_key == "execution_contract_sha256"
                and isinstance(self, SampleHoldALExecutor) else _frozen_plan(self.runtime))
        expected_mock = self.launcher not in {_native_launcher, _parameterized_native_launcher}
        if (raw.get("vector_sha256") != vector_sha
                or raw.get("mock") is not expected_mock
                or raw.get("fixture_sha256") != plan["fixture"].fixture_hash
                or raw.get("stimulus_sha256") != plan["config"]["stimulus_sha256"]):
            raise AutoConfigError("recovered candidate result provenance mismatch")
        evaluation = dict(raw["evaluation"])
        if expected_mock:
            evaluation["diagnostics"] = {**evaluation.get("diagnostics", {}),
                                         "mock_only": True}
        cached = candidate / "evaluation.json"
        if cached.exists():
            record = json.loads(cached.read_text(encoding="utf-8"))
            if (record.get("vector_sha256") != vector_sha
                    or record.get("arguments") != arguments
                    or record.get("evaluation") != evaluation):
                raise AutoConfigError("partial cached candidate identity mismatch")
        else:
            _write_json(cached, {"vector_sha256": vector_sha,
                                 "arguments": dict(arguments), "evaluation": evaluation})
        if any(row.get("event") == "native_attempt_finished"
               and row.get("vector_sha256") == vector_sha for row in events):
            raise AutoConfigError("candidate completion ledger conflicts with recovery")
        reservations = [row for row in events if row.get("event") == "native_attempt_reserved"
                        and row.get("vector_sha256") == vector_sha]
        if len(reservations) != 1:
            raise AutoConfigError("candidate reservation missing or duplicated")
        _append_ledger(self.ledger, {"event": "native_attempt_finished",
                                     "attempt": reservations[0]["attempt"],
                                     "vector_sha256": vector_sha, "outcome": "complete",
                                     "evaluation_sha256": sha256_file(cached),
                                     "recovered_after_reaping": True, "unix_s": time.time()})
        return evaluation

    def _checkpoint_identity(self, record: Mapping[str, Any]) -> tuple[str, str]:
        """Reconcile valid records with native cache and invalid ones with validation."""
        from ActiveLearning.onboarding.sample_hold import SampleHoldFunctionalValidator
        from ActiveLearning.InitializeD0.validation.parameter_validator import validate_parameters

        params = record.get("params") or {}
        validation_result = validate_parameters(params, self.runtime.param_template)
        if not validation_result["parameter_valid"]:
            status = record.get("status") or {}
            if (status.get("parameter_valid") is not False
                    or status.get("simulation_valid") is not None
                    or status.get("functional_valid") is not False
                    or status.get("functional_status") != "invalid"
                    or status.get("functional_feasible") is not False
                    or status.get("meets_spec") is not None
                    or record.get("failure_reasons") != ["parameter_invalid"]
                    or record.get("diagnostics") != {
                        "parameter_violations": list(validation_result["violations"])}
                    or any(value is not None for value in (record.get("metrics") or {}).values())
                    or record.get("functional_checks") != {}):
                raise AutoConfigError("D0 parameter-invalid checkpoint status mismatch")
            return (_canonical_sha(params), _canonical_sha({
                "parameter_invalid": params,
                "violations": list(validation_result["violations"])}))

        arguments = self.runtime.candidate_builder.expand_candidate(params)
        vector_sha = _canonical_sha(arguments)
        cached = self.experiment / "candidates" / vector_sha[:16] / "evaluation.json"
        if not cached.is_file():
            raise AutoConfigError("D0 checkpoint record has no durable candidate evaluation")
        cache = json.loads(cached.read_text(encoding="utf-8"))
        if cache.get("vector_sha256") != vector_sha or cache.get("arguments") != arguments:
            raise AutoConfigError("D0 checkpoint candidate cache identity mismatch")
        validation = SampleHoldFunctionalValidator().validate(
            record["params"], cache["evaluation"], target_specs=None)
        expected_status = dict(validation["status"])
        expected_status["functional_feasible"] = expected_status.get("functional_valid")
        if (record.get("metrics") != validation["metrics"]
                or record.get("status") != expected_status
                or record.get("failure_reasons") != validation["failure_reasons"]
                or record.get("diagnostics") != validation["diagnostics"]
                or record.get("functional_checks") != validation["functional_checks"]):
            raise AutoConfigError("D0 checkpoint record differs from durable evaluation")
        return vector_sha, sha256_file(cached)

    def _append_persisted_event(self, record: Mapping[str, Any], destination: Path,
                                *, recovered: bool = False) -> None:
        vector_sha, evaluation_sha = self._checkpoint_identity(record)
        _append_ledger(self.ledger, {
            "event": "d0_record_persisted",
            "sample_id": record["sample_id"],
            "vector_sha256": vector_sha,
            "evaluation_sha256": evaluation_sha,
            "record_sha256": _canonical_sha(record),
            "checkpoint_jsonl_sha256": sha256_file(destination),
            "recovered_after_checkpoint_gap": recovered,
            "unix_s": time.time(),
        })

    def persist_record(self, record: Mapping[str, Any], destination: Path) -> None:
        """Append one authentic record durably before another candidate may launch."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_index = int(str(record.get("sample_id", "")).rsplit("_", 1)[-1])
        existing = (destination.read_text(encoding="utf-8").splitlines()
                    if destination.exists() else [])
        if expected_index != len(existing):
            raise AutoConfigError("D0 checkpoint append order differs from frozen plan")
        self._checkpoint_identity(record)
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._append_persisted_event(record, destination)

    def validate_resume_records(self, records: list[dict], destination: Path) -> None:
        """Verify the checkpoint prefix and reconcile only a proven append/event gap."""
        if len(records) > self.expected_records:
            raise AutoConfigError("D0 checkpoint exceeds frozen candidate count")
        plan = json.loads(self.candidate_plan_path.read_text(encoding="utf-8"))
        candidates = plan["candidates"]
        events = ([json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]
                  if self.ledger.exists() else [])
        persisted = [row for row in events if row.get("event") == "d0_record_persisted"]
        for index, record in enumerate(records):
            if (index >= len(candidates)
                    or record.get("sample_id") != candidates[index]["sample_id"]
                    or record.get("params") != candidates[index]["params"]):
                raise AutoConfigError("D0 checkpoint is not an ordered frozen-plan prefix")
            vector_sha, evaluation_sha = self._checkpoint_identity(record)
            expected = {
                "sample_id": record["sample_id"], "vector_sha256": vector_sha,
                "evaluation_sha256": evaluation_sha,
                "record_sha256": _canonical_sha(record),
            }
            if index < len(persisted):
                if any(persisted[index].get(key) != value for key, value in expected.items()):
                    raise AutoConfigError("D0 checkpoint persistence ledger mismatch")
            elif index == len(persisted) and index == len(records) - 1:
                self._append_persisted_event(record, Path(destination), recovered=True)
                persisted.append({**expected, "event": "d0_record_persisted",
                                  "checkpoint_jsonl_sha256": sha256_file(destination)})
            else:
                raise AutoConfigError("D0 checkpoint persistence ledger is incomplete")
        if len(persisted) != len(records):
            raise AutoConfigError("D0 checkpoint persistence ledger has extra records")
        if records and persisted[-1].get("checkpoint_jsonl_sha256") != sha256_file(destination):
            raise AutoConfigError("D0 checkpoint bytes differ from persistence ledger")

    def continuation_review_binding(self, records: list[dict]) -> dict:
        if self.operator_full:
            raise AutoConfigError("operator-full D0 has no continuation review")
        if len(records) < self.pilot_candidates:
            raise AutoConfigError("D0 pilot record is not yet available for review")
        pilot = records[:self.pilot_candidates]
        return {
            "schema": "sc_independent_d0_pilot_review_v1",
            "decision": "continue",
            "circuit_id": self.runtime.circuit_id,
            "experiment_id": self.experiment_id,
            "batch_approval_sha256": self.approval_sha,
            "candidate_plan_sha256": self.candidate_plan_sha,
            "pilot_sample_ids": [record["sample_id"] for record in pilot],
            "pilot_records_sha256": _canonical_sha(pilot),
        }

    def allowed_new_records(self, records: list[dict]) -> int:
        if self.operator_full:
            return max(0, self.expected_records - len(records))
        if len(records) < self.pilot_candidates:
            return self.pilot_candidates - len(records)
        if len(records) >= self.expected_records:
            return 0
        if not self.continuation_review_path.is_file():
            return 0
        review = json.loads(self.continuation_review_path.read_text(encoding="utf-8"))
        expected = self.continuation_review_binding(records)
        if (set(review) != set(expected) | {"reviewed_by", "reviewed_at"}
                or any(review.get(key) != value for key, value in expected.items())
                or not isinstance(review.get("reviewed_by"), str)
                or not review["reviewed_by"].strip()):
            raise AutoConfigError("D0 continuation review does not bind the pilot record")
        try:
            reviewed_at = datetime.fromisoformat(review["reviewed_at"])
        except (TypeError, ValueError) as error:
            raise AutoConfigError("D0 continuation review timestamp invalid") from error
        if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
            raise AutoConfigError("D0 continuation review timestamp requires timezone")
        self.continuation_review_sha = sha256_file(self.continuation_review_path)
        return self.expected_records - len(records)

    def _child_command(self, candidate: Path, token: str) -> list[str]:
        command = [
            sys.executable, "-m", "ActiveLearning.onboarding.sample_hold_batch_child",
            "--config", str(self.config_path),
            "--candidate", str(candidate), "--token", token,
        ]
        if not self.operator_full:
            command.extend(["--approval", str(self.approval_path)])
        if self.active_extension:
            command.extend(["--extension", str(self.extension_path)])
        return command

    def __call__(self, arguments: Mapping[str, Any]) -> dict:
        """Called by the shared D0 evaluator after parameter validation."""
        self._assert_identity()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.lock.mkdir()
        except FileExistsError as error:
            raise AutoConfigError("D0 batch lock exists; concurrent/orphaned execution") from error
        release_lock = True
        try:
            identity = self._initialize_or_verify()
            vector_sha = _canonical_sha(arguments)
            candidate = self.experiment / "candidates" / vector_sha[:16]
            cached = candidate / "evaluation.json"
            events = ([json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]
                      if self.ledger.exists() else [])
            if cached.exists():
                record = json.loads(cached.read_text(encoding="utf-8"))
                if record.get("vector_sha256") != vector_sha or record.get("arguments") != arguments:
                    raise AutoConfigError("cached candidate vector identity mismatch")
                if any(row.get("event") == "native_attempt_finished"
                           and row.get("vector_sha256") == vector_sha
                           and row.get("outcome") == "complete"
                           and row.get("evaluation_sha256") == sha256_file(cached)
                           for row in events):
                    return record["evaluation"]
                return self._recover_reaped_success(candidate, arguments, vector_sha, events)
            if candidate.exists():
                return self._recover_reaped_success(candidate, arguments, vector_sha, events)
            attempts = sum(row.get("event") == "native_attempt_reserved" for row in events)
            if attempts >= self.budget["physical_attempts_max"]:
                raise AutoConfigError("D0 physical attempt budget exhausted")
            if not self.operator_full and attempts >= self.pilot_candidates and self.continuation_review_sha is None:
                raise AutoConfigError("D0 pilot review required before continuation")
            if self.active_extension:
                window = read_window(
                    self.window_path, self.ledger,
                    extension_sha256=self.extension_sha,
                    continuation_window_s=self.extension["execution_window_policy"]
                    ["continuation_window_s"],
                )
                if window is None:
                    expires = datetime.fromisoformat(self.extension["activation_not_after"])
                    if time.time() > expires.timestamp():
                        raise TimeoutError("D0 extension activation validity expired")
                    remaining = self.extension["execution_window_policy"]["continuation_window_s"]
                else:
                    remaining = window["deadline_unix_s"] - time.time()
            else:
                remaining = self.budget["overall_timeout_s"] - (time.time() - identity["started_unix_s"])
            timeout = min(self.budget["single_attempt_timeout_s"], remaining)
            if timeout <= 0:
                raise TimeoutError("D0 overall deadline exhausted")
            candidate.mkdir(parents=True)
            token = uuid.uuid4().hex
            intent = {"event": "native_attempt_reserved", "attempt": attempts + 1,
                      "vector_sha256": vector_sha, "token": token,
                      "unix_s": time.time()}
            _write_json(candidate / "input.json", {"arguments": dict(arguments),
                                                    "vector_sha256": vector_sha,
                                                    self.contract_key: self.approval_sha})
            _append_ledger(self.ledger, intent)  # Durable before child launch.
            _write_json(candidate / "launch_intent.json", intent)
            command = self._child_command(candidate, token)
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            try:
                if self.active_extension:
                    window = read_window(
                        self.window_path, self.ledger,
                        extension_sha256=self.extension_sha,
                        continuation_window_s=self.extension["execution_window_policy"]
                        ["continuation_window_s"],
                    )
                    if window is None:
                        now = time.time()
                        if now > datetime.fromisoformat(
                                self.extension["activation_not_after"]).timestamp():
                            raise TimeoutError("D0 extension activation validity expired")
                        window = {
                            "schema": WINDOW_SCHEMA,
                            "extension_sha256": self.extension_sha,
                            "started_unix_s": now,
                            "deadline_unix_s": now + self.extension["execution_window_policy"]
                            ["continuation_window_s"],
                        }
                        # Exclusive, durable creation: an existing activation is
                        # evidence to inspect, never a clock to overwrite.
                        with self.window_path.open("xb") as stream:
                            stream.write((json.dumps(window, sort_keys=True) + "\n").encode("utf-8"))
                            stream.flush()
                            os.fsync(stream.fileno())
                        _append_ledger(self.ledger, {"event": "execution_window_activated",
                                                     **{key: window[key] for key in (
                                                         "extension_sha256", "started_unix_s",
                                                         "deadline_unix_s")}})
                    timeout = min(self.budget["single_attempt_timeout_s"],
                                  window["deadline_unix_s"] - time.time())
                    if timeout <= 0:
                        raise TimeoutError("D0 continuation window exhausted")
                result = self.launcher(command, cwd=ROOT, timeout=timeout,
                                       environment=environment)
                evidence = getattr(result, "job_evidence", None)
                if (not isinstance(evidence, dict)
                        or evidence.get("contained_before_resume") is not True
                        or evidence.get("active_processes_at_return") != 0):
                    raise UncertainProcessError("D0 child/descendant reaping unconfirmed")
                _write_json(candidate / "process_status.json", {
                    "returncode": result.returncode, "job_evidence": evidence,
                    "stdout": result.stdout, "stderr": result.stderr})
                if result.returncode != 0:
                    raise AutoConfigError(f"D0 native child exited {result.returncode}")
                raw = json.loads((candidate / "native_result.json").read_text(encoding="utf-8"))
                plan = (_al_plan(self.runtime) if isinstance(self, SampleHoldALExecutor)
                        else _frozen_plan(self.runtime))
                expected_mock = self.launcher not in {
                    _native_launcher, _parameterized_native_launcher,
                }
                if (raw.get("vector_sha256") != vector_sha or raw.get("mock") is not expected_mock
                        or raw.get("fixture_sha256") != plan["fixture"].fixture_hash
                        or raw.get("stimulus_sha256") != plan["config"]["stimulus_sha256"]):
                    raise AutoConfigError("D0 native result provenance mismatch")
                evaluation = raw["evaluation"]
                if expected_mock:
                    evaluation["diagnostics"] = {**evaluation.get("diagnostics", {}),
                                                 "mock_only": True}
                _write_json(cached, {"vector_sha256": vector_sha,
                                     "arguments": dict(arguments), "evaluation": evaluation})
                _append_ledger(self.ledger, {"event": "native_attempt_finished",
                                             "attempt": attempts + 1, "vector_sha256": vector_sha,
                                             "outcome": "complete",
                                             "evaluation_sha256": sha256_file(cached),
                                             "unix_s": time.time()})
                return evaluation
            except BaseException as error:
                outcome = ("timeout" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired))
                           else "uncertain" if isinstance(error, (UncertainProcessError, KeyboardInterrupt))
                           else "failed")
                stage_path = candidate / "worker_stage.json"
                stage = "before_native_initialization"
                if stage_path.is_file():
                    try:
                        stage = json.loads(stage_path.read_text(encoding="utf-8")).get(
                            "stage", "stage_evidence_unreadable")
                    except (OSError, ValueError):
                        stage = "stage_evidence_unreadable"
                _append_ledger(self.ledger, {"event": "native_attempt_finished",
                                     "attempt": attempts + 1, "vector_sha256": vector_sha,
                                     "outcome": outcome, "stage": stage,
                                     "error": type(error).__name__,
                                     "unix_s": time.time()})
                _write_json(candidate / "failure.json", {"outcome": outcome,
                                                          "stage": stage,
                                                          "error": type(error).__name__,
                                                          "message": str(error)})
                if outcome == "uncertain":
                    release_lock = False
                raise
        finally:
            if release_lock:
                self.lock.rmdir()

    def finalize(self, d0_path: Path, expected_records: int) -> Path:
        """Mark complete only after the shared JSONL/CSV/manifest exist and reconcile."""
        if self.lock.exists():
            raise AutoConfigError("cannot finalize while D0 batch lock exists")
        self._assert_identity()
        d0_path = Path(d0_path).resolve(strict=True)
        csv_path = d0_path.with_suffix(".csv")
        initialization = d0_path.with_name(d0_path.stem + "_initialization.json")
        if not csv_path.is_file() or not initialization.is_file():
            raise AutoConfigError("D0 JSONL/CSV/initialization artifacts incomplete")
        rows = [json.loads(line) for line in d0_path.read_text(encoding="utf-8").splitlines()]
        if len(rows) != expected_records:
            raise AutoConfigError("D0 record count differs from batch contract")
        events = [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]
        attempts = [row for row in events if row.get("event") == "native_attempt_reserved"]
        complete = [row for row in events if row.get("event") == "native_attempt_finished"
                    and row.get("outcome") == "complete"]
        persisted = [row for row in events if row.get("event") == "d0_record_persisted"]
        valid_rows = [row for row in rows if row["status"].get("parameter_valid") is True]
        if (len(attempts) != len(valid_rows) or len(complete) != len(valid_rows)
                 or len(persisted) != expected_records
                 or {row["vector_sha256"] for row in attempts}
                 != {row["vector_sha256"] for row in complete}
                 or {row["vector_sha256"] for row in attempts}
                 != {self._checkpoint_identity(row)[0] for row in valid_rows}
                 or [row["sample_id"] for row in persisted]
                != [row["sample_id"] for row in rows]):
            raise AutoConfigError("D0 ledger does not account for every completed candidate")
        path = self.experiment / "batch_completion.json"
        if path.exists():
            raise FileExistsError("D0 batch completion record already exists")
        _write_json(path, {"status": "complete", "experiment_id": self.experiment_id,
                           self.contract_key: self.approval_sha,
                           "physical_attempts": len(attempts),
                           "records": len(rows), "d0_jsonl_sha256": sha256_file(d0_path),
                           "d0_csv_sha256": sha256_file(csv_path),
                           "d0_initialization_sha256": sha256_file(initialization)})
        return path


class SampleHoldALExecutor(SampleHoldBatchExecutor):
    """Bounded AL validation using the existing isolated SampleHold worker."""

    def __init__(self, runtime: Any, config_path: Path, pilot: Mapping[str, Any],
                 *, launcher: Callable | None = None,
                 runtime_preflight: Mapping[str, Any] | None = None,
                 d0_path: Path | None = None):
        self.runtime = runtime
        self.config_path = Path(config_path).resolve(strict=True)
        self.launcher = launcher or _parameterized_native_launcher
        if not isinstance(pilot, Mapping):
            raise AutoConfigError("invalid bounded SampleHold AL pilot contract")
        budget = pilot.get("budget")
        execution_id = pilot.get("execution_id")
        root = Path(pilot.get("output_root") or "")
        if (runtime.circuit_id not in {"circuit_0259", "circuit_0263"}
                or pilot.get("enabled") is not True
                or set(pilot) != {"enabled", "execution_id", "output_root", "budget"}
                or not isinstance(execution_id, str)
                or re.fullmatch(r"sc[0-9]{4}_al_pilot_[a-z0-9_]+", execution_id) is None
                or execution_id[2:6] != runtime.circuit_id[-4:]
                or not root.is_absolute() or root.resolve().is_relative_to(ROOT)
                or not isinstance(budget, dict)
                or set(budget) != {"physical_attempts_max", "single_attempt_timeout_s",
                                   "overall_timeout_s", "automatic_retries"}
                or type(budget.get("physical_attempts_max")) is not int
                or budget["physical_attempts_max"] not in {1, 5}
                or type(budget.get("automatic_retries")) is not int
                or budget["automatic_retries"] != 0
                or type(budget.get("single_attempt_timeout_s")) not in (int, float)
                or type(budget.get("overall_timeout_s")) not in (int, float)
                or not math.isfinite(budget["single_attempt_timeout_s"])
                or not math.isfinite(budget["overall_timeout_s"])
                or budget["single_attempt_timeout_s"] <= 0
                or budget["overall_timeout_s"] < budget["single_attempt_timeout_s"]):
            raise AutoConfigError("invalid bounded SampleHold AL pilot contract")
        self.root = root.resolve()
        self.runtime_preflight = dict(runtime_preflight or {})
        self.d0_path = Path(d0_path).resolve(strict=True) if d0_path else None
        if self.launcher is _parameterized_native_launcher and os.name == "posix":
            if self.d0_path is None:
                raise AutoConfigError("SampleHold POSIX executor requires frozen D0")
            from ActiveLearning.onboarding.sample_hold_ubuntu import runtime_preflight as verify
            if self.runtime_preflight != verify(runtime, self.config_path, self.d0_path, pilot):
                raise AutoConfigError("SampleHold Ubuntu runtime preflight changed")
        self.experiment_id = execution_id
        self.budget = dict(budget)
        self.contract_key = "execution_contract_sha256"
        self.operator_full = True
        self.pilot_candidates = self.budget["physical_attempts_max"]
        self.continuation_review_path = None
        self.continuation_review_sha = None
        self.candidate_plan_sha = None
        self.candidate_plan_path = None
        self.expected_records = self.budget["physical_attempts_max"]
        self.checkpoint_path = None
        self.approval_path = None
        self.active_extension = False
        self.extension_path = None
        self.extension_sha = None
        self.extension = None
        self.experiment = self.root / execution_id
        self.ledger = self.experiment / "physical_attempt_ledger.jsonl"
        self.window_path = self.experiment / "execution_window_v1.json"
        self.approval = None
        self.approval_sha = _canonical_sha(al_pilot_contract(
            runtime, self.config_path, self.root, execution_id, self.budget,
        ))
        self.lock = self.root / (execution_id + ".lock")

    def _assert_identity(self) -> None:
        if (self.launcher is _parameterized_native_launcher and os.name == "posix"
                and not self.runtime_preflight):
            raise AutoConfigError("SampleHold POSIX executor requires runtime preflight")
        if _canonical_sha(al_pilot_contract(
                self.runtime, self.config_path, self.root,
                self.experiment_id, self.budget)) != self.approval_sha:
            raise AutoConfigError("SampleHold AL pilot execution contract changed")
        if self.runtime_preflight:
            from ActiveLearning.onboarding.sample_hold_ubuntu import runtime_preflight as verify
            pilot = {"execution_id": self.experiment_id, "budget": self.budget}
            if verify(self.runtime, self.config_path, self.d0_path, pilot) != self.runtime_preflight:
                raise AutoConfigError("SampleHold Ubuntu runtime preflight changed")
        if (self.launcher is _parameterized_native_launcher
                and os.name != "posix"
                and self.runtime.circuit_id != "circuit_0259"):
            from ActiveLearning.sc_sample_hold_canary import _native_environment_verified

            if not _native_environment_verified(_frozen_plan(self.runtime)["closure"]):
                raise AutoConfigError("AL pilot native environment differs from frozen closure")

    def _initialize_or_verify(self) -> dict:
        fresh = not self.experiment.exists()
        if not fresh and self.runtime_preflight:
            path = self.experiment / "runtime_preflight.json"
            if (not path.is_file() or json.loads(path.read_text(encoding="utf-8"))
                    != self.runtime_preflight):
                raise AutoConfigError("SampleHold Ubuntu preflight record missing or changed")
        identity = super()._initialize_or_verify()
        if fresh and self.runtime_preflight:
            _write_json(self.experiment / "runtime_preflight.json", self.runtime_preflight)
        return identity

    def _child_command(self, candidate: Path, token: str) -> list[str]:
        return [
            sys.executable, "-m", "ActiveLearning.onboarding.sample_hold_batch_child",
            "--config", str(self.config_path), "--al-pilot",
            "--output-root", str(self.root),
            "--candidate", str(candidate), "--token", token,
        ]

    def execution_statistics(self) -> dict[str, Any]:
        events = (
            [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]
            if self.ledger.is_file() else []
        )
        reserved = [event for event in events if event.get("event") == "native_attempt_reserved"]
        finished = [event for event in events if event.get("event") == "native_attempt_finished"]
        return {
            "schema": _AL_OPERATOR_SCHEMA,
            "execution_id": self.experiment_id,
            "execution_contract_sha256": self.approval_sha,
            "physical_attempts_reserved": len(reserved),
            "physical_attempts_max": self.budget["physical_attempts_max"],
            "automatic_retries": self.budget["automatic_retries"],
            "outcomes": [event.get("outcome") for event in finished],
            "ledger_path": str(self.ledger.resolve()),
            "candidate_cache_root": str((self.experiment / "candidates").resolve()),
            "lock_present": self.lock.exists(),
        }
