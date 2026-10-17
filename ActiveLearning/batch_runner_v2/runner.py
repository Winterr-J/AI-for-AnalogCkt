"""Sequential offline mock scheduler. This module cannot execute real AL."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable

from .schema import load_manifest, verify_job
from .storage import append_jsonl, atomic_json, batch_lock, read_json, read_jsonl_strict, sha256_file


class UnverifiedOrphanError(RuntimeError):
    """The child (or one of its descendants) may still be doing work."""


def plan_batch(manifest_path: str | Path) -> dict:
    """Pure read-only preflight; no AL, SPICE, D0, or LLM calls."""
    manifest = load_manifest(manifest_path)
    return {"batch_id": manifest["batch_id"], "status": "READY_FOR_OFFLINE_MOCK",
            "manifest_sha256": manifest["manifest_sha256"],
            "jobs": [{"job_id": job["job_id"], "circuit_type": job["circuit_type"],
                      "circuit_id": job["circuit_id"], "configuration_id": job["configuration_id"]}
                     for job in manifest["jobs"]],
            "real_execution_available": False}


def _batch_dir(output_root: str | Path, batch_id: str) -> Path:
    from .schema import _label

    _label(batch_id, "batch_id")
    root = Path(output_root).resolve()
    return root / batch_id


def _event(directory: Path, job_id: str, state: str, detail: str | None = None) -> None:
    append_jsonl(directory / "events.jsonl", {"job_id": job_id, "state": state,
                                                    "detail": detail})


def _save(directory: Path, state: dict) -> None:
    atomic_json(directory / "status.json", state)


def _initial_state(manifest: dict) -> dict:
    return {"batch_id": manifest["batch_id"],
            "manifest_sha256": manifest["manifest_sha256"],
            "offline_only": True,
            "jobs": {job["job_id"]: {
                "state": "PENDING", "attempts": [], "circuit_type": job["circuit_type"],
                "circuit_id": job["circuit_id"], "configuration_id": job["configuration_id"],
                "config_sha256": job["config_sha256"],
                "d0_sha256": job["d0_sha256"], "source_sha256": job["source_sha256"],
                "max_iterations": job["max_iterations"], "seed": job["seed"],
                "provider": manifest["provider"], "model": manifest["model"],
                "pipeline_success": None, "meets_spec": None,
            } for job in manifest["jobs"]}}


def _verify_completion(directory: Path, job_id: str, row: dict) -> bool:
    if row.get("state") != "COMPLETED" or not row["attempts"]:
        return False
    attempt = row["attempts"][-1]
    result_path = directory / "jobs" / job_id / "attempts" / attempt["attempt_id"] / "result.json"
    if not result_path.is_file() or sha256_file(result_path) != attempt.get("result_sha256"):
        return False
    try:
        result = read_json(result_path)
    except (ValueError, json.JSONDecodeError):
        return False
    al_result = result.get("result_path")
    if al_result is not None:
        try:
            al_path = Path(al_result)
            if al_path.is_symlink() or not al_path.resolve(strict=True).is_relative_to(
                    result_path.parent.resolve()) or \
                    sha256_file(al_path) != result.get("al_result_sha256"):
                return False
        except (OSError, ValueError):
            return False
    return (result.get("attempt_id") == attempt["attempt_id"]
            and result.get("pipeline_success") is True
            and result.get("stop_reason") in {
                "meets_spec", "max_iterations", "initial_d0_meets_spec"
            } and type(result.get("meets_spec")) is bool
            and result.get("meets_spec") == row.get("meets_spec"))


def run_mock_batch(
    manifest_path: str | Path, output_root: str | Path,
    worker: Callable[[dict, Path], dict],
) -> dict:
    """Execute only an explicitly injected *mock* worker.

    No built-in real worker or execute flag exists. The supplied worker must be
    controlled by the caller; production use is outside this milestone.
    """
    if not callable(worker):
        raise TypeError("an injected mock worker is required")
    manifest = load_manifest(manifest_path)
    directory = _batch_dir(output_root, manifest["batch_id"])
    if directory.is_symlink():
        raise ValueError("batch directory must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True)
    with batch_lock(directory / ".runner.lock"):
        status_path = directory / "status.json"
        if status_path.exists():
            state = read_json(status_path)
            if state.get("manifest_sha256") != manifest["manifest_sha256"] or \
                    state.get("offline_only") is not True:
                raise ValueError("batch identity changed or is not offline-only")
            if set(state.get("jobs", {})) != {job["job_id"] for job in manifest["jobs"]}:
                raise ValueError("persisted job set differs from manifest")
        else:
            if any(child.name != ".runner.lock" for child in directory.iterdir()):
                raise ValueError("refusing to adopt an existing non-V2 batch directory")
            state = _initial_state(manifest)
            atomic_json(directory / "manifest.json", manifest)
            _save(directory, state)
        if state.get("orphan_unverified"):
            raise UnverifiedOrphanError("an earlier child may still be running; manual review required")
        for job in manifest["jobs"]:
            job_id = job["job_id"]
            row = state["jobs"][job_id]
            if row["state"] == "COMPLETED":
                if not _verify_completion(directory, job_id, row):
                    row["state"] = "BLOCKED"
                    row["error_code"] = "COMPLETION_ARTIFACT_INVALID"
                    _save(directory, state)
                    _event(directory, job_id, "BLOCKED", row["error_code"])
                continue
            if row["state"] in {"RUNNING", "WAITING_RETRY"}:
                row["state"] = "INTERRUPTED_UNCERTAIN"
                row["error_code"] = "LOST_AL_CALL_STACK"
                attempt = row["attempts"][-1]
                attempt_dir = directory / "jobs" / job_id / "attempts" / attempt["attempt_id"]
                if (attempt_dir / "child_spawn_intent.json").exists() and not \
                        (attempt_dir / "child_reaped.json").exists():
                    row["error_code"] = "ORPHAN_UNVERIFIED"
                    state["orphan_unverified"] = True
                _save(directory, state)
                _event(directory, job_id, "INTERRUPTED_UNCERTAIN", row["error_code"])
                if state.get("orphan_unverified"):
                    break
                continue
            if row["state"] != "PENDING":
                continue
            provider_block = directory / "provider_block.json"
            if provider_block.exists():
                block = read_json(provider_block)
                if block.get("scope") == (f"{manifest['provider']}/{manifest['model']}/"
                                           f"{manifest['credential_scope']}"):
                    row["state"] = "BLOCKED"
                    row["error_code"] = f"PROVIDER_{block.get('reason', 'BLOCKED').upper()}"
                    _save(directory, state)
                    _event(directory, job_id, "BLOCKED", row["error_code"])
                    continue
            try:
                verify_job(manifest, job)
            except (ValueError, OSError) as error:
                row["state"] = "BLOCKED"
                row["error_code"] = type(error).__name__
                _save(directory, state)
                _event(directory, job_id, "BLOCKED", row["error_code"])
                continue
            attempt_id = uuid.uuid4().hex
            attempt_dir = directory / "jobs" / job_id / "attempts" / attempt_id
            attempt_dir.mkdir(parents=True, exist_ok=False)
            row["attempts"].append({"attempt_id": attempt_id,
                                    "attempt_directory": str(attempt_dir)})
            row["state"] = "RUNNING"
            _save(directory, state)
            _event(directory, job_id, "RUNNING", attempt_id)
            try:
                outcome = worker(job, attempt_dir)
            except UnverifiedOrphanError:
                row["state"] = "INTERRUPTED_UNCERTAIN"
                row["error_code"] = "ORPHAN_UNVERIFIED"
                state["orphan_unverified"] = True
                _save(directory, state)
                _event(directory, job_id, "INTERRUPTED_UNCERTAIN", row["error_code"])
                break
            except Exception as error:
                # Unexpected worker exceptions may have consumed LLM/SPICE work.
                row["state"] = "INTERRUPTED_UNCERTAIN"
                row["error_code"] = type(error).__name__
                _save(directory, state)
                _event(directory, job_id, "INTERRUPTED_UNCERTAIN", row["error_code"])
                continue
            if isinstance(outcome, dict) and outcome.get("state") == "FAILED_TERMINAL":
                row["state"] = "FAILED_TERMINAL"
                row["pipeline_success"] = False
                row["meets_spec"] = None
                row["error_code"] = str(outcome.get("error_code") or "MOCK_TERMINAL")
                _save(directory, state)
                _event(directory, job_id, "FAILED_TERMINAL", row["error_code"])
                continue
            if not isinstance(outcome, dict) or outcome.get("stop_reason") not in {
                "meets_spec", "max_iterations", "initial_d0_meets_spec"
            } or type(outcome.get("meets_spec")) is not bool:
                row["state"] = "INTERRUPTED_UNCERTAIN"
                row["error_code"] = "INVALID_WORKER_RESULT"
                _save(directory, state)
                _event(directory, job_id, "INTERRUPTED_UNCERTAIN", row["error_code"])
                continue
            # After the mock worker returns, any persistence failure must leave
            # RUNNING for uncertain restart reconciliation, not claim completion.
            try:
                result = {"attempt_id": attempt_id, "run_id": outcome.get("run_id") or attempt_id,
                          "pipeline_success": True, "meets_spec": outcome["meets_spec"],
                          "simulation_valid": outcome.get("simulation_valid"),
                          "stop_reason": outcome["stop_reason"],
                          "iterations_completed": outcome.get("iterations_completed"),
                          "result_path": outcome.get("result_path"),
                          "al_result_sha256": outcome.get("al_result_sha256")}
                result_path = attempt_dir / "result.json"
                atomic_json(result_path, result)
                row["attempts"][-1]["result_sha256"] = sha256_file(result_path)
                row["state"] = "COMPLETED"
                row["pipeline_success"] = True
                row["meets_spec"] = outcome["meets_spec"]
                row["simulation_valid"] = outcome.get("simulation_valid")
                row["stop_reason"] = outcome["stop_reason"]
                _save(directory, state)
                _event(directory, job_id, "COMPLETED", attempt_id)
            except Exception:
                raise  # A restart will classify RUNNING as uncertain.
        summary = report_batch(manifest["batch_id"], output_root)
        atomic_json(directory / "summary.json", summary)
        return summary


def status_batch(batch_id: str, output_root: str | Path) -> dict:
    return read_json(_batch_dir(output_root, batch_id) / "status.json")


def report_batch(batch_id: str, output_root: str | Path) -> dict:
    state = status_batch(batch_id, output_root)
    rows = state["jobs"]
    ledger = read_jsonl_strict(_batch_dir(output_root, batch_id) / "api_usage.jsonl")
    started = [item for item in ledger if item.get("event") == "attempt_started"]
    finished = [item for item in ledger if item.get("event") == "attempt_finished"]
    logical = [item for item in ledger if item.get("event") == "logical_started"]
    waits = [item for item in ledger if item.get("event") == "wait_finished"]
    logical_finished = [item for item in ledger if item.get("event") == "logical_finished"]
    reported = [item for item in finished if type(item.get("total_tokens")) is int]
    usage_unknown = (len(started) != len(finished)
                     or any(item.get("usage_unknown") is True for item in finished))
    def per_job_usage(job_id: str) -> dict:
        own_started = [item for item in started if item.get("job_id") == job_id]
        own_finished = [item for item in finished if item.get("job_id") == job_id]
        unknown = (len(own_started) != len(own_finished)
                   or any(item.get("usage_unknown") is True for item in own_finished))
        return {"physical_attempts": len(own_started),
                "reported_total_tokens": sum(item["total_tokens"] for item in own_finished
                                             if type(item.get("total_tokens")) is int),
                "request_seconds": sum(float(item.get("latency_seconds") or 0) for item in own_finished),
                "usage_unknown": unknown}
    return {"batch_id": batch_id, "offline_only": True,
            "completed": sum(row["state"] == "COMPLETED" for row in rows.values()),
            "performance_success": sum(row.get("meets_spec") is True for row in rows.values()),
            "failed_terminal": sum(row["state"] == "FAILED_TERMINAL" for row in rows.values()),
            "blocked": sum(row["state"] == "BLOCKED" for row in rows.values()),
            "interrupted_uncertain": sum(row["state"] == "INTERRUPTED_UNCERTAIN" for row in rows.values()),
            "api_usage": {"logical_requests": len(logical),
                          "physical_attempts": len(started),
                          "successful_responses": sum(item.get("outcome") == "success" for item in finished),
                          "failed_attempts": sum(item.get("outcome") != "success" for item in finished),
                          "reported_total_tokens": sum(item["total_tokens"] for item in reported),
                          "request_seconds": sum(float(item.get("latency_seconds") or 0) for item in finished),
                          "retry_wait_seconds": sum(float(item.get("seconds") or 0) for item in waits),
                          "logical_wall_seconds": sum(float(item.get("wall_seconds") or 0)
                                                      for item in logical_finished),
                          "usage_unknown": usage_unknown,
                          "total_tokens": None if usage_unknown else sum(item["total_tokens"] for item in reported)},
            "jobs": {key: {"state": row["state"], "pipeline_success": row["pipeline_success"],
                           "meets_spec": row["meets_spec"], "attempts": len(row["attempts"]),
                           "api_usage": per_job_usage(key)}
                     for key, row in rows.items()}}
