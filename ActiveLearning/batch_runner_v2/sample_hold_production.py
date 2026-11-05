"""MTV2_SH_001 Ubuntu entry point over the existing Batch Runner V2 scheduler."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

import yaml

from ActiveLearning import main as al_main
from ActiveLearning.inverse_design.llm_client import _provider_settings
from ActiveLearning.onboarding import build_active_learning_circuit_config
from ActiveLearning.onboarding.sample_hold_batch import al_pilot_contract
from ActiveLearning.onboarding.sample_hold_ubuntu import OUTPUT_ROOT_ENV
from ActiveLearning.sample_hold_posix import ProcessGroupUncertain, run_isolated
from .runner import UnverifiedOrphanError, _run_batch, report_batch
from .schema import canonical_sha256, configuration_identity, verify_job
from .storage import atomic_json, read_json, sha256_file


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("MTV2_SH_001.json")
_CONFIGS = {
    "circuit_0259": "ActiveLearning/experiments/SampleHold/circuit_0259/five_iteration_validation_v1.yaml",
    "circuit_0263": "ActiveLearning/experiments/SampleHold/circuit_0263/five_iteration_validation_v1.yaml",
}


def inspect_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    """Static selection check; safe on Windows and without native imports."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (set(payload) != {"schema_version", "batch_id", "output_root",
                         "credential_scope", "continue_on_verified_failure", "jobs"}
            or payload["schema_version"] != "sample_hold_batch_v1"
            or payload["batch_id"] != "MTV2_SH_001"
            or payload["credential_scope"] != "bytecat_ubuntu"
            or payload["continue_on_verified_failure"] is not True
            or not isinstance(payload["jobs"], list)
            or len(payload["jobs"]) != 2):
        raise ValueError("MTV2_SH_001 identity or failure policy changed")
    output = PurePosixPath(payload["output_root"])
    if not output.is_absolute() or str(output) != "/home/ubuntu/sc_al_batches":
        raise ValueError("SampleHold Batch output root changed")
    expected = [("sc0259", "circuit_0259"), ("sc0263", "circuit_0263")]
    for job, (job_id, circuit_id) in zip(payload["jobs"], expected):
        if (not isinstance(job, dict)
                or job != {"job_id": job_id, "circuit_id": circuit_id,
                           "config_path": _CONFIGS[circuit_id], "max_iterations": 5}):
            raise ValueError("SampleHold Batch selection or five-iteration limit changed")
        config = yaml.safe_load((ROOT / job["config_path"]).read_text(encoding="utf-8"))
        settings = config.get("active_learning") or {}
        data = config.get("data") or {}
        pilot = config.get("sample_hold_pilot") or {}
        budget = pilot.get("budget") or {}
        if (config.get("circuit_info") != {"circuit_type": "SampleHold", "circuit_id": circuit_id}
                or settings.get("max_iterations") != 5
                or settings.get("stop_on_meets_spec") is not False
                or data.get("d0_mode") != "reuse"
                or data.get("initialize_if_missing") is not False
                or budget.get("physical_attempts_max") != 5
                or budget.get("automatic_retries") != 0
                or pilot.get("execution_id") == "sc0259_al_pilot_v1"
                or not pilot.get("execution_id", "").startswith("sc" + circuit_id[-4:] + "_")):
            raise ValueError("SampleHold frozen config, D0 or physical budget changed")
    return payload


def prepare(path: Path = DEFAULT_MANIFEST) -> tuple[dict, dict]:
    """Read-only Ubuntu preflight, including each circuit's existing AL guard."""
    spec = inspect_manifest(path)
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise RuntimeError("MTV2_SH_001 physical batch requires Ubuntu/Linux")
    batch_root = Path(spec["output_root"]).resolve() / spec["batch_id"]
    pilot_path = Path("/home/ubuntu/sc_al_pilots/sc0259_al_pilot_v1").resolve()
    if (batch_root == pilot_path or batch_root.is_relative_to(pilot_path)
            or pilot_path.is_relative_to(batch_root) or batch_root.is_relative_to(ROOT)):
        raise ValueError("Batch output overlaps the completed 0259 Pilot")
    os.environ[OUTPUT_ROOT_ENV] = str(batch_root)
    selected, _key, model, base_url = _provider_settings("openai", None, None, None)
    transport = os.getenv("AL_OPENAI_TRANSPORT", "responses").strip().lower()
    if (selected != "openai" or os.getenv("AL_LLM_PROVIDER", "openai").lower() != "openai"
            or transport not in {"chat_completions", "responses"}):
        raise ValueError("ByteCat OpenAI-compatible environment is not configured")
    jobs, preflights, sources = [], {}, {}
    for item in spec["jobs"]:
        circuit_id = item["circuit_id"]
        config_path = (ROOT / item["config_path"]).resolve(strict=True)
        runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
        paths, config, targets, d0_path = al_main._resolve_experiment(
            "SampleHold", circuit_id, config_path, runtime)
        al_main._check_independent_d0(config, d0_path)
        executor = al_main._validate_sample_hold_pilot(
            config, runtime, config_path, d0_path, targets,
            al_main._load_yaml(paths["performance_template"]),
        )
        preflights[item["job_id"]] = executor.runtime_preflight
        contract = al_pilot_contract(runtime, config_path, batch_root,
                                     executor.experiment_id, executor.budget)
        for name, digest in contract["source_hashes"].items():
            if name in sources and sources[name] != digest:
                raise ValueError("SampleHold circuit source identities conflict")
            sources[name] = digest
        source_path = paths["topology"].resolve(strict=True)
        config_sha = sha256_file(config_path)
        jobs.append({"job_id": item["job_id"], "circuit_type": "SampleHold",
                     "circuit_id": circuit_id,
                     "configuration_id": configuration_identity(config, config_sha),
                     "config_path": item["config_path"], "config_sha256": config_sha,
                     "d0_path": d0_path.resolve(strict=True).relative_to(ROOT).as_posix(),
                     "d0_sha256": sha256_file(d0_path),
                     "source_path": source_path.relative_to(ROOT).as_posix(),
                     "source_sha256": sha256_file(source_path),
                     "target_spec_sha256": canonical_sha256(config["target_specs"]),
                     "max_iterations": 5, "seed": 42, "execution_mode": "al_only",
                     "sample_hold_execution_id": executor.experiment_id})
    if len({job["sample_hold_execution_id"] for job in jobs}) != 2:
        raise ValueError("SampleHold Batch physical identities collide")
    for name in (
        "ActiveLearning/batch_runner_v2/sample_hold_production.py",
        "ActiveLearning/batch_runner_v2/runner.py",
        "ActiveLearning/batch_runner_v2/schema.py",
        "ActiveLearning/batch_runner_v2/storage.py",
        "ActiveLearning/inverse_design/llm_client.py",
    ):
        sources[name] = sha256_file(ROOT / name)
    manifest = {
        "schema_version": "sample_hold_batch_runtime_v1",
        "batch_id": spec["batch_id"], "provider": "openai", "model": model,
        "provider_base_url": base_url, "transport": transport,
        "credential_scope": spec["credential_scope"],
        "resource_limits": {"native_attempts_max_total": 10,
                            "per_circuit_native_attempts_max": 5,
                            "worker_timeout_seconds": 2400},
        "retry_policy": {"automatic_native_retries": 0},
        "jobs": jobs, "source_closure": sources,
        "repository_root": str(ROOT), "code_root": str(ROOT),
        "manifest_path": str(Path(path).resolve(strict=True)),
    }
    manifest["manifest_sha256"] = canonical_sha256({
        "manifest_file_sha256": sha256_file(Path(path)),
        "runtime": manifest, "preflight": preflights,
    })
    for job in jobs:
        verify_job(manifest, job)
    return manifest, preflights


def _child_result(manifest: dict, job: dict, attempt: Path) -> dict:
    """Run one AL job only under the scheduler's persisted RUNNING attempt."""
    batch_root = attempt.parents[3]
    intent_path = attempt / "child_spawn_intent.json"
    if (attempt != batch_root / "jobs" / job["job_id"] / "attempts" / attempt.name
            or not intent_path.is_file()):
        raise PermissionError("SampleHold child escaped its scheduled attempt")
    state = read_json(batch_root / "status.json")
    row = state["jobs"][job["job_id"]]
    if (read_json(intent_path) != {
            "manifest_sha256": manifest["manifest_sha256"],
            "job_id": job["job_id"], "attempt_id": attempt.name,
        } or state["manifest_sha256"] != manifest["manifest_sha256"]
            or row["state"] != "RUNNING"
            or row["attempts"][-1]["attempt_id"] != attempt.name):
        raise PermissionError("SampleHold child scheduler identity changed")
    context = {}

    def capture(value):
        context.update(value)
        atomic_json(attempt / "al_run_context.json", {
            "run_id": value["run_id"], "run_directory": str(value["run_directory"]),
        })

    result = al_main.run_configured_active_learning(
        "SampleHold", job["circuit_id"], config_path=ROOT / job["config_path"],
        runs_root=attempt / "al_runs", run_context_callback=capture,
        require_existing_d0=True, expected_config_sha256=job["config_sha256"],
        expected_d0_sha256=job["d0_sha256"],
    )
    run_dir = Path(context["run_directory"]).resolve(strict=True)
    if not run_dir.is_relative_to(attempt.resolve()) or run_dir.is_symlink():
        raise ValueError("SampleHold AL run escaped its Batch attempt")
    summary_path = run_dir / "summary.json"
    completed = read_json(summary_path)
    if (completed.get("iterations_completed") != result["iterations_completed"]
            or completed.get("stop_reason") != result["stop_reason"]
            or completed.get("llm_calls") != result["llm_calls"]
            or completed.get("total_tokens") != result["total_tokens"]):
        raise ValueError("SampleHold AL summary differs from returned result")
    physical = batch_root / job["sample_hold_execution_id"] / "physical_attempt_ledger.jsonl"
    outcome = {
        "state": "COMPLETED", "run_id": context["run_id"],
        "stop_reason": completed["stop_reason"],
        "iterations_completed": completed["iterations_completed"],
        "parameter_valid": completed["parameter_valid"],
        "simulation_valid": completed["simulation_valid"],
        "functional_valid": completed["functional_valid"],
        "meets_spec": completed["meets_spec"],
        "final_metrics": completed["final_metrics"],
        "d0_record_count": sum(1 for line in (ROOT / job["d0_path"]).read_text(
            encoding="utf-8").splitlines() if line.strip()),
        "result_path": str(summary_path), "al_result_sha256": sha256_file(summary_path),
        "llm_calls": completed["llm_calls"], "total_tokens": completed["total_tokens"],
        "total_timing": completed["total_timing"],
        "physical_ledger_path": str(physical),
    }
    atomic_json(attempt / "worker_result.json", outcome)
    return outcome


def _worker(manifest: dict, job: dict, attempt: Path) -> dict:
    intent = {"manifest_sha256": manifest["manifest_sha256"],
              "job_id": job["job_id"], "attempt_id": attempt.name}
    atomic_json(attempt / "child_spawn_intent.json", intent)
    command = [sys.executable, "-m", "ActiveLearning.batch_runner_v2.sample_hold_production",
               "child", "--manifest", manifest["manifest_path"],
               "--job-id", job["job_id"], "--attempt", str(attempt)]
    try:
        completed = run_isolated(command, cwd=ROOT, timeout=2400,
                                 environment=dict(os.environ))
    except ProcessGroupUncertain as error:
        raise UnverifiedOrphanError("SampleHold Batch child reaping is uncertain") from error
    except subprocess.TimeoutExpired:
        atomic_json(attempt / "child_reaped.json", {"attempt_id": attempt.name,
                   "exit_confirmed": True, "exit_code": 124})
        return {"state": "FAILED_TERMINAL", "error_code": "WORKER_TIMEOUT_REAPED"}
    atomic_json(attempt / "child_reaped.json", {"attempt_id": attempt.name,
               "exit_confirmed": True, "exit_code": completed.returncode})
    (attempt / "worker_stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (attempt / "worker_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        return {"state": "FAILED_TERMINAL",
                "error_code": f"CHILD_EXIT_{completed.returncode}"}
    outcome = read_json(attempt / "worker_result.json")
    result_path = Path(outcome["result_path"]).resolve(strict=True)
    if (outcome.get("state") != "COMPLETED"
            or not result_path.is_relative_to(attempt.resolve())
            or sha256_file(result_path) != outcome.get("al_result_sha256")):
        raise ValueError("SampleHold Batch child result provenance mismatch")
    return outcome


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MTV2_SH_001 Ubuntu Batch Runner V2 adapter")
    parser.add_argument("command", choices=("inspect", "preflight", "run", "report", "child"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--job-id")
    parser.add_argument("--attempt", type=Path)
    args = parser.parse_args(argv)
    if args.command == "inspect":
        result = inspect_manifest(args.manifest)
    elif args.command == "report":
        spec = inspect_manifest(args.manifest)
        result = report_batch(spec["batch_id"], spec["output_root"])
    elif args.command == "child":
        if not args.job_id or args.attempt is None:
            parser.error("child requires --job-id and --attempt")
        manifest, _ = prepare(args.manifest)
        job = next((j for j in manifest["jobs"] if j["job_id"] == args.job_id), None)
        if job is None:
            raise ValueError("SampleHold child job is not in Batch manifest")
        result = _child_result(manifest, job, args.attempt.resolve(strict=True))
    else:
        manifest, preflights = prepare(args.manifest)
        spec = inspect_manifest(args.manifest)
        if args.command == "preflight":
            result = {"batch_id": manifest["batch_id"], "status": "READY",
                      "manifest_sha256": manifest["manifest_sha256"],
                      "output_directory": str(Path(spec["output_root"]) / spec["batch_id"]),
                      "provider": manifest["provider"], "model": manifest["model"],
                      "provider_base_url": manifest["provider_base_url"],
                      "transport": manifest["transport"],
                      "jobs": {name: {"evidence_sha256": value["evidence_sha256"],
                                      "execution_contract_sha256": value["execution_contract_sha256"],
                                      "physical_attempts_max": value["budget"]["physical_attempts_max"],
                                      "execution_id": next(job["sample_hold_execution_id"]
                                                           for job in manifest["jobs"]
                                                           if job["job_id"] == name)}
                               for name, value in preflights.items()}}
        else:
            result = _run_batch(
                manifest, spec["output_root"],
                lambda job, attempt: _worker(manifest, job, attempt),
                offline_only=False, execution_scope="sample_hold_production",
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
