"""Offline subprocess contract for AL integration; real AL is not executable here."""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .runner import UnverifiedOrphanError
from .storage import atomic_json, sha256_file


def production_invocation_contract(job: dict, attempt_dir: Path) -> dict:
    """Describe the future callable boundary without returning an executable command."""
    return {"callable": "ActiveLearning.main.run_configured_active_learning",
            "circuit_type": job["circuit_type"], "circuit_id": job["circuit_id"],
            "config_path": job["config_path"], "configuration_id": job["configuration_id"],
            "config_sha256": job["config_sha256"], "d0_sha256": job["d0_sha256"],
            "attempt_directory": str(attempt_dir.resolve()),
            "required_optional_arguments": ["runs_root", "run_context_callback",
                                            "require_existing_d0", "expected_config_sha256",
                                            "expected_d0_sha256", "llm_client"],
            "real_execution_enabled": False}


def inspect_runtime_environment() -> dict:
    """Read-only dependency visibility; no simulator or provider invocation."""
    return {"python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "modules_visible": {name: importlib.util.find_spec(name) is not None
                                for name in ("yaml", "numpy", "scipy", "PySpice", "openai")},
            "ngspice_executable": shutil.which("ngspice")}


def _group_alive(pgid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _wait_group_empty(pgid: int, seconds: float = 2) -> bool:
    deadline = time.monotonic() + seconds
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _stop_posix_group(process: subprocess.Popen) -> bool:
    """Terminate the isolated session, then prove the entire group is gone."""
    pgid = process.pid  # Popen(start_new_session=True) makes the child its group leader.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
        if _wait_group_empty(pgid):
            try:
                return process.poll() is not None
            except OSError:
                return False
    return False


def _stop_child(process: subprocess.Popen) -> bool:
    if os.name == "posix":
        return _stop_posix_group(process)
    try:
        process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            return False
    except OSError:
        try:
            return process.poll() is not None
        except OSError:
            return False
    try:
        return process.poll() is not None
    except OSError:
        return False


def run_offline_fake_child(job: dict, attempt_dir: Path, *, scenario: str,
                           timeout_seconds: float = 5) -> dict:
    """Run only the bundled no-AL fake child; never accepts a command or module."""
    if scenario not in {"success", "invalid", "terminal", "nonzero", "crash",
                        "partial", "corrupt", "sleep"}:
        raise ValueError("unsupported offline scenario")
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    attempt_id = attempt_dir.name
    if not attempt_id.isalnum() or len(attempt_id) != 32:
        raise ValueError("expected a V2-generated attempt identity")
    identity = {"batch_id": attempt_dir.parents[3].name,
                "job_id": job["job_id"], "attempt_id": attempt_id,
                "run_id": f"al_{attempt_id}",
                "circuit_type": job["circuit_type"], "circuit_id": job["circuit_id"],
                "configuration_id": job["configuration_id"],
                "config_sha256": job["config_sha256"], "d0_sha256": job["d0_sha256"]}
    command = [sys.executable, "-m", "ActiveLearning.batch_runner_v2.fake_al_child",
               "--identity", json.dumps(identity, sort_keys=True),
               "--attempt-dir", str(attempt_dir), "--scenario", scenario]
    # Intent is durable *before* spawn. An abrupt scheduler death between spawn
    # and reaping therefore cannot silently allow the next circuit to start.
    atomic_json(attempt_dir / "child_spawn_intent.json", identity)
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[2],
                                   start_new_session=os.name == "posix")
    except OSError as error:
        # A failed spawn is usually child-free, but a partial OS spawn cannot be
        # proven here. Keep the durable intent and halt the batch for review.
        raise UnverifiedOrphanError("child spawn failed; process state unverified") from error
    try:
        code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        if not _stop_child(process):
            raise UnverifiedOrphanError("fake child exit could not be confirmed") from error
        atomic_json(attempt_dir / "child_reaped.json", {"attempt_id": attempt_id,
                                                        "exit_confirmed": True})
        # Even a confirmed stop cannot prove that prior AL side effects did not occur.
        raise RuntimeError("child timeout; stopped attempt is not replayable") from error
    except OSError as error:
        if not _stop_child(process):
            raise UnverifiedOrphanError("child wait failed and exit is unverified") from error
        atomic_json(attempt_dir / "child_reaped.json", {"attempt_id": attempt_id,
                                                        "exit_confirmed": True})
        raise RuntimeError("child wait failed; attempt is uncertain") from error
    if os.name == "posix" and _group_alive(process.pid):
        if not _stop_child(process):
            raise UnverifiedOrphanError("descendant process group may still be running")
        atomic_json(attempt_dir / "child_reaped.json", {"attempt_id": attempt_id,
                                                        "exit_confirmed": True, "descendants_stopped": True})
        raise RuntimeError("descendants remained after parent exit; attempt uncertain")
    atomic_json(attempt_dir / "child_reaped.json", {"attempt_id": attempt_id,
                                                    "exit_confirmed": True, "exit_code": code})
    result_path = attempt_dir / "al_run" / identity["run_id"] / "al_summary.json"
    try:
        if result_path.is_symlink() or not result_path.resolve(strict=True).is_relative_to(attempt_dir):
            raise ValueError("result path escapes attempt")
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("missing or corrupt child result") from error
    if not isinstance(result, dict) or any(result.get(key) != value for key, value in identity.items()):
        raise RuntimeError("child result identity mismatch")
    if code == 2 and result.get("error_code") == "SYNTHETIC_CIRCUIT_FAILURE":
        return {"state": "FAILED_TERMINAL", "error_code": result["error_code"]}
    if code != 0:
        raise RuntimeError(f"child exited without a verified completion: {code}")
    if result.get("al_completed") is not True or type(result.get("simulation_valid")) is not bool \
            or type(result.get("meets_spec")) is not bool \
            or result.get("stop_reason") not in {"meets_spec", "max_iterations", "initial_d0_meets_spec"} \
            or type(result.get("iterations_completed")) is not int:
        raise RuntimeError("invalid AL completion evidence")
    return {"run_id": identity["run_id"], "result_path": str(result_path),
            "al_result_sha256": sha256_file(result_path),
            "simulation_valid": result["simulation_valid"],
            "meets_spec": result["meets_spec"],
            "stop_reason": result["stop_reason"],
            "iterations_completed": result["iterations_completed"]}
