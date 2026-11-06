"""Sequential V2 scheduler with mock and explicitly authorized worker paths."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable

from .schema import load_manifest, verify_job
from .storage import append_jsonl, atomic_json, batch_lock, read_json, read_jsonl_strict, sha256_file


class UnverifiedOrphanError(RuntimeError):
    """The child (or one of its descendants) may still be doing work."""


COMPLETION_STOP_REASONS = {
    "meets_spec", "max_iterations", "initial_d0_meets_spec",
}
_CURRENT_MIRROR_STATUS_FIELDS = (
    "parameter_valid", "simulation_valid", "functional_valid", "meets_spec",
)


def classify_batch_success(circuit_type: str, payload: dict) -> dict:
    """Classify numeric and CurrentMirror functional success without guessing."""
    numerical = payload.get("meets_spec") is True
    if circuit_type != "CurrentMirror":
        return {
            "numerical_spec_success": numerical,
            "functional_joint_success": None,
            "functional_unverified": False,
        }
    values = tuple(payload.get(name) for name in _CURRENT_MIRROR_STATUS_FIELDS)
    joint = payload.get("joint_success")
    if not all(type(value) is bool for value in values) or type(joint) is not bool:
        if joint is True:
            raise ValueError(
                "CurrentMirror joint_success lacks explicit validity evidence"
            )
        return {
            "numerical_spec_success": numerical,
            "functional_joint_success": None,
            "functional_unverified": True,
        }
    expected = all(values)
    if joint is not expected:
        raise ValueError(
            "CurrentMirror joint_success contradicts explicit validity fields"
        )
    return {
        "numerical_spec_success": numerical,
        "functional_joint_success": joint,
        "functional_unverified": False,
    }


def _current_mirror_stop_is_consistent(stop_reason: object, classification: dict) -> bool:
    claims_success = stop_reason in {"meets_spec", "initial_d0_meets_spec"}
    return claims_success is (
        classification["functional_joint_success"] is True
    )


def plan_batch(manifest_path: str | Path) -> dict:
    """Pure read-only preflight; no AL, SPICE, D0, or LLM calls."""
    manifest = load_manifest(manifest_path)
    status = ("READY_FOR_AUTHORIZATION"
              if manifest["schema_version"] == "batch_runner_v2b2a_candidate_v1"
              else "READY_FOR_OFFLINE_MOCK")
    return {"batch_id": manifest["batch_id"], "status": status,
            "manifest_sha256": manifest["manifest_sha256"],
            "source_revision": manifest.get("source_revision"),
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


def _record_verified_isolation(directory: Path, state: dict, job_id: str) -> None:
    row = state["jobs"][job_id]
    if row.get("isolation_verified") is not True:
        row["isolation_verified"] = True
        _save(directory, state)
        _event(directory, job_id, "ISOLATED_VERIFIED", row.get("error_code"))


def _initial_state(
    manifest: dict, *, offline_only: bool,
    authorization: dict | None = None,
    execution_scope: str | None = None,
) -> dict:
    return {"batch_id": manifest["batch_id"],
            "manifest_sha256": manifest["manifest_sha256"],
            "offline_only": offline_only,
            "execution_scope": (execution_scope if execution_scope is not None else
                                "offline_mock" if authorization is None
                                else authorization["execution_scope"]),
            "authorization_id": (None if authorization is None
                                 else authorization["authorization_id"]),
            "authorization_sha256": (None if authorization is None
                                     else authorization["authorization_sha256"]),
            "source_revision": manifest.get("source_revision"),
            "jobs": {job["job_id"]: {
                "state": "PENDING", "attempts": [], "circuit_type": job["circuit_type"],
                "circuit_id": job["circuit_id"], "configuration_id": job["configuration_id"],
                "config_sha256": job["config_sha256"],
                "d0_sha256": job["d0_sha256"], "source_sha256": job["source_sha256"],
                "max_iterations": job["max_iterations"], "seed": job["seed"],
                "provider": manifest["provider"], "model": manifest["model"],
                "pipeline_success": None, "meets_spec": None,
                "parameter_valid": None, "simulation_valid": None,
                "functional_valid": None, "joint_success": None,
                "numerical_spec_success": None,
                "functional_joint_success": None,
                "functional_unverified": None,
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
    try:
        classification = classify_batch_success(row["circuit_type"], result)
    except ValueError:
        return False
    if row["circuit_type"] == "CurrentMirror" and not (
            _current_mirror_stop_is_consistent(
                result.get("stop_reason"), classification)):
        return False
    status_matches = all(
        result.get(name) == row.get(name)
        for name in (
            "parameter_valid", "simulation_valid", "functional_valid",
            "meets_spec", "joint_success", "numerical_spec_success",
            "functional_joint_success", "functional_unverified",
        )
        if name in result or name in row
    )
    return (result.get("attempt_id") == attempt["attempt_id"]
            and result.get("pipeline_success") is True
            and result.get("stop_reason") in COMPLETION_STOP_REASONS
            and type(result.get("meets_spec")) is bool
            and result.get("meets_spec") == row.get("meets_spec")
            and status_matches)


def _verify_safe_skip(directory: Path, job_id: str, row: dict) -> bool:
    if row.get("state") not in {"SKIPPED_JOB_TIMEOUT", "SKIPPED_JOB_BUDGET"} or (
            not row.get("attempts")):
        return False
    attempt = row["attempts"][-1]
    attempt_dir = directory / "jobs" / job_id / "attempts" / attempt["attempt_id"]
    result_path = attempt_dir / "worker_result.json"
    reaped_path = attempt_dir / "child_reaped.json"
    try:
        if (not result_path.is_file()
                or sha256_file(result_path) != attempt.get("worker_result_sha256")):
            return False
        result = read_json(result_path)
        reaped = read_json(reaped_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (result.get("state") == row["state"]
            and result.get("batch_id") == directory.name
            and result.get("job_id") == job_id
            and result.get("attempt_id") == attempt["attempt_id"]
            and result.get("safe_point") == (
                "rate_limit_wait" if row["state"] == "SKIPPED_JOB_TIMEOUT"
                else "per_job_cap")
            and reaped.get("attempt_id") == attempt["attempt_id"]
            and reaped.get("exit_confirmed") is True
            and reaped.get("exit_code") == 3)


def _confirmed_failed_child(directory: Path, job_id: str, row: dict,
                            manifest: dict, authorization: dict | None) -> bool:
    """Continue only after the failed attempt is reaped and batch authority is intact."""
    try:
        attempt = row["attempts"][-1]
        attempt_dir = directory / "jobs" / job_id / "attempts" / attempt["attempt_id"]
        path = attempt_dir / "child_reaped.json"
        if (attempt.get("attempt_directory") != str(attempt_dir)
                or attempt_dir.is_symlink() or path.is_symlink()):
            return False
        reaped = read_json(path)
        if reaped.get("attempt_id") != attempt["attempt_id"] or \
                reaped.get("exit_confirmed") is not True or \
                type(reaped.get("exit_code")) is not int:
            return False
        state = read_json(directory / "status.json")
        if state.get("offline_only") is False:
            intent_path = attempt_dir / "child_spawn_intent.json"
            if intent_path.is_symlink():
                return False
            intent = read_json(intent_path)
            if (intent.get("attempt_id") != attempt["attempt_id"]
                    or intent.get("job_id") != job_id
                    or intent.get("manifest_sha256", state["manifest_sha256"])
                    != state["manifest_sha256"]):
                return False
        ledger = read_jsonl_strict(directory / "api_usage.jsonl")
        started = [item for item in ledger if item.get("event") == "attempt_started"]
        finished = [item for item in ledger if item.get("event") == "attempt_finished"]
        key = lambda item: (item.get("job_id"), item.get("attempt_id"),
                            item.get("logical_id"), item.get("attempt"))
        started_keys, finished_keys = list(map(key, started)), list(map(key, finished))
        if (len(started_keys) != len(set(started_keys))
                or len(finished_keys) != len(set(finished_keys))
                or sorted(started_keys, key=str) != sorted(finished_keys, key=str)):
            return False
        job = next(item for item in manifest["jobs"] if item["job_id"] == job_id)
        execution_ids = [value for name, value in job.items()
                         if name.endswith("_execution_id")]
        if len(execution_ids) > 1:
            return False
        if execution_ids:
            native = directory / execution_ids[0]
            if (native.is_symlink() or not native.resolve().is_relative_to(directory.resolve())
                    or (native / ".lock").exists()):
                return False
            if native.exists():
                identity_path = native / "identity.json"
                physical_path = native / "physical_attempt_ledger.jsonl"
                if identity_path.is_symlink() or physical_path.is_symlink():
                    return False
                identity = read_json(identity_path)
                limit = manifest.get("resource_limits", {}).get(
                    "per_circuit_native_attempts_max")
                if (identity.get("circuit_id") != job["circuit_id"]
                        or identity.get("stage") != "al"
                        or type(limit) is not int or identity.get("limit") != limit
                        or identity.get("extra", {}).get("batch_id") != manifest["batch_id"]
                        or identity["extra"].get("config_sha256") != job["config_sha256"]
                        or identity["extra"].get("d0_sha256") != job["d0_sha256"]):
                    return False
                from ActiveLearning.sc_sample_hold_generic import canonical_sha256
                identity_sha = canonical_sha256(identity)
                physical = read_jsonl_strict(physical_path)
                reserved = [item for item in physical if item.get("event") == "reserved"]
                closed = [item for item in physical if item.get("event") == "finished"]
                physical_key = lambda item: (item.get("attempt"), item.get("vector_sha256"))
                reservation_keys = list(map(physical_key, reserved))
                if (len(reserved) > limit or len(reserved) != len(closed)
                        or len(set(reservation_keys)) != len(reservation_keys)
                        or [item.get("attempt") for item in reserved]
                        != list(range(1, len(reserved) + 1))
                        or any(item.get("identity_sha256") != identity_sha for item in reserved)
                        or any(item.get("outcome") not in {
                            "complete", "timeout_reaped", "failed_reaped"} for item in closed)
                        or sorted(reservation_keys, key=str)
                        != sorted(map(physical_key, closed), key=str)):
                    return False
        if authorization is not None:
            from .authorization import (
                validate_authorization, validate_authorization_claim,
                verify_published_source_closure,
            )
            current = load_manifest(manifest["manifest_path"])
            if current["manifest_sha256"] != manifest["manifest_sha256"]:
                return False
            approved = validate_authorization(
                current, authorization["authorization_path"],
                authorization["authorization_sha256"], directory.parent)
            validate_authorization_claim(approved)
            verify_published_source_closure(current)
            verify_job(current, job)
    except Exception:
        return False
    return True


def _run_batch(
    manifest: dict, output_root: str | Path,
    worker: Callable[[dict, Path], dict], *, offline_only: bool,
    authorization: dict | None = None,
    execution_scope: str | None = None,
    abandoned_orphan_job_ids: set[str] | frozenset[str] | None = None,
) -> dict:
    abandoned_orphans = frozenset(abandoned_orphan_job_ids or ())
    if any(not isinstance(job_id, str) for job_id in abandoned_orphans):
        raise TypeError("abandoned orphan job IDs must be strings")
    directory = _batch_dir(output_root, manifest["batch_id"])
    if directory.is_symlink():
        raise ValueError("batch directory must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True)
    with batch_lock(directory / ".runner.lock"):
        status_path = directory / "status.json"
        if status_path.exists():
            state = read_json(status_path)
            if state.get("manifest_sha256") != manifest["manifest_sha256"] or \
                    state.get("offline_only") is not offline_only:
                raise ValueError("batch identity or execution scope changed")
            expected_authorization = (None if authorization is None
                                      else authorization["authorization_sha256"])
            if state.get("authorization_sha256") != expected_authorization:
                raise ValueError("batch authorization changed")
            if execution_scope is not None and state.get("execution_scope") != execution_scope:
                raise ValueError("batch execution scope changed")
            if set(state.get("jobs", {})) != {job["job_id"] for job in manifest["jobs"]}:
                raise ValueError("persisted job set differs from manifest")
        else:
            if any(child.name != ".runner.lock" for child in directory.iterdir()):
                raise ValueError("refusing to adopt an existing non-V2 batch directory")
            state = _initial_state(
                manifest, offline_only=offline_only, authorization=authorization,
                execution_scope=execution_scope)
            atomic_json(directory / "manifest.json", manifest)
            _save(directory, state)
        orphan_rows = {
            job_id for job_id, row in state["jobs"].items()
            if row.get("state") == "INTERRUPTED_UNCERTAIN"
            and row.get("error_code") == "ORPHAN_UNVERIFIED"
        }
        invalid_abandonments = abandoned_orphans - orphan_rows
        if invalid_abandonments:
            raise ValueError(
                "only persisted INTERRUPTED_UNCERTAIN/ORPHAN_UNVERIFIED jobs "
                f"may be abandoned: {sorted(invalid_abandonments)}")
        if state.get("orphan_unverified") and not orphan_rows:
            raise UnverifiedOrphanError(
                "persisted orphan marker has no matching interrupted job; "
                "manual review required")
        verified_orphans = {
            job_id for job_id in orphan_rows
            if _confirmed_failed_child(directory, job_id, state["jobs"][job_id],
                                       manifest, authorization)
        }
        unapproved_orphans = orphan_rows - verified_orphans - abandoned_orphans
        if unapproved_orphans:
            raise UnverifiedOrphanError(
                "an earlier child may still be running; explicitly abandon only "
                f"the reviewed jobs to continue: {sorted(unapproved_orphans)}")
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
                if _confirmed_failed_child(directory, job_id, row, manifest, authorization):
                    _record_verified_isolation(directory, state, job_id)
                    continue
                break
            if row["state"] == "INTERRUPTED_UNCERTAIN":
                if job_id in abandoned_orphans:
                    continue
                if _confirmed_failed_child(directory, job_id, row, manifest, authorization):
                    _record_verified_isolation(directory, state, job_id)
                    continue
                break
            if row["state"] in {"SKIPPED_JOB_TIMEOUT", "SKIPPED_JOB_BUDGET"}:
                if not _verify_safe_skip(directory, job_id, row):
                    row["state"] = "INTERRUPTED_UNCERTAIN"
                    row["error_code"] = "SAFE_STOP_ARTIFACT_INVALID"
                    _save(directory, state)
                    _event(directory, job_id, "INTERRUPTED_UNCERTAIN", row["error_code"])
                    break
                continue
            if row["state"] in {"FAILED_TERMINAL", "BLOCKED"} and row["attempts"]:
                if not offline_only and not _confirmed_failed_child(
                        directory, job_id, row, manifest, authorization):
                    break
                if not offline_only:
                    _record_verified_isolation(directory, state, job_id)
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
            if manifest["retry_policy"].get("rate_limit_mode") == "job_deadline":
                started = time.monotonic()
                row["attempts"][-1]["job_started_monotonic"] = started
                row["attempts"][-1]["job_deadline_monotonic"] = (
                    started + manifest["resource_limits"]["worker_timeout_seconds"])
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
                if _confirmed_failed_child(directory, job_id, row, manifest, authorization):
                    _record_verified_isolation(directory, state, job_id)
                    continue
                break
            except Exception as error:
                # Unexpected worker exceptions may have consumed LLM/SPICE work.
                row["state"] = "INTERRUPTED_UNCERTAIN"
                row["error_code"] = type(error).__name__
                _save(directory, state)
                _event(directory, job_id, "INTERRUPTED_UNCERTAIN", row["error_code"])
                if _confirmed_failed_child(directory, job_id, row, manifest, authorization):
                    _record_verified_isolation(directory, state, job_id)
                    continue
                break
            if isinstance(outcome, dict) and outcome.get("state") in {
                    "SKIPPED_JOB_TIMEOUT", "SKIPPED_JOB_BUDGET"}:
                row["state"] = outcome["state"]
                row["pipeline_success"] = False
                row["meets_spec"] = None
                row["stop_reason"] = ("job_deadline" if outcome["state"] == "SKIPPED_JOB_TIMEOUT"
                                      else "per_job_physical_attempt_cap")
                row["attempts"][-1]["worker_result_sha256"] = outcome["worker_result_sha256"]
                _save(directory, state)
                _event(directory, job_id, outcome["state"], row["stop_reason"])
                continue
            if isinstance(outcome, dict) and outcome.get("state") == "FAILED_TERMINAL":
                row["state"] = "FAILED_TERMINAL"
                row["pipeline_success"] = False
                row["meets_spec"] = None
                row["error_code"] = str(outcome.get("error_code") or "MOCK_TERMINAL")
                _save(directory, state)
                _event(directory, job_id, "FAILED_TERMINAL", row["error_code"])
                if not offline_only and not _confirmed_failed_child(
                        directory, job_id, row, manifest, authorization):
                    break
                if not offline_only:
                    _record_verified_isolation(directory, state, job_id)
                continue
            if isinstance(outcome, dict) and outcome.get("state") == "BLOCKED":
                row["state"] = "BLOCKED"
                row["pipeline_success"] = False
                row["meets_spec"] = None
                row["error_code"] = str(outcome.get("error_code") or "REAL_AL_BLOCKED")
                _save(directory, state)
                _event(directory, job_id, "BLOCKED", row["error_code"])
                if not offline_only and not _confirmed_failed_child(
                        directory, job_id, row, manifest, authorization):
                    break
                if not offline_only:
                    _record_verified_isolation(directory, state, job_id)
                continue
            try:
                classification = (
                    classify_batch_success(job["circuit_type"], outcome)
                    if isinstance(outcome, dict) else None
                )
            except ValueError:
                classification = None
            if (not isinstance(outcome, dict)
                    or outcome.get("stop_reason") not in COMPLETION_STOP_REASONS
                    or type(outcome.get("meets_spec")) is not bool
                    or classification is None
                    or (job["circuit_type"] == "CurrentMirror"
                        and not _current_mirror_stop_is_consistent(
                            outcome.get("stop_reason"), classification))):
                row["state"] = "INTERRUPTED_UNCERTAIN"
                row["error_code"] = "INVALID_WORKER_RESULT"
                _save(directory, state)
                _event(directory, job_id, "INTERRUPTED_UNCERTAIN", row["error_code"])
                if _confirmed_failed_child(directory, job_id, row, manifest, authorization):
                    _record_verified_isolation(directory, state, job_id)
                    continue
                break
            # After the mock worker returns, any persistence failure must leave
            # RUNNING for uncertain restart reconciliation, not claim completion.
            try:
                result = {"attempt_id": attempt_id, "run_id": outcome.get("run_id") or attempt_id,
                          "pipeline_success": True, "meets_spec": outcome["meets_spec"],
                          "parameter_valid": outcome.get("parameter_valid"),
                          "simulation_valid": outcome.get("simulation_valid"),
                          "functional_valid": outcome.get("functional_valid"),
                          "joint_success": outcome.get("joint_success"),
                          **classification,
                          "stop_reason": outcome["stop_reason"],
                          "iterations_completed": outcome.get("iterations_completed"),
                          "final_metrics": outcome.get("final_metrics"),
                          "d0_record_count": outcome.get("d0_record_count"),
                          "result_path": outcome.get("result_path"),
                          "al_result_sha256": outcome.get("al_result_sha256")}
                if execution_scope in {"sample_hold_production", "sc_integrator_production"}:
                    result.update({name: outcome.get(name) for name in (
                        "llm_calls", "total_tokens", "total_timing", "physical_ledger_path",
                    )})
                result_path = attempt_dir / "result.json"
                atomic_json(result_path, result)
                row["attempts"][-1]["result_sha256"] = sha256_file(result_path)
                row["state"] = "COMPLETED"
                row["pipeline_success"] = True
                row["meets_spec"] = outcome["meets_spec"]
                row["parameter_valid"] = outcome.get("parameter_valid")
                row["simulation_valid"] = outcome.get("simulation_valid")
                row["functional_valid"] = outcome.get("functional_valid")
                row["joint_success"] = outcome.get("joint_success")
                row.update(classification)
                row["stop_reason"] = outcome["stop_reason"]
                row["final_metrics"] = outcome.get("final_metrics")
                row["d0_record_count"] = outcome.get("d0_record_count")
                if execution_scope in {"sample_hold_production", "sc_integrator_production"}:
                    row.update({name: outcome.get(name) for name in (
                        "llm_calls", "total_tokens", "total_timing", "physical_ledger_path",
                    )})
                _save(directory, state)
                _event(directory, job_id, "COMPLETED", attempt_id)
            except Exception:
                raise  # A restart will classify RUNNING as uncertain.
        summary = report_batch(manifest["batch_id"], output_root)
        atomic_json(directory / "summary.json", summary)
        return summary


def run_mock_batch(
    manifest_path: str | Path, output_root: str | Path,
    worker: Callable[[dict, Path], dict],
    *,
    abandoned_orphan_job_ids: set[str] | frozenset[str] | None = None,
) -> dict:
    """Execute only an explicitly injected mock worker."""
    if not callable(worker):
        raise TypeError("an injected mock worker is required")
    manifest = load_manifest(manifest_path)
    return _run_batch(
        manifest, output_root, worker, offline_only=True,
        abandoned_orphan_job_ids=abandoned_orphan_job_ids,
    )


def run_authorized_batch(
    manifest_path: str | Path,
    output_root: str | Path,
    authorization_path: str | Path,
    expected_authorization_sha256: str,
    *,
    enable_production: bool = False,
    offline_scenarios: dict[str, str] | None = None,
    abandoned_orphan_job_ids: set[str] | frozenset[str] | None = None,
) -> dict:
    """Run only a B2A batch whose exact approval digest was pinned externally.

    This function is deliberately absent from the CLI. Offline integration
    approvals can run only built-in fixtures. Production additionally requires
    a production-scoped approval and the explicit in-process enable flag.
    """
    from .al_process import run_real_al_child
    from .authorization import (
        ProductionExecutionDisabledError, claim_authorization,
        validate_authorization, verify_published_source_closure,
    )

    manifest = load_manifest(manifest_path)
    authorization = validate_authorization(
        manifest, authorization_path, expected_authorization_sha256, output_root)
    if authorization["execution_scope"] == "production":
        if enable_production is not True:
            raise ProductionExecutionDisabledError(
                "production execution requires a separate explicit enable decision")
        if offline_scenarios is not None:
            raise ValueError("production execution cannot accept offline scenarios")
        verify_published_source_closure(manifest)
    else:
        if enable_production:
            raise ValueError("offline authorization cannot enable production")
        if not isinstance(offline_scenarios, dict) or \
                set(offline_scenarios) != {job["job_id"] for job in manifest["jobs"]}:
            raise ValueError("offline integration requires one fixed scenario per job")
    authorization["authorization_claim_path"] = str(claim_authorization(authorization))

    def authorized_worker(job: dict, attempt: Path) -> dict:
        scenario = (None if authorization["execution_scope"] == "production"
                    else offline_scenarios[job["job_id"]])
        return run_real_al_child(
            manifest, job, attempt, authorization,
            offline_scenario=scenario, production_enabled=enable_production,
        )

    return _run_batch(
        manifest, output_root, authorized_worker,
        offline_only=authorization["execution_scope"] != "production",
        authorization=authorization,
        abandoned_orphan_job_ids=abandoned_orphan_job_ids,
    )


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
                     or any(item.get("usage_unknown") is True for item in finished)
                     or any(row["state"] == "INTERRUPTED_UNCERTAIN" for row in rows.values()))

    def report_classification(row: dict) -> dict:
        """Derive V2 reporting fields without rewriting historical state."""
        if row["state"] != "COMPLETED":
            return {
                "numerical_spec_success": row.get("numerical_spec_success"),
                "functional_joint_success": row.get("functional_joint_success"),
                "functional_unverified": row.get("functional_unverified"),
                "success_evidence_inconsistent": False,
            }
        try:
            classified = classify_batch_success(row["circuit_type"], row)
            classified["success_evidence_inconsistent"] = False
            return classified
        except ValueError:
            # Old records can contain partial or contradictory derived fields.
            # Never promote those to functional success; report them separately.
            return {
                "numerical_spec_success": row.get("meets_spec") is True,
                "functional_joint_success": None,
                "functional_unverified": row["circuit_type"] == "CurrentMirror",
                "success_evidence_inconsistent": True,
            }

    classifications = {key: report_classification(row)
                       for key, row in rows.items()}

    def per_job_usage(job_id: str) -> dict:
        own_started = [item for item in started if item.get("job_id") == job_id]
        own_finished = [item for item in finished if item.get("job_id") == job_id]
        unknown = (len(own_started) != len(own_finished)
                   or any(item.get("usage_unknown") is True for item in own_finished)
                   or rows[job_id]["state"] == "INTERRUPTED_UNCERTAIN")
        return {"physical_attempts": len(own_started),
                "reported_total_tokens": sum(item["total_tokens"] for item in own_finished
                                             if type(item.get("total_tokens")) is int),
                "request_seconds": sum(float(item.get("latency_seconds") or 0) for item in own_finished),
                "usage_unknown": unknown,
                "total_tokens": None if unknown else sum(
                    item["total_tokens"] for item in own_finished
                    if type(item.get("total_tokens")) is int)}
    summary = {"batch_id": batch_id, "offline_only": state.get("offline_only", True),
            "execution_scope": state.get("execution_scope", "offline_mock"),
            "authorization_id": state.get("authorization_id"),
            "completed": sum(row["state"] == "COMPLETED" for row in rows.values()),
            "performance_success": sum(row.get("meets_spec") is True for row in rows.values()),
            "numerical_spec_success": sum(
                item["numerical_spec_success"] is True
                for item in classifications.values()),
            "current_mirror_completed": sum(
                row["state"] == "COMPLETED"
                and row["circuit_type"] == "CurrentMirror"
                for row in rows.values()),
            "functional_joint_success": sum(
                row["state"] == "COMPLETED"
                and row["circuit_type"] == "CurrentMirror"
                and classifications[key]["functional_joint_success"] is True
                for key, row in rows.items()),
            "functional_joint_failure": sum(
                row["state"] == "COMPLETED"
                and row["circuit_type"] == "CurrentMirror"
                and classifications[key]["functional_unverified"] is False
                and classifications[key]["functional_joint_success"] is False
                for key, row in rows.items()),
            "functional_unverified": sum(
                row["state"] == "COMPLETED"
                and row["circuit_type"] == "CurrentMirror"
                and classifications[key]["functional_unverified"] is True
                for key, row in rows.items()),
            "success_evidence_inconsistent": sum(
                item["success_evidence_inconsistent"] is True
                for item in classifications.values()),
            "failed_terminal": sum(row["state"] == "FAILED_TERMINAL" for row in rows.values()),
            "blocked": sum(row["state"] == "BLOCKED" for row in rows.values()),
            "interrupted_uncertain": sum(row["state"] == "INTERRUPTED_UNCERTAIN" for row in rows.values()),
            "skipped_job_timeout": sum(row["state"] == "SKIPPED_JOB_TIMEOUT" for row in rows.values()),
            "skipped_job_budget": sum(row["state"] == "SKIPPED_JOB_BUDGET" for row in rows.values()),
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
                           "parameter_valid": row.get("parameter_valid"),
                           "simulation_valid": row.get("simulation_valid"),
                           "functional_valid": row.get("functional_valid"),
                           "meets_spec": row["meets_spec"],
                           "joint_success": row.get("joint_success"),
                           "numerical_spec_success": classifications[key]["numerical_spec_success"],
                           "functional_joint_success": classifications[key]["functional_joint_success"],
                           "functional_unverified": classifications[key]["functional_unverified"],
                           "success_evidence_inconsistent": classifications[key]["success_evidence_inconsistent"],
                           "attempts": len(row["attempts"]),
                           "error_code": row.get("error_code"),
                           "isolation_verified": row.get("isolation_verified", False),
                           "final_metrics": row.get("final_metrics"),
                           "d0_record_count": row.get("d0_record_count"),
                           "api_usage": per_job_usage(key)}
                     for key, row in rows.items()}}
    if state.get("execution_scope") in {"sample_hold_production", "sc_integrator_production"}:
        usage_key = ("sample_hold_usage" if state["execution_scope"] == "sample_hold_production"
                     else "sc_integrator_usage")
        for key, row in rows.items():
            summary["jobs"][key].update({name: row.get(name) for name in (
                "llm_calls", "total_tokens", "total_timing", "physical_ledger_path",
            )})
            tokens = (row.get("total_tokens") or {}).get("total_tokens")
            summary["jobs"][key]["api_usage"] = {
                "logical_requests": row.get("llm_calls"),
                "physical_attempts": None,
                "reported_total_tokens": tokens,
                "usage_unknown": row["state"] != "COMPLETED",
                "total_tokens": tokens if row["state"] == "COMPLETED" else None,
            }
        completed = [row for row in rows.values() if row["state"] == "COMPLETED"]
        summary[usage_key] = {
            "llm_calls": sum(row.get("llm_calls") or 0 for row in completed),
            "reported_total_tokens": sum(
                (row.get("total_tokens") or {}).get("total_tokens") or 0
                for row in completed),
            "usage_unknown": any(row["state"] in {"FAILED_TERMINAL", "INTERRUPTED_UNCERTAIN"}
                                 for row in rows.values()),
        }
        # The existing AL usage log counts completed calls and tokens, but not
        # hidden transport attempts. Never report zero physical requests as fact.
        summary["api_usage"] = {
            "logical_requests": summary[usage_key]["llm_calls"],
            "physical_attempts": None,
            "successful_responses": summary[usage_key]["llm_calls"],
            "failed_attempts": None,
            "reported_total_tokens": summary[usage_key]["reported_total_tokens"],
            "request_seconds": None, "retry_wait_seconds": None,
            "logical_wall_seconds": sum(
                (row.get("total_timing") or {}).get("total_seconds") or 0
                for row in completed),
            "usage_unknown": summary[usage_key]["usage_unknown"],
            "total_tokens": (None if summary[usage_key]["usage_unknown"]
                             else summary[usage_key]["reported_total_tokens"]),
        }
    return summary
