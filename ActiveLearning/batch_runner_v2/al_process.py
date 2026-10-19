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
from .storage import atomic_json, read_json, sha256_file


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


def _verified_safe_stop(manifest: dict, job: dict, attempt_dir: Path,
                        invocation: dict, result: dict) -> dict:
    """Accept only a closed request ledger and a normally reaped safe-point child."""
    from .storage import read_jsonl_strict

    state = result.get("state")
    point = result.get("safe_point")
    if (state, point) not in {
            ("SKIPPED_JOB_TIMEOUT", "rate_limit_wait"),
            ("SKIPPED_JOB_BUDGET", "per_job_cap")}:
        raise RuntimeError("unverified safe-stop classification")
    if result.get("job_deadline_monotonic") != invocation.get("job_deadline_monotonic"):
        raise RuntimeError("safe-stop deadline identity mismatch")
    journal = attempt_dir.parents[3] / "api_usage.jsonl"
    if not journal.is_file() or sha256_file(journal) != result.get("journal_sha256"):
        raise RuntimeError("safe-stop journal checkpoint mismatch")
    rows = read_jsonl_strict(journal)
    started = [row for row in rows if row.get("event") == "attempt_started"]
    finished = [row for row in rows if row.get("event") == "attempt_finished"]
    key = lambda row: (row.get("job_id"), row.get("attempt_id"),
                       row.get("logical_id"), row.get("attempt"))
    if sorted(map(key, started), key=str) != sorted(map(key, finished), key=str):
        raise RuntimeError("safe-stop has an unresolved physical request")
    if len(started) > manifest["resource_limits"]["max_physical_attempts"]:
        raise RuntimeError("safe-stop exceeds batch request cap")
    own = [row for row in finished if row.get("job_id") == job["job_id"]
           and row.get("attempt_id") == attempt_dir.name]
    if state == "SKIPPED_JOB_TIMEOUT":
        ended = [row for row in rows if row.get("event") == "logical_finished"
                 and row.get("job_id") == job["job_id"]
                 and row.get("attempt_id") == attempt_dir.name]
        if not own or own[-1].get("outcome") != "rate_limit" or not ended or (
                ended[-1].get("status") != "job_deadline"):
            raise RuntimeError("safe timeout lacks a confirmed 429 waiting point")
    else:
        cap = manifest["resource_limits"].get("per_job_physical_attempt_cap")
        used = sum(row.get("job_id") == job["job_id"] for row in started)
        ended = [row for row in rows if row.get("event") == "logical_finished"
                 and row.get("job_id") == job["job_id"]
                 and row.get("attempt_id") == attempt_dir.name]
        if (cap is None or used < cap or not ended
                or ended[-1].get("status") != "job_budget"):
            raise RuntimeError("safe budget stop did not reach the approved job cap")
    return {"state": state, "worker_result_sha256": sha256_file(attempt_dir / "worker_result.json")}


def run_real_al_child(
    manifest: dict,
    job: dict,
    attempt_dir: Path,
    authorization: dict,
    *,
    offline_scenario: str | None = None,
    production_enabled: bool = False,
) -> dict:
    """Launch the fixed authorized AL child; never accepts an arbitrary command."""
    if manifest.get("schema_version") != "batch_runner_v2b2a_candidate_v1":
        raise ValueError("real AL requires a B2A candidate manifest")
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    attempt_id = attempt_dir.name
    if not attempt_id.isalnum() or len(attempt_id) != 32:
        raise ValueError("expected a V2-generated attempt identity")
    if authorization.get("execution_scope") == "production":
        if offline_scenario is not None:
            raise ValueError("production authorization cannot run an offline fixture")
        if production_enabled is not True:
            raise PermissionError("production execution is not enabled")
    elif authorization.get("execution_scope") == "offline_integration":
        if offline_scenario is None:
            raise ValueError("offline-integration authorization requires a fixed fixture")
    else:
        raise ValueError("unsupported authorization execution scope")

    invocation = {
        "schema_version": "batch_runner_v2b2a_invocation_v1",
        "batch_id": manifest["batch_id"], "job_id": job["job_id"],
        "attempt_id": attempt_id, "manifest_path": manifest["manifest_path"],
        "manifest_sha256": manifest["manifest_sha256"],
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": authorization["authorization_sha256"],
        "attempt_directory": str(attempt_dir),
        "offline_scenario": offline_scenario,
    }
    if manifest["retry_policy"].get("rate_limit_mode") == "job_deadline":
        scheduler = read_json(attempt_dir.parents[3] / "status.json")
        current = scheduler["jobs"][job["job_id"]]
        if current["state"] != "RUNNING" or current["attempts"][-1]["attempt_id"] != attempt_id:
            raise RuntimeError("job deadline has no active scheduler identity")
        deadline = current["attempts"][-1].get("job_deadline_monotonic")
        if type(deadline) not in {int, float} or deadline <= time.monotonic():
            raise RuntimeError("job deadline is missing or already expired")
        invocation["job_deadline_monotonic"] = deadline
    invocation_path = attempt_dir / "real_al_invocation.json"
    atomic_json(invocation_path, invocation)
    atomic_json(attempt_dir / "child_spawn_intent.json", {
        "batch_id": manifest["batch_id"], "job_id": job["job_id"],
        "attempt_id": attempt_id, "authorization_id": authorization["authorization_id"],
        "authorization_sha256": authorization["authorization_sha256"],
    })
    command = [
        sys.executable, "-m", "ActiveLearning.batch_runner_v2.real_al_child",
        "--invocation", str(invocation_path),
        "--authorization", authorization["authorization_path"],
        "--authorization-sha256", authorization["authorization_sha256"],
    ]
    environment = os.environ.copy()
    if production_enabled:
        environment["BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256"] = \
            authorization["authorization_sha256"]
    else:
        environment.pop("BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256", None)
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd=manifest["code_root"], env=environment,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        raise UnverifiedOrphanError("real AL child spawn failed; process state unverified") from error
    try:
        timeout = manifest["resource_limits"]["worker_timeout_seconds"]
        if "job_deadline_monotonic" in invocation:
            timeout = max(0.001, invocation["job_deadline_monotonic"] - time.monotonic() + 1)
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if not _stop_child(process):
            raise UnverifiedOrphanError("real AL child exit could not be confirmed") from error
        atomic_json(attempt_dir / "child_reaped.json", {
            "attempt_id": attempt_id, "exit_confirmed": True, "timeout": True,
        })
        raise RuntimeError("real AL child timeout; stopped attempt is not replayable") from error
    except OSError as error:
        if not _stop_child(process):
            raise UnverifiedOrphanError("real AL child wait failed and exit is unverified") from error
        atomic_json(attempt_dir / "child_reaped.json", {
            "attempt_id": attempt_id, "exit_confirmed": True, "wait_error": True,
        })
        raise RuntimeError("real AL child wait failed; attempt is uncertain") from error
    if os.name == "posix" and _group_alive(process.pid):
        if not _stop_child(process):
            raise UnverifiedOrphanError("real AL descendant process group may still be running")
        atomic_json(attempt_dir / "child_reaped.json", {
            "attempt_id": attempt_id, "exit_confirmed": True,
            "descendants_stopped": True,
        })
        raise RuntimeError("real AL descendants remained after parent exit; attempt uncertain")
    atomic_json(attempt_dir / "child_reaped.json", {
        "attempt_id": attempt_id, "exit_confirmed": True, "exit_code": code,
    })

    result_path = attempt_dir / "worker_result.json"
    try:
        if result_path.is_symlink() or not result_path.resolve(strict=True).is_relative_to(attempt_dir):
            raise ValueError("worker result escapes attempt")
        result = read_json(result_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("real AL child has no trustworthy completion result") from error
    for name, expected in {
        "batch_id": manifest["batch_id"], "job_id": job["job_id"],
        "attempt_id": attempt_id, "manifest_sha256": manifest["manifest_sha256"],
        "authorization_sha256": authorization["authorization_sha256"],
    }.items():
        if result.get(name) != expected:
            raise RuntimeError(f"real AL child result identity mismatch: {name}")
    if code == 3 and result.get("state") in {
            "SKIPPED_JOB_TIMEOUT", "SKIPPED_JOB_BUDGET"}:
        return _verified_safe_stop(manifest, job, attempt_dir, invocation, result)
    if code == 0 and "job_deadline_monotonic" in invocation and (
            time.monotonic() > invocation["job_deadline_monotonic"]):
        raise RuntimeError("AL completed after the job deadline")
    if code == 2 and result.get("state") in {"FAILED_TERMINAL", "BLOCKED"}:
        return {"state": result["state"],
                "error_code": str(result.get("error_code") or "REAL_AL_TERMINAL")}
    if code != 0 or result.get("state") != "COMPLETED":
        raise RuntimeError(f"real AL child exited without verified completion: {code}")
    if result.get("al_completed") is not True \
            or type(result.get("simulation_valid")) is not bool \
            or type(result.get("meets_spec")) is not bool \
            or result.get("stop_reason") not in {
                "meets_spec", "max_iterations", "initial_d0_meets_spec"
            } \
            or type(result.get("iterations_completed")) is not int \
            or result["iterations_completed"] < 0 \
            or result["iterations_completed"] > job["max_iterations"] \
            or not isinstance(result.get("final_metrics"), dict):
        raise RuntimeError("real AL child completion evidence is invalid")
    al_path = Path(result["result_path"])
    if al_path.is_symlink() or not al_path.resolve(strict=True).is_relative_to(attempt_dir) \
            or sha256_file(al_path) != result.get("al_result_sha256"):
        raise RuntimeError("real AL result artifact is invalid")
    return {
        "run_id": result["run_id"], "result_path": str(al_path),
        "al_result_sha256": result["al_result_sha256"],
        "simulation_valid": result["simulation_valid"],
        "meets_spec": result["meets_spec"], "stop_reason": result["stop_reason"],
        "iterations_completed": result["iterations_completed"],
        "final_metrics": result["final_metrics"],
        "d0_record_count": result.get("d0_record_count"),
    }
