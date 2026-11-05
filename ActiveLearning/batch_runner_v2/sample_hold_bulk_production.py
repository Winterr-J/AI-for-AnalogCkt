"""Manifest-driven Ubuntu SampleHold preparation and Batch Runner V2 adapter.

Inspect/report are read-only. Preparation and run are explicit operator actions.
No physical failure is retried automatically, including interrupted preparation.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("MTV2_SH_002.json")
EXPECTED = {
    "MTV2_SH_002": ("0260", "0262", "0605", "0606", "0607", "0608", "0609",
                    "0610", "0612", "0614"),
    "MTV2_SH_003": ("0615", "0617", "0618", "0619", "0658"),
}
_MTV2_SH_002_RECOVERY_JOBS = frozenset({"sc0607", "sc0608", "sc0609"})
_RECOVERY_SOURCE_DRIFT = frozenset({
    "ActiveLearning/batch_runner_v2/runner.py",
    "ActiveLearning/batch_runner_v2/sample_hold_bulk_production.py",
})


def inspect_manifest(path: Path) -> dict:
    path = Path(path).resolve(strict=True)
    spec = json.loads(path.read_text(encoding="utf-8"))
    batch = spec.get("batch_id")
    if (batch not in EXPECTED
            or path != (ROOT / "ActiveLearning/batch_runner_v2" / f"{batch}.json").resolve()
            or set(spec) != {"schema_version", "batch_id", "output_root",
                             "preparation_root", "credential_scope",
                             "continue_on_verified_failure", "preparation", "jobs"}
            or spec["schema_version"] != "sample_hold_bulk_v1"
            or spec["output_root"] != "/home/ubuntu/sc_al_batches"
            or spec["preparation_root"] != f"/home/ubuntu/sc_al_preparations/{batch}"
            or spec["credential_scope"] != "bytecat_ubuntu"
            or spec["continue_on_verified_failure"] is not True
            or spec["preparation"] != {"seed": 42, "independent_d0_samples": 20,
                                       "sampling_mode": "v4", "profile_attempts_max": 40,
                                       "d0_attempts_max": 20}):
        raise ValueError("SampleHold bulk manifest identity/policy changed")
    expected = [{"job_id": "sc" + n, "circuit_id": "circuit_" + n,
                 "max_iterations": 5} for n in EXPECTED[batch]]
    if spec["jobs"] != expected:
        raise ValueError("SampleHold bulk job selection or five-round limit changed")
    for root in (spec["output_root"], spec["preparation_root"]):
        if not PurePosixPath(root).is_absolute():
            raise ValueError("SampleHold output root must be absolute")
    return spec


def _linux() -> None:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise RuntimeError("SampleHold bulk physical work requires Ubuntu/Linux")


def _sha(path: Path) -> str:
    from .storage import sha256_file
    return sha256_file(path)


def _bytecat_settings() -> tuple[str, str, str]:
    from ActiveLearning.inverse_design.llm_client import _provider_settings

    selected, _key, model, base_url = _provider_settings("openai", None, None, None)
    transport = os.getenv("AL_OPENAI_TRANSPORT", "responses").strip().lower()
    if (selected != "openai" or os.getenv("AL_LLM_PROVIDER", "openai").lower() != "openai"
            or transport not in {"chat_completions", "responses"}):
        raise ValueError("Ubuntu ByteCat OpenAI-compatible configuration missing")
    return model, base_url, transport


def _receipt(spec: dict, item: dict) -> dict:
    """Verify one immutable READY result, never promote a failed preparation."""
    from ActiveLearning.onboarding.sample_hold_bulk_core import derive_plan
    from .schema import canonical_sha256
    directory = Path(spec["preparation_root"]) / item["circuit_id"]
    receipt_path = directory / "result.json"
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    if saved.get("status") != "PREPARATION_READY":
        return saved
    if (saved.get("batch_id") != spec["batch_id"]
            or saved.get("circuit_id") != item["circuit_id"]
            or saved.get("manifest_sha256") != _sha(Path(saved["manifest_path"]))):
        raise ValueError("SampleHold preparation receipt identity changed")
    plan = derive_plan(item["circuit_id"])
    if (saved.get("source_sha256") != plan["source_sha256"]
            or saved.get("fixture_sha256") != plan["fixture_sha256"]
            or saved.get("stimulus_sha256") != plan["stimulus_sha256"]):
        raise ValueError("SampleHold preparation source/fixture changed")
    for name, digest in saved["artifacts"].items():
        path = (directory / name).resolve(strict=True)
        if not path.is_relative_to(directory.resolve()) or path.is_symlink() or _sha(path) != digest:
            raise ValueError("SampleHold preparation artifact changed: " + name)
    config_path = (directory / saved["config_path"]).resolve(strict=True)
    d0_path = (directory / saved["d0_path"]).resolve(strict=True)
    import yaml
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    profile_path = (directory / saved["profile_path"]).resolve(strict=True)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    target_generation = json.loads(config_path.with_name("target_generation.json").read_text(
        encoding="utf-8"))
    provenance = config.get("target_provenance") or {}
    attempt_counts = {}
    for stage, maximum in (("profile", 40), ("d0", 20)):
        identity_path = directory / f"physical_{stage}" / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        expected_code = {
            "executor_sha256": "ActiveLearning/onboarding/sample_hold_bulk_native.py",
            "child_sha256": "ActiveLearning/onboarding/sample_hold_bulk_child.py",
            "binding_sha256": "ActiveLearning/onboarding/sample_hold_bulk_core.py",
            "measurement_sha256": "ActiveLearning/sc_sample_hold_generic.py",
            "fixture_code_sha256": "ActiveLearning/sample_hold_offline.py",
        }
        if (identity.get("stage") != stage or identity.get("limit") != maximum
                or identity.get("circuit_id") != item["circuit_id"]
                or identity.get("source_sha256") != plan["source_sha256"]
                or identity.get("extra", {}).get("manifest_sha256") != saved["manifest_sha256"]
                or (stage == "d0" and identity.get("extra", {}).get("config_sha256")
                    != _sha(config_path))
                or any(identity.get(key) != _sha(ROOT / name)
                       for key, name in expected_code.items())):
            raise ValueError(f"SampleHold {stage} physical code/identity changed")
        ledger = directory / f"physical_{stage}" / "physical_attempt_ledger.jsonl"
        events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        count = sum(event.get("event") == "reserved" for event in events)
        if not 1 <= count <= maximum:
            raise ValueError(f"SampleHold {stage} physical attempt count invalid")
        attempt_counts[stage] = count
    rows = [json.loads(line) for line in d0_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if (len(rows) != 20 or len({row.get("sample_id") for row in rows}) != 20
            or sum(row.get("status", {}).get("functional_valid") is True for row in rows) < 1
            or profile.get("evaluation_backend") != "spice"
            or profile.get("circuit_id") != item["circuit_id"]
            or profile.get("anchor", {}).get("functional_valid") is not True
            or target_generation.get("status") != "TARGET_VALIDATED"
            or config.get("target_source") != "auto_generated"
            or provenance.get("profiling_backend") != "spice"
            or saved.get("physical_attempts") != attempt_counts
            or config.get("circuit_info") != {"circuit_type": "SampleHold",
                                               "circuit_id": item["circuit_id"]}
            or config.get("active_learning", {}).get("max_iterations") != 5
            or config["active_learning"].get("stop_on_meets_spec") is not False
            or config["active_learning"].get("prediction_prompt_version") != "compact_v1"
            or canonical_sha256(config["target_specs"]) != saved["target_spec_sha256"]):
        raise ValueError("SampleHold preparation D0, target or AL config invalid")
    return saved


def preparation(path: Path, *, allow_spice: bool, allow_llm: bool) -> dict:
    _linux()
    if not allow_spice or not allow_llm:
        raise PermissionError("real preparation requires --allow-spice and --allow-llm")
    _bytecat_settings()
    from ActiveLearning.auto_target import prepare_experiment
    from ActiveLearning.d0_initialization import initialize_configured_d0
    from ActiveLearning.onboarding import build_active_learning_circuit_config
    from ActiveLearning.onboarding.sample_hold_bulk_core import derive_plan
    from ActiveLearning.onboarding.sample_hold_bulk_native import BulkPhysicalExecutor
    from .schema import canonical_sha256
    from .storage import atomic_json
    import yaml

    spec = inspect_manifest(path)
    manifest_path = Path(path).resolve(strict=True)
    prep_root = Path(spec["preparation_root"])
    if (prep_root.is_relative_to(ROOT) or Path(spec["output_root"]).is_relative_to(ROOT)
            or Path(spec["output_root"]) / spec["batch_id"] == prep_root):
        raise ValueError("SampleHold output directories overlap checkout or each other")
    prep_root.mkdir(parents=True, exist_ok=True)
    results = {}
    for item in spec["jobs"]:
        circuit_id = item["circuit_id"]
        directory = prep_root / circuit_id
        receipt_path = directory / "result.json"
        if directory.exists():
            try:
                results[item["job_id"]] = _receipt(spec, item)
            except Exception as error:
                results[item["job_id"]] = {"circuit_id": circuit_id, "status": "BLOCKED",
                                            "reason": f"existing preparation incomplete/changed: {error}"}
            continue
        directory.mkdir()
        try:
            runtime = build_active_learning_circuit_config("SampleHold", circuit_id)
            plan = derive_plan(circuit_id)
            common = {"manifest_sha256": _sha(manifest_path)}
            runtime.testbench_config["_bulk_executor"] = BulkPhysicalExecutor(
                runtime, directory / "physical_profile", stage="profile", limit=40,
                extra_identity=common)
            target = prepare_experiment(
                "SampleHold", circuit_id, runtime=runtime, allow_spice=True,
                allow_llm=True, provider="openai", model=None,
                output_root=directory / "auto_target", profile_samples=12,
                max_profile_evaluations=20, additional_samples=0,
                max_anchor_evaluations=12, max_llm_calls=3,
                seed=42, d0_mode="independent",
            )
            if target["status"] != "READY_FOR_D0":
                raise RuntimeError(f"AutoTarget {target['status']}: {target.get('reason')}")
            profile_attempts = runtime.testbench_config["_bulk_executor"].execution_statistics()[
                "physical_attempts"]
            config_path = Path(target["artifact_paths"]["config"]).resolve(strict=True)
            if not config_path.is_relative_to(directory.resolve()):
                raise ValueError("generated AL config escaped preparation output")
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["circuit_info"] = {"circuit_type": "SampleHold", "circuit_id": circuit_id}
            config.setdefault("active_learning", {}).update({
                "max_iterations": 5, "stop_on_meets_spec": False,
                "prediction_prompt_version": "compact_v1",
            })
            data = config["data"]
            data.update({"d0_mode": "independent", "d0_path_relative_to": "config",
                         "d0_path": "d0/d0_v4_seed42.jsonl", "initialize_if_missing": True,
                         "sampling_mode": "v4", "seed": 42, "n_samples": 20,
                         "n_local": 10, "n_global": 9, "local_radius": 0.05})
            data.pop("local_factor", None)
            config["sample_hold_bulk"] = {
                "batch_id": spec["batch_id"], "manifest_path": str(manifest_path),
                "receipt_path": str(receipt_path),
                "al_native_root": str(Path(spec["output_root"]) / spec["batch_id"]
                                      / "native" / item["job_id"]),
                "fixture_sha256": plan["fixture_sha256"],
                "stimulus_sha256": plan["stimulus_sha256"],
            }
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            runtime.testbench_config["_bulk_executor"] = BulkPhysicalExecutor(
                runtime, directory / "physical_d0", stage="d0", limit=20,
                extra_identity={**common, "config_sha256": _sha(config_path)})
            d0_path = config_path.parent / data["d0_path"]
            d0 = initialize_configured_d0(runtime, config, config["target_specs"], d0_path)
            records = d0["records"]
            valid = sum(row.get("status", {}).get("functional_valid") is True for row in records)
            if len(records) != 20 or valid < 1:
                raise ValueError("independent D0 has no verified functional-valid physical point")
            artifacts = [config_path, d0_path, d0_path.with_suffix(".csv"),
                         d0_path.with_name(d0_path.stem + "_initialization.json")]
            for stage in ("profile", "d0"):
                artifacts.extend((directory / f"physical_{stage}" / name)
                                 for name in ("identity.json", "physical_attempt_ledger.jsonl"))
            profile_path = Path(target["artifact_paths"]["profiling"]).resolve(strict=True)
            artifacts.extend(Path(target["artifact_paths"][name]).resolve(strict=True)
                             for name in ("anchor", "profiling", "target_generation", "result"))
            if any(not file.is_file() for file in artifacts):
                raise ValueError("independent D0 artifacts incomplete")
            if any(not file.is_relative_to(directory.resolve()) for file in artifacts):
                raise ValueError("preparation artifact escaped output directory")
            saved = {"status": "PREPARATION_READY", "batch_id": spec["batch_id"],
                     "circuit_id": circuit_id, "manifest_path": str(manifest_path),
                     "manifest_sha256": _sha(manifest_path),
                     "source_sha256": plan["source_sha256"],
                     "fixture_sha256": plan["fixture_sha256"],
                     "stimulus_sha256": plan["stimulus_sha256"],
                     "target_spec_sha256": canonical_sha256(config["target_specs"]),
                     "config_path": config_path.relative_to(directory).as_posix(),
                     "d0_path": d0_path.relative_to(directory).as_posix(),
                     "profile_path": profile_path.relative_to(directory).as_posix(),
                     "d0_records": len(records), "functional_valid_count": valid,
                     "physical_attempts": {
                         "profile": profile_attempts,
                         "d0": runtime.testbench_config["_bulk_executor"].execution_statistics()["physical_attempts"],
                     },
                     "artifacts": {file.relative_to(directory).as_posix(): _sha(file)
                                   for file in artifacts}}
            atomic_json(receipt_path, saved)
            results[item["job_id"]] = saved
        except Exception as error:
            saved = {"status": "BLOCKED", "batch_id": spec["batch_id"],
                     "circuit_id": circuit_id, "reason": f"{type(error).__name__}: {error}"}
            atomic_json(receipt_path, saved)
            results[item["job_id"]] = saved
    summary = {"batch_id": spec["batch_id"], "status": "PREPARATION_COMPLETE",
               "ready": sum(row["status"] == "PREPARATION_READY" for row in results.values()),
               "blocked": sum(row["status"] == "BLOCKED" for row in results.values()),
               "circuits": results}
    atomic_json(prep_root / "summary.json", summary)
    return summary


def preflight(path: Path) -> tuple[dict, dict]:
    _linux()
    from ActiveLearning.onboarding import build_active_learning_circuit_config
    from ActiveLearning.onboarding.sample_hold_bulk_native import BulkPhysicalExecutor
    from ActiveLearning.onboarding.sample_hold_bulk_core import derive_plan
    from .schema import canonical_sha256, configuration_identity, verify_job

    spec = inspect_manifest(path)
    model, base_url, transport = _bytecat_settings()
    jobs, blocked = [], {}
    for item in spec["jobs"]:
        try:
            saved = _receipt(spec, item)
            if saved["status"] != "PREPARATION_READY":
                blocked[item["job_id"]] = saved.get("reason", "Preparation BLOCKED")
                continue
            directory = Path(spec["preparation_root"]) / item["circuit_id"]
            config_path = directory / saved["config_path"]
            d0_path = directory / saved["d0_path"]
            import yaml
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            runtime = build_active_learning_circuit_config("SampleHold", item["circuit_id"])
            plan = derive_plan(item["circuit_id"])
            executor = BulkPhysicalExecutor(
                runtime, Path(config["sample_hold_bulk"]["al_native_root"]),
                stage="al", limit=5,
                extra_identity={"batch_id": spec["batch_id"],
                                "config_sha256": _sha(config_path),
                                "d0_sha256": _sha(d0_path)})
            executor.verify_existing()
            jobs.append({"job_id": item["job_id"], "circuit_type": "SampleHold",
                         "circuit_id": item["circuit_id"],
                         "configuration_id": configuration_identity(config, _sha(config_path)),
                         "config_path": str(config_path), "config_sha256": _sha(config_path),
                         "d0_path": str(d0_path), "d0_sha256": _sha(d0_path),
                         "source_path": f"dataset/{item['circuit_id']}/topology/{item['circuit_id']}.py",
                         "source_sha256": plan["source_sha256"][
                             f"dataset/{item['circuit_id']}/topology/{item['circuit_id']}.py"],
                         "target_spec_sha256": saved["target_spec_sha256"],
                         "max_iterations": 5, "seed": 42, "execution_mode": "al_only",
                         "sample_hold_execution_id": f"native/{item['job_id']}"})
        except Exception as error:
            blocked[item["job_id"]] = f"{type(error).__name__}: {error}"
    if not jobs:
        return {"batch_id": spec["batch_id"], "status": "NO_READY_JOBS"}, blocked
    sources = {name: _sha(ROOT / name) for name in (
        "ActiveLearning/onboarding/sample_hold_bulk_core.py",
        "ActiveLearning/onboarding/sample_hold_bulk_native.py",
        "ActiveLearning/onboarding/sample_hold_bulk_child.py",
        "ActiveLearning/onboarding/sample_hold_bulk_fixtures.json",
        "ActiveLearning/batch_runner_v2/sample_hold_bulk_production.py",
        "ActiveLearning/batch_runner_v2/runner.py",
        "ActiveLearning/batch_runner_v2/schema.py",
        "ActiveLearning/batch_runner_v2/sample_hold_production.py",
        "ActiveLearning/main.py", "ActiveLearning/auto_target.py",
        "ActiveLearning/d0_initialization.py",
        "ActiveLearning/InitializeD0/d0_builder.py",
        "ActiveLearning/sc_sample_hold_generic.py",
        "ActiveLearning/sample_hold_offline.py",
        "ActiveLearning/sample_hold_posix.py",
        "ActiveLearning/inverse_design/llm_client.py",
        "ActiveLearning/onboarding/sample_hold.py",
        "ActiveLearning/onboarding/runtime_provider.py",
    )}
    manifest = {"schema_version": "sample_hold_bulk_runtime_v1",
                "batch_id": spec["batch_id"], "provider": "openai", "model": model,
                "provider_base_url": base_url, "transport": transport,
                "credential_scope": spec["credential_scope"],
                "resource_limits": {"native_attempts_max_total": len(jobs) * 5,
                                    "per_circuit_native_attempts_max": 5,
                                    "worker_timeout_seconds": 2400},
                "retry_policy": {"automatic_native_retries": 0},
                "jobs": jobs, "source_closure": sources,
                "repository_root": str(ROOT), "preparation_root": spec["preparation_root"],
                "manifest_path": str(Path(path).resolve(strict=True))}
    manifest["manifest_sha256"] = canonical_sha256({
        "manifest_file_sha256": _sha(Path(path)), "runtime": manifest})
    for job in jobs:
        verify_job(manifest, job)
    return manifest, blocked


def _resume_manifest_for_abandoned_orphan(
    manifest: dict, output_root: str | Path,
    abandoned_orphan_job_ids: frozenset[str],
) -> dict:
    """Bind the reviewed MTV2_SH_002 recovery to its existing batch identity."""
    from .schema import canonical_sha256
    from .storage import read_json

    if manifest.get("batch_id") != "MTV2_SH_002" or abandoned_orphan_job_ids != {"sc0605"}:
        raise ValueError("SampleHold bulk recovery permits only MTV2_SH_002/sc0605")
    directory = Path(output_root) / "MTV2_SH_002"
    persisted = read_json(directory / "manifest.json")
    state = read_json(directory / "status.json")
    if (persisted.get("manifest_sha256") != state.get("manifest_sha256")
            or persisted.get("batch_id") != "MTV2_SH_002"
            or set(state.get("jobs", {})) != {job["job_id"] for job in manifest["jobs"]}
            or state["jobs"].get("sc0262", {}).get("state") != "COMPLETED"
            or state["jobs"].get("sc0605", {}).get("state") != "INTERRUPTED_UNCERTAIN"
            or state["jobs"]["sc0605"].get("error_code") != "ORPHAN_UNVERIFIED"
            or state.get("orphan_unverified") is not True):
        raise ValueError("MTV2_SH_002 persisted recovery state changed")
    pending = {job_id for job_id, row in state["jobs"].items()
               if row.get("state") == "PENDING"}
    if not pending.issubset(_MTV2_SH_002_RECOVERY_JOBS):
        raise ValueError(f"MTV2_SH_002 has unexpected PENDING jobs: {sorted(pending)}")

    old_sources = persisted.get("source_closure")
    new_sources = manifest.get("source_closure")
    if not isinstance(old_sources, dict) or not isinstance(new_sources, dict):
        raise ValueError("MTV2_SH_002 source closure missing")
    changed_sources = {
        name for name in set(old_sources) | set(new_sources)
        if old_sources.get(name) != new_sources.get(name)
    }
    if changed_sources != _RECOVERY_SOURCE_DRIFT:
        raise ValueError(
            "MTV2_SH_002 recovery source drift differs from the reviewed patch: "
            f"{sorted(changed_sources)}")

    def stable_payload(value: dict) -> dict:
        payload = copy.deepcopy(value)
        payload.pop("manifest_sha256", None)
        payload.pop("source_closure", None)
        payload.pop("_resume_manifest_sha256", None)
        return payload

    if canonical_sha256(stable_payload(persisted)) != canonical_sha256(
            stable_payload(manifest)):
        raise ValueError("MTV2_SH_002 jobs, assets, policy, or runtime identity changed")
    resumed = copy.deepcopy(manifest)
    resumed["manifest_sha256"] = persisted["manifest_sha256"]
    resumed["_resume_manifest_sha256"] = persisted["manifest_sha256"]
    return resumed


def _resume_child_manifest_identity(
    manifest: dict, attempt: Path, expected_manifest_sha256: str,
) -> dict:
    """Restore the reviewed parent identity; _child_result rechecks it again."""
    from .storage import read_json

    if (not isinstance(expected_manifest_sha256, str)
            or len(expected_manifest_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in expected_manifest_sha256)):
        raise ValueError("invalid resume manifest SHA-256")
    intent = read_json(attempt / "child_spawn_intent.json")
    state = read_json(attempt.parents[3] / "status.json")
    if (intent.get("manifest_sha256") != expected_manifest_sha256
            or state.get("manifest_sha256") != expected_manifest_sha256):
        raise PermissionError("SampleHold recovery child identity changed")
    resumed = copy.deepcopy(manifest)
    resumed["manifest_sha256"] = expected_manifest_sha256
    return resumed


def _worker(manifest: dict, job: dict, attempt: Path) -> dict:
    from ActiveLearning.sample_hold_posix import ProcessGroupUncertain, run_isolated
    from .runner import UnverifiedOrphanError
    from .storage import atomic_json, read_json, sha256_file

    atomic_json(attempt / "child_spawn_intent.json", {
        "manifest_sha256": manifest["manifest_sha256"],
        "job_id": job["job_id"], "attempt_id": attempt.name})
    command = [sys.executable, "-m", "ActiveLearning.batch_runner_v2.sample_hold_bulk_production",
               "child", "--manifest", manifest["manifest_path"],
               "--job-id", job["job_id"], "--attempt", str(attempt)]
    if manifest.get("_resume_manifest_sha256"):
        command.extend(["--resume-manifest-sha256", manifest["_resume_manifest_sha256"]])
    try:
        completed = run_isolated(command, cwd=ROOT, timeout=2400,
                                 environment=dict(os.environ))
    except ProcessGroupUncertain as error:
        raise UnverifiedOrphanError("SampleHold Batch child reaping uncertain") from error
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
    result = read_json(attempt / "worker_result.json")
    path = Path(result["result_path"]).resolve(strict=True)
    if (result.get("state") != "COMPLETED" or not path.is_relative_to(attempt.resolve())
            or sha256_file(path) != result.get("al_result_sha256")):
        raise ValueError("SampleHold Batch child result provenance mismatch")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SampleHold bulk Preparation + V2 Batch")
    parser.add_argument("command", choices=("inspect", "preparation", "preflight", "run",
                                             "status", "report", "child"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--allow-spice", action="store_true")
    parser.add_argument("--allow-llm", action="store_true")
    parser.add_argument("--job-id")
    parser.add_argument("--attempt", type=Path)
    parser.add_argument("--abandoned-orphan-job-id", action="append", default=[])
    parser.add_argument("--resume-manifest-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.abandoned_orphan_job_id and args.command != "run":
        parser.error("--abandoned-orphan-job-id is valid only for run")
    if args.resume_manifest_sha256 and args.command != "child":
        parser.error("--resume-manifest-sha256 is internal to the child command")
    if args.command == "inspect":
        result = inspect_manifest(args.manifest)
    elif args.command == "preparation":
        result = preparation(args.manifest, allow_spice=args.allow_spice,
                             allow_llm=args.allow_llm)
    elif args.command in {"status", "report"}:
        from .runner import report_batch, status_batch
        spec = inspect_manifest(args.manifest)
        prep = Path(spec["preparation_root"]) / "summary.json"
        result = {"batch_id": spec["batch_id"],
                  "preparation": json.loads(prep.read_text(encoding="utf-8"))
                  if prep.is_file() else None}
        batch_dir = Path(spec["output_root"]) / spec["batch_id"]
        if (batch_dir / "status.json").is_file():
            result["batch"] = (status_batch(spec["batch_id"], spec["output_root"])
                               if args.command == "status" else
                               report_batch(spec["batch_id"], spec["output_root"]))
    else:
        manifest, blocked = preflight(args.manifest)
        if args.command == "preflight":
            result = {"batch_id": manifest["batch_id"], "status": manifest["status"]
                      if "status" in manifest else "READY",
                      "ready_jobs": [job["job_id"] for job in manifest.get("jobs", [])],
                      "blocked_jobs": blocked,
                      "manifest_sha256": manifest.get("manifest_sha256")}
        elif manifest.get("status") == "NO_READY_JOBS":
            raise RuntimeError("no PREPARATION_READY circuit; Batch will not start")
        elif args.command == "child":
            if not args.job_id or args.attempt is None:
                parser.error("child requires --job-id and --attempt")
            from .sample_hold_production import _child_result
            job = next((item for item in manifest["jobs"]
                        if item["job_id"] == args.job_id), None)
            if job is None:
                raise ValueError("SampleHold child job not in ready Batch set")
            if args.resume_manifest_sha256:
                manifest = _resume_child_manifest_identity(
                    manifest, args.attempt.resolve(strict=True),
                    args.resume_manifest_sha256)
            result = _child_result(manifest, job, args.attempt.resolve(strict=True))
        else:
            from .runner import _run_batch
            output_root = inspect_manifest(args.manifest)["output_root"]
            abandoned_orphans = frozenset(args.abandoned_orphan_job_id)
            if abandoned_orphans:
                manifest = _resume_manifest_for_abandoned_orphan(
                    manifest, output_root, abandoned_orphans)
            result = _run_batch(manifest, output_root,
                                lambda job, attempt: _worker(manifest, job, attempt),
                                offline_only=False, execution_scope="sample_hold_production",
                                abandoned_orphan_job_ids=abandoned_orphans)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
