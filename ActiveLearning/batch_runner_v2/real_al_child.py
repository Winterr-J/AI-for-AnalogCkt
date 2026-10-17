"""Authorized real-AL child boundary with a fixed offline integration fixture."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .authorization import (
    validate_authorization, validate_authorization_claim,
    verify_published_source_closure,
)
from .gemini_recovery import (
    GeminiRecovery, PhysicalAttemptLimitError, ProviderQuotaError,
    ProviderTerminalError,
)
from .llm_bridge import GeminiALClient, build_gemini_chat_transport
from .schema import _load_config, load_manifest, verify_job
from .storage import atomic_json, read_json, sha256_file


_INVOCATION_KEYS = {
    "schema_version", "batch_id", "job_id", "attempt_id",
    "manifest_path", "manifest_sha256", "authorization_id",
    "authorization_sha256", "attempt_directory", "offline_scenario",
}
_OFFLINE_SCENARIOS = {
    "success", "429_success", "auth_terminal", "terminal", "uncertain",
}


class _OfflineRateLimit(Exception):
    status_code = 429
    headers = {"Retry-After": "0.01"}


class _OfflineAuthenticationFailure(Exception):
    status_code = 401


def _read_invocation(path: str | Path) -> tuple[dict, Path]:
    invocation_path = Path(path)
    if invocation_path.is_symlink():
        raise ValueError("invocation must not be a symbolic link")
    invocation_path = invocation_path.resolve(strict=True)
    invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
    if not isinstance(invocation, dict) or set(invocation) != _INVOCATION_KEYS:
        raise ValueError("invalid real-AL invocation fields")
    if invocation["schema_version"] != "batch_runner_v2b2a_invocation_v1":
        raise ValueError("unsupported real-AL invocation schema")
    attempt = Path(invocation["attempt_directory"])
    if attempt.is_symlink():
        raise ValueError("attempt directory must not be a symbolic link")
    attempt = attempt.resolve(strict=True)
    if invocation_path.parent != attempt or invocation["attempt_id"] != attempt.name:
        raise ValueError("invocation attempt identity mismatch")
    scenario = invocation["offline_scenario"]
    if scenario is not None and scenario not in _OFFLINE_SCENARIOS:
        raise ValueError("unsupported offline real-AL scenario")
    return invocation, attempt


def _asset_snapshot(manifest: dict, job: dict) -> dict[str, str]:
    repository_root = Path(manifest["repository_root"])
    code_root = Path(manifest["code_root"])
    result = {
        f"job:{name}": sha256_file(repository_root / job[name])
        for name in ("config_path", "d0_path", "source_path")
    }
    result.update({
        f"source:{name}": sha256_file(code_root / name)
        for name in manifest["source_closure"]
    })
    return result


def _identity(invocation: dict, manifest: dict, job: dict, approval: dict) -> dict:
    return {
        "batch_id": invocation["batch_id"],
        "job_id": invocation["job_id"],
        "attempt_id": invocation["attempt_id"],
        "circuit_type": job["circuit_type"],
        "circuit_id": job["circuit_id"],
        "configuration_id": job["configuration_id"],
        "config_sha256": job["config_sha256"],
        "d0_sha256": job["d0_sha256"],
        "source_sha256": job["source_sha256"],
        "target_spec_sha256": job["target_spec_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "source_revision": manifest["source_revision"],
        "authorization_id": approval["authorization_id"],
        "authorization_sha256": approval["authorization_sha256"],
    }


def _validate_scheduler_state(
    invocation: dict, manifest: dict, approval: dict, attempt: Path,
) -> None:
    output_root = attempt.parents[4]
    expected_attempt = (output_root / manifest["batch_id"] / "jobs" /
                        invocation["job_id"] / "attempts" /
                        invocation["attempt_id"]).resolve()
    if expected_attempt != attempt:
        raise PermissionError("attempt is outside the authorized scheduler layout")
    try:
        state = read_json(output_root / manifest["batch_id"] / "status.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise PermissionError("scheduler state is missing or invalid") from error
    if state.get("manifest_sha256") != manifest["manifest_sha256"] or \
            state.get("authorization_sha256") != approval["authorization_sha256"]:
        raise PermissionError("scheduler state identity differs from authorization")
    row = (state.get("jobs") or {}).get(invocation["job_id"])
    if not isinstance(row, dict) or row.get("state") != "RUNNING" or not row.get("attempts") \
            or row["attempts"][-1].get("attempt_id") != invocation["attempt_id"]:
        raise PermissionError("attempt is not the scheduler's active RUNNING attempt")


def _recovery(manifest: dict, job: dict, attempt: Path) -> GeminiRecovery:
    retry = manifest["retry_policy"]
    return GeminiRecovery(
        attempt.parents[3], provider=manifest["provider"], model=manifest["model"],
        credential_scope=manifest["credential_scope"],
        max_physical_attempts=manifest["resource_limits"]["max_physical_attempts"],
        request_timeout=retry["request_timeout_seconds"],
        max_attempts=retry["max_attempts"],
        max_elapsed=retry["max_elapsed_seconds"],
        job_id=job["job_id"], attempt_id=attempt.name,
    )


def _write_al_summary(attempt: Path, job: dict, identity: dict, summary: dict) -> Path:
    run_id = summary["run_id"]
    run_dir = attempt / "al_runs" / job["circuit_type"] / job["circuit_id"] / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    path = run_dir / "summary.json"
    atomic_json(path, {**identity, **summary})
    return path


def _offline_fixture(
    scenario: str, invocation: dict, manifest: dict, job: dict,
    approval: dict, attempt: Path,
) -> dict:
    """Exercise the production boundary without importing AL/SPICE/provider SDKs."""
    if scenario == "uncertain":
        atomic_json(attempt / "offline_partial.json", {"attempt_id": attempt.name})
        os._exit(7)
    identity = _identity(invocation, manifest, job, approval)
    if scenario == "terminal":
        return {**identity, "state": "FAILED_TERMINAL",
                "error_code": "OFFLINE_SYNTHETIC_TERMINAL"}

    repository_root = Path(manifest["repository_root"])
    config = _load_config(repository_root / job["config_path"])
    d0_rows = [json.loads(line) for line in
               (repository_root / job["d0_path"]).read_text(encoding="utf-8").splitlines()
               if line.strip()]
    if not d0_rows or not all(isinstance(row, dict) for row in d0_rows):
        raise ValueError("offline fixture requires a nonempty existing D0")

    physical_calls = 0
    def send(_messages, _timeout):
        nonlocal physical_calls
        physical_calls += 1
        if scenario == "429_success" and physical_calls == 1:
            raise _OfflineRateLimit()
        if scenario == "auth_terminal":
            raise _OfflineAuthenticationFailure()
        return {"choices": [{"message": {"content": "offline-seed"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1,
                          "total_tokens": 3}}

    usage = []
    client = GeminiALClient(_recovery(manifest, job, attempt), send, manifest["model"])
    selected_seed = client("OFFLINE_B2A_FIXED_PROMPT", usage_log=usage)
    # Fake SPICE is intentionally fixed and local. It records selected metrics;
    # it does not reproduce or replace the optimization algorithm.
    target_specs = config["target_specs"]
    metrics = {name: float(spec["value"]) for name, spec in target_specs.items()}
    run_id = f"offline_{attempt.name}"
    summary = {
        "run_id": run_id,
        "al_completed": True,
        "simulation_valid": True,
        "meets_spec": True,
        "stop_reason": "meets_spec",
        "iterations_completed": 1,
        "final_metrics": metrics,
        "d0_record_count": len(d0_rows),
        "selected_seed": selected_seed,
        "fixture_physical_calls": physical_calls,
    }
    result_path = _write_al_summary(attempt, job, identity, summary)
    return {**identity, **summary, "state": "COMPLETED",
            "result_path": str(result_path),
            "al_result_sha256": sha256_file(result_path)}


def _production_run(
    invocation: dict, manifest: dict, job: dict, approval: dict, attempt: Path,
) -> dict:
    if os.getenv("BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256") != \
            approval["authorization_sha256"]:
        raise PermissionError("production execution switch is not pinned to this approval")
    from ActiveLearning import main as al_main

    repository_root = Path(manifest["repository_root"])
    config_path = (repository_root / job["config_path"]).resolve(strict=True)
    d0_path = (repository_root / job["d0_path"]).resolve(strict=True)
    d0_record_count = sum(1 for line in d0_path.read_text(encoding="utf-8").splitlines()
                          if line.strip())
    if d0_record_count < 1:
        raise ValueError("authorized D0 is empty")
    source_path = (repository_root / job["source_path"]).resolve(strict=True)
    resolved = al_main._resolve_paths(job["circuit_type"], job["circuit_id"], config_path)
    if Path(resolved["topology"]).resolve(strict=True) != source_path:
        raise ValueError("manifest source is not the topology used by the AL runtime")

    recovery = _recovery(manifest, job, attempt)
    transport = build_gemini_chat_transport(model=manifest["model"])
    llm_client = GeminiALClient(recovery, transport, manifest["model"])
    run_context = {}
    def capture_context(value):
        run_context.update(value)
        atomic_json(attempt / "al_run_context.json", {
            "run_id": value["run_id"],
            "run_directory": str(Path(value["run_directory"]).resolve()),
        })

    os.environ["AL_LLM_PROVIDER"] = "gemini"
    result = al_main.run_configured_active_learning(
        job["circuit_type"], job["circuit_id"], config_path=config_path,
        llm_client=llm_client, runs_root=attempt / "al_runs",
        run_context_callback=capture_context, require_existing_d0=True,
        expected_config_sha256=job["config_sha256"],
        expected_d0_sha256=job["d0_sha256"],
    )
    if not run_context:
        raise RuntimeError("AL runtime did not report its run identity")
    run_dir = Path(run_context["run_directory"]).resolve(strict=True)
    if not run_dir.is_relative_to(attempt) or run_dir.is_symlink():
        raise RuntimeError("AL run directory escaped the authorized attempt")
    result_path = run_dir / "summary.json"
    completed = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(completed, dict):
        raise RuntimeError("AL completion summary is invalid")
    final_record = result.get("best_or_final_record") or {}
    status = final_record.get("status") or {}
    return {
        **_identity(invocation, manifest, job, approval),
        "state": "COMPLETED", "run_id": run_context["run_id"],
        "al_completed": True,
        "simulation_valid": status.get("simulation_valid") is True,
        "meets_spec": status.get("meets_spec") is True,
        "stop_reason": result.get("stop_reason"),
        "iterations_completed": result.get("iterations_completed"),
        "final_metrics": final_record.get("metrics") or {},
        "d0_record_count": d0_record_count,
        "result_path": str(result_path),
        "al_result_sha256": sha256_file(result_path),
    }


def execute_invocation(
    invocation_path: str | Path,
    authorization_path: str | Path,
    expected_authorization_sha256: str,
) -> dict:
    invocation, attempt = _read_invocation(invocation_path)
    manifest = load_manifest(invocation["manifest_path"])
    if manifest["manifest_sha256"] != invocation["manifest_sha256"] or \
            manifest["batch_id"] != invocation["batch_id"]:
        raise ValueError("invocation manifest identity mismatch")
    approval = validate_authorization(
        manifest, authorization_path, expected_authorization_sha256,
        attempt.parents[4])
    validate_authorization_claim(approval)
    if approval["authorization_id"] != invocation["authorization_id"] or \
            approval["authorization_sha256"] != invocation["authorization_sha256"]:
        raise ValueError("invocation authorization identity mismatch")
    job = next((row for row in manifest["jobs"]
                if row["job_id"] == invocation["job_id"]), None)
    if job is None:
        raise ValueError("authorized job is absent from the manifest")
    _validate_scheduler_state(invocation, manifest, approval, attempt)
    verify_job(manifest, job)
    before = _asset_snapshot(manifest, job)
    scenario = invocation["offline_scenario"]
    if scenario is not None:
        if approval["execution_scope"] != "offline_integration":
            raise PermissionError("offline fixture requires offline-integration approval")
        result = _offline_fixture(scenario, invocation, manifest, job, approval, attempt)
    else:
        if approval["execution_scope"] != "production":
            raise PermissionError("production child requires production authorization")
        verify_published_source_closure(manifest)
        result = _production_run(invocation, manifest, job, approval, attempt)
    if _asset_snapshot(manifest, job) != before:
        raise RuntimeError("authorized source or input asset changed during execution")
    atomic_json(attempt / "worker_result.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Authorized Batch Runner V2 real-AL child")
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--authorization-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = execute_invocation(
            args.invocation, args.authorization, args.authorization_sha256)
    except (ProviderQuotaError, PhysicalAttemptLimitError) as error:
        invocation, _attempt = _read_invocation(args.invocation)
        atomic_json(_attempt / "worker_result.json", {
            "batch_id": invocation["batch_id"], "job_id": invocation["job_id"],
            "attempt_id": invocation["attempt_id"], "state": "BLOCKED",
            "error_code": type(error).__name__,
            "manifest_sha256": invocation["manifest_sha256"],
            "authorization_sha256": invocation["authorization_sha256"],
        })
        return 2
    except ProviderTerminalError as error:
        invocation, _attempt = _read_invocation(args.invocation)
        atomic_json(_attempt / "worker_result.json", {
            "batch_id": invocation["batch_id"], "job_id": invocation["job_id"],
            "attempt_id": invocation["attempt_id"], "state": "FAILED_TERMINAL",
            "error_code": type(error).__name__,
            "manifest_sha256": invocation["manifest_sha256"],
            "authorization_sha256": invocation["authorization_sha256"],
        })
        return 2
    return 2 if result.get("state") == "FAILED_TERMINAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
