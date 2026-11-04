"""Generic guarded E1 canary. Default CLI only validates; native mode needs approval."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from ActiveLearning.sc_0259_canary import (
    UncertainProcessError, _append_ledger, _native_launcher, _write_json,
)
from ActiveLearning.sc_sample_hold_generic import (
    GenericCanaryError, ROOT, evaluate_physical, load_candidate, sha256_file,
)


def _verified_closure(config: dict) -> dict:
    asset = config.get("source_closure")
    if not isinstance(asset, dict) or set(asset) != {"path", "sha256"}:
        raise GenericCanaryError("separate source closure identity required")
    path = (ROOT / asset["path"]).resolve(strict=True)
    if not path.is_relative_to(ROOT) or sha256_file(path) != asset["sha256"]:
        raise GenericCanaryError("source closure raw bytes mismatch")
    closure = json.loads(path.read_text(encoding="utf-8"))
    files = closure.get("source_files")
    if (closure.get("schema") != "sc_sample_hold_source_closure_sc4c3_v1"
            or closure.get("circuit_id") != config["circuit_id"]
            or not isinstance(files, list) or not files
            or files != sorted(files, key=lambda item: item["path"])):
        raise GenericCanaryError("malformed or mismatched source closure")
    seen = set()
    for item in files:
        if set(item) != {"path", "sha256"} or item["path"] in seen:
            raise GenericCanaryError("duplicate or malformed closure item")
        seen.add(item["path"])
        source = (ROOT / item["path"]).resolve(strict=True)
        if not source.is_relative_to(ROOT) or sha256_file(source) != item["sha256"]:
            raise GenericCanaryError("source closure item changed")
    required = {asset["path"] for asset in config["source_assets"].values()}
    required |= {"ActiveLearning/sc_sample_hold_generic.py",
                 "ActiveLearning/sc_sample_hold_canary.py",
                 "ActiveLearning/sc_sample_hold_canary_child.py",
                 "ActiveLearning/sample_hold_offline.py",
                 "ActiveLearning/windows_job_isolation.py",
                 "ActiveLearning/sc_0259_canary.py"}
    if not required.issubset(seen):
        raise GenericCanaryError("source closure omits execution-affecting code")
    return closure


def load_plan(candidate_path: Path) -> dict:
    """Purely static plan; no output directory, native import or ledger mutation."""
    plan = load_candidate(candidate_path)
    plan["closure"] = _verified_closure(plan["config"])
    return plan


def _native_environment_verified(closure: dict) -> bool:
    if closure.get("status") != "frozen_verified":
        return False
    env = closure.get("external_environment", {})
    if set(env.get("packages", {})) != {"PySpice", "numpy", "scipy", "PyYAML", "cffi", "pywin32"}:
        return False
    if (Path(sys.executable).resolve() != Path(env.get("python_executable", "")).resolve()
            or sys.version.split()[0] != env.get("python_version")
            or os.environ.get("PYTHONPATH")):
        return False
    for name, version in env.get("packages", {}).items():
        try:
            if importlib.metadata.version(name) != version:
                return False
        except importlib.metadata.PackageNotFoundError:
            return False
    if set(env.get("environment_paths", {})) != {"NGSPICE_LIBRARY_PATH", "SPICE_LIB_DIR"}:
        return False
    for name, value in env["environment_paths"].items():
        if Path(os.environ.get(name, "")).resolve() != Path(value).resolve():
            return False
    if set(env.get("native_files", {})) != {"ngspice_dll", "libomp_dll", "spinit"}:
        return False
    for asset in env["native_files"].values():
        path = Path(asset["path"])
        if not path.is_absolute() or not path.is_file() or sha256_file(path) != asset["sha256"]:
            return False
    binding = env.get("resolved_win32job_module")
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        return False
    spec = importlib.util.find_spec("win32job")
    if (spec is None or spec.origin is None or
            Path(spec.origin).resolve() != Path(binding["path"]).resolve() or
            sha256_file(Path(spec.origin)) != binding["sha256"]):
        return False
    module_bindings = env.get("resolved_pywin32_modules")
    if not isinstance(module_bindings, dict) or set(module_bindings) != {
            "win32job", "win32api", "win32process", "win32event", "win32con"}:
        return False
    for name, expected in module_bindings.items():
        if not isinstance(expected, dict) or set(expected) != {"path", "sha256"}:
            return False
        try:
            origin = Path(importlib.import_module(name).__file__).resolve()
        except (ImportError, AttributeError, TypeError):
            return False
        if (origin != Path(expected["path"]).resolve() or
                sha256_file(origin) != expected["sha256"]):
            return False
    return True


def _approval(plan: dict, approval_path: Path) -> dict:
    approval_path = approval_path.resolve(strict=True)
    if approval_path.is_relative_to(ROOT):
        raise GenericCanaryError("independent approval must remain outside repository")
    data = json.loads(approval_path.read_text(encoding="utf-8"))
    config = plan["config"]
    expected = {"schema": "sc_sample_hold_one_time_operator_approval_v1",
                "intent": "one_real_transient", "experiment_id": config["experiment_id"],
                "circuit_id": config["circuit_id"],
                "candidate_file_sha256": plan["file_sha256"],
                "fixture_sha256": plan["fixture"].fixture_hash,
                "stimulus_sha256": config["stimulus_sha256"],
                "source_closure_file_sha256": config["source_closure"]["sha256"],
                "budget": config["budget"]}
    if set(data) != set(expected) | {"approved_by", "approved_at", "output_root"} or any(
            data.get(key) != value for key, value in expected.items()):
        raise GenericCanaryError("approval does not bind candidate, closure and budget")
    if not str(data["approved_by"]).strip():
        raise GenericCanaryError("operator identity missing")
    try:
        timestamp = datetime.fromisoformat(data["approved_at"])
    except (TypeError, ValueError) as error:
        raise GenericCanaryError("operator approval timestamp invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise GenericCanaryError("operator approval must include timezone")
    root = Path(data["output_root"])
    if (not root.is_absolute() or root.resolve() == ROOT or
            root.resolve().is_relative_to(ROOT) or root.resolve() != Path(config["output_root"]).resolve()):
        raise GenericCanaryError("approval output root differs from independent budget root")
    return data


def run_guarded(plan: dict, approval_path: Path, *, acknowledgement: bool = False,
                launcher: Callable = _native_launcher) -> dict:
    """One durable attempt only; fake launchers are solely for selected offline tests."""
    if not acknowledgement:
        raise GenericCanaryError("separate operator acknowledgement required")
    fresh = load_plan(plan["path"])
    if fresh["file_sha256"] != plan["file_sha256"]:
        raise GenericCanaryError("candidate changed after static plan")
    plan = fresh
    approval = _approval(plan, approval_path)
    if launcher is _native_launcher and (os.name != "nt" or
                                       not _native_environment_verified(plan["closure"])):
        raise GenericCanaryError("strict Windows native environment is not frozen/verified")
    start = time.monotonic()
    config = plan["config"]
    root = Path(approval["output_root"]).resolve()
    experiment = root / config["experiment_id"]
    root.mkdir(parents=True, exist_ok=True)
    lock = root / (config["experiment_id"] + ".lock")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise GenericCanaryError("experiment lock exists") from error
    release_lock = False
    try:
        if experiment.exists():
            raise GenericCanaryError("experiment exists; no overwrite/retry")
        experiment.mkdir()
        ledger = experiment / "physical_attempt_ledger.jsonl"
        _write_json(experiment / "candidate.json", config)
        _write_json(experiment / "stimulus.json", plan["stimulus"])
        _write_json(experiment / "identity.json", {
            "candidate_file_sha256": plan["file_sha256"],
            "fixture_sha256": plan["fixture"].fixture_hash,
            "source_closure_file_sha256": config["source_closure"]["sha256"],
            "approval_file_sha256": sha256_file(approval_path),
        })
        token = uuid.uuid4().hex
        intent = {"event": "native_attempt_reserved", "attempt": 1,
                  "experiment_id": config["experiment_id"],
                  "candidate_file_sha256": plan["file_sha256"],
                  "utc_unix_s": time.time(), "token": token}
        _append_ledger(ledger, intent)  # Durable before any child can start.
        _write_json(experiment / "launch_intent.json", intent)
        command = [sys.executable, "-m", "ActiveLearning.sc_sample_hold_canary_child",
                   "--candidate", str(plan["path"]), "--experiment", str(experiment),
                   "--token", token]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        timeout = min(config["budget"]["single_native_attempt_timeout_s"],
                      config["budget"]["overall_execution_timeout_s"] -
                      (time.monotonic() - start))
        if timeout <= 0:
            raise TimeoutError("overall deadline elapsed before launch")
        try:
            completed = launcher(command, cwd=ROOT, timeout=timeout,
                                 environment=environment)
            if launcher is _native_launcher:
                evidence = getattr(completed, "job_evidence", None)
                if (not isinstance(evidence, dict) or
                        evidence.get("contained_before_resume") is not True or
                        evidence.get("active_processes_at_return") != 0):
                    raise UncertainProcessError("native Job containment/reaping not confirmed")
            _write_json(experiment / "simulator_status.json", {
                "returncode": completed.returncode, "mock": launcher is not _native_launcher,
                "process_isolation": getattr(completed, "job_evidence", None)})
            (experiment / "child_stdout.txt").write_text(completed.stdout or "", encoding="utf-8")
            (experiment / "child_stderr.txt").write_text(completed.stderr or "", encoding="utf-8")
            if completed.returncode != 0:
                raise GenericCanaryError(f"native child exited {completed.returncode}")
            if time.monotonic() - start > config["budget"]["overall_execution_timeout_s"]:
                raise TimeoutError("overall deadline exceeded")
            raw = json.loads((experiment / "raw_waveforms.json").read_text(encoding="utf-8"))
            if (raw.get("mock") is not (launcher is not _native_launcher)
                    or raw.get("fixture_sha256") != plan["fixture"].fixture_hash
                    or raw.get("stimulus_sha256") != config["stimulus_sha256"]):
                raise GenericCanaryError("physical waveform provenance mismatch")
            result = evaluate_physical(plan["fixture"], raw["waveforms"], mock=raw["mock"])
            _write_json(experiment / "evaluation_result.json", result)
            _append_ledger(ledger, {"event": "native_attempt_finished", "attempt": 1,
                                    "outcome": "complete" if result["evaluation"]["measurement_valid"]
                                    else "measurement_invalid", "utc_unix_s": time.time()})
            release_lock = True
            return {"status": "complete" if result["evaluation"]["measurement_valid"]
                    else "measurement_invalid", "experiment": str(experiment),
                    "result": result, "mock": raw["mock"]}
        except BaseException as error:
            outcome = ("timeout" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired))
                       else "interrupted_or_uncertain" if isinstance(
                           error, (KeyboardInterrupt, UncertainProcessError)) else "failed")
            _append_ledger(ledger, {"event": "native_attempt_finished", "attempt": 1,
                                    "outcome": outcome, "error_type": type(error).__name__,
                                    "utc_unix_s": time.time()})
            _write_json(experiment / "failure_diagnostics.json", {
                "outcome": outcome, "error_type": type(error).__name__, "message": str(error)})
            release_lock = not isinstance(error, UncertainProcessError)
            raise
    finally:
        if release_lock:
            lock.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generic E1 offline plan by default")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--execute-real-spice", action="store_true")
    parser.add_argument("--approval-file", type=Path)
    parser.add_argument("--acknowledge-approval", action="store_true")
    args = parser.parse_args(argv)
    plan = load_plan(args.candidate)
    if not args.execute_real_spice:
        print(json.dumps({"mode": "OFFLINE_PLAN", "circuit_id": plan["config"]["circuit_id"],
                          "fixture_sha256": plan["fixture"].fixture_hash,
                          "stimulus_sha256": plan["config"]["stimulus_sha256"],
                          "source_hashes_verified": True, "native_attempts": 0,
                          "real_execution_authorized": False}, indent=2))
        return 0
    if args.approval_file is None:
        raise GenericCanaryError("independent approval file required")
    run_guarded(plan, args.approval_file, acknowledgement=args.acknowledge_approval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
