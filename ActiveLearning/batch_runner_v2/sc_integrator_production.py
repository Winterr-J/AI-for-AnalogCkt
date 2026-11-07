"""Operator-driven SCIntegrator Preparation, preflight and V2 Batch adapter.

The 82 archived manifests remain candidate lists. This module derives a
separate runtime manifest only from immutable PREPARATION_READY receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "ActiveLearning/candidate_manifests/sc_integrator_v2/batches"
MASTER = MANIFESTS.parent / "master_manifest.json"
OUTPUT_ROOT = Path("/home/ubuntu/sc_integrator_batches")
PREP_ROOT = Path("/home/ubuntu/sc_integrator_preparations")
_AUTOCONFIG_ERROR = "AutoConfigError: Unsupported AutoConfig circuit type: SCIntegrator"
_DEPLOYMENT_RESOURCES = (
    "ActiveLearning/onboarding/sc_integrator_bulk_fixtures.json",
    "ActiveLearning/circuit_types/SCIntegrator/performance_template.yaml",
    "ActiveLearning/inverse_design/prompts/circuit_types/sc_integrator_bulk_v1.txt",
    "ActiveLearning/performance_prediction/prompts/circuit_types/sc_integrator_bulk_v1.txt",
)


def _sha(path: Path) -> str:
    from .storage import sha256_file
    return sha256_file(path)


def inspect_manifest(batch_id: str) -> dict:
    if not batch_id.startswith("MTV2_SCI_PREP_") or not batch_id[-3:].isdigit():
        raise ValueError("invalid SCIntegrator candidate Batch ID")
    number = int(batch_id[-3:])
    if not 1 <= number <= 82:
        raise ValueError("SCIntegrator candidate Batch ID outside 001..082")
    path = MANIFESTS / f"{batch_id}.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    master = json.loads(MASTER.read_text(encoding="utf-8"))
    ids = spec.get("circuit_ids")
    all_ids = [item["circuit_id"] for item in master["candidates"]]
    offset = (number - 1) * 10
    expected = all_ids[offset:offset + 10]
    if (spec.get("schema_version") != "sc_integrator_preparation_candidate_batch_v1"
            or spec.get("batch_id") != batch_id
            or spec.get("circuit_type") != "SCIntegrator"
            or spec.get("status") != "CANDIDATE_IDS_ONLY_NOT_PREPARATION_READY"
            or spec.get("master_sha256") != _sha(MASTER)
            or ids != expected or len(ids) != len(set(ids))
            or [row.get("circuit_id") for row in spec.get("candidates", [])] != ids
            or master.get("candidate_count") != 811):
        raise ValueError("SCIntegrator candidate/master Manifest identity changed")
    return {"batch_id": batch_id, "circuit_ids": ids,
            "candidate_manifest": str(path.resolve()),
            "candidate_manifest_sha256": _sha(path),
            "master_sha256": _sha(MASTER),
            "preparation_root": str(PREP_ROOT / batch_id),
            "output_root": str(OUTPUT_ROOT)}


def _linux() -> None:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise RuntimeError("SCIntegrator physical work requires Ubuntu/Linux")


def _receipt(spec: dict, circuit_id: str) -> dict:
    from ActiveLearning.onboarding.sc_integrator_bulk_core import derive_plan
    from .schema import canonical_sha256
    directory = Path(spec["preparation_root"]) / circuit_id
    saved = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if saved.get("status") != "PREPARATION_READY":
        return saved
    if (saved.get("batch_id") != spec["batch_id"] or saved.get("circuit_id") != circuit_id
            or saved.get("candidate_manifest_sha256") != spec["candidate_manifest_sha256"]):
        raise ValueError("SCIntegrator READY receipt identity changed")
    plan = derive_plan(circuit_id)
    if (saved.get("source_sha256") != plan["source_sha256"]
            or saved.get("fixture_sha256") != plan["fixture_sha256"]
            or saved.get("stimulus_sha256") != plan["stimulus_sha256"]):
        raise ValueError("SCIntegrator source/fixture/stimulus changed")
    for name, digest in saved["artifacts"].items():
        path = (directory / name).resolve(strict=True)
        if not path.is_relative_to(directory.resolve()) or path.is_symlink() or _sha(path) != digest:
            raise ValueError("SCIntegrator preparation artifact changed: " + name)
    config_path = (directory / saved["config_path"]).resolve(strict=True)
    d0_path = (directory / saved["d0_path"]).resolve(strict=True)
    import yaml
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    profile = json.loads((directory / saved["profile_path"]).read_text(encoding="utf-8"))
    target = json.loads(config_path.with_name("target_generation.json").read_text(encoding="utf-8"))
    if (config.get("circuit_info") != {"circuit_type": "SCIntegrator", "circuit_id": circuit_id}
            or config.get("target_source") != "auto_generated"
            or config.get("target_provenance", {}).get("profiling_backend") != "spice"
            or profile.get("evaluation_backend") != "spice"
            or profile.get("anchor", {}).get("functional_valid") is not True
            or target.get("status") != "TARGET_VALIDATED"
            or config.get("active_learning", {}).get("max_iterations") != 5
            or type(config["active_learning"].get("stop_on_meets_spec")) is not bool
            or config.get("data", {}).get("d0_mode") != "independent"
            or config["data"].get("seed") != 42 or config["data"].get("n_samples") != 20
            or canonical_sha256(config.get("target_specs")) != saved["target_spec_sha256"]):
        raise ValueError("SCIntegrator READY target/config/profile invalid")
    rows = [json.loads(line) for line in d0_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if (len(rows) != 20 or len({row.get("sample_id") for row in rows}) != 20
            or not any(row.get("status", {}).get("functional_valid") is True for row in rows)):
        raise ValueError("SCIntegrator READY independent D0 invalid")
    for stage, maximum in (("profile", 40), ("d0", 20)):
        native = directory / f"physical_{stage}"
        identity = json.loads((native / "identity.json").read_text(encoding="utf-8"))
        if (identity.get("schema") != "sc_integrator_bulk_native_v1"
                or identity.get("circuit_id") != circuit_id or identity.get("stage") != stage
                or identity.get("limit") != maximum
                or identity.get("source_sha256") != plan["source_sha256"]
                or identity.get("fixture_sha256") != plan["fixture_sha256"]
                or identity.get("stimulus_sha256") != plan["stimulus_sha256"]
                or identity.get("extra", {}).get("candidate_manifest_sha256")
                != spec["candidate_manifest_sha256"]
                or (stage == "d0" and identity.get("extra", {}).get("config_sha256")
                    != _sha(config_path))):
            raise ValueError(f"SCIntegrator {stage} physical identity changed")
        code = {"executor_sha256": "ActiveLearning/onboarding/sc_integrator_bulk_native.py",
                "ledger_helpers_sha256": "ActiveLearning/onboarding/sample_hold_bulk_native.py",
                "model_binding_sha256": "ActiveLearning/onboarding/sample_hold_bulk_core.py",
                "parameter_builder_sha256": "ActiveLearning/onboarding/sample_hold.py",
                "posix_sha256": "ActiveLearning/sample_hold_posix.py",
                "ngspice_config_sha256": "run/single_stage_amplifier/run_ideal_dataset_simulations.py",
                "child_sha256": "ActiveLearning/onboarding/sc_integrator_bulk_child.py",
                "binding_sha256": "ActiveLearning/onboarding/sc_integrator_bulk_core.py",
                "measurement_sha256": "ActiveLearning/onboarding/sc_integrator_bulk_core.py"}
        if any(identity.get(key) != _sha(ROOT / path) for key, path in code.items()):
            raise ValueError(f"SCIntegrator {stage} physical source changed")
        events = [json.loads(line) for line in (native / "physical_attempt_ledger.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
        count = sum(row.get("event") == "reserved" for row in events)
        if not 1 <= count <= maximum or saved["physical_attempts"][stage] != count:
            raise ValueError(f"SCIntegrator {stage} physical budget invalid")
    return saved


def _preserve_autoconfig_blocked_receipt(spec: dict, circuit_id: str) -> None:
    """Preserve one pre-physical AutoConfig or repaired deployment failure."""
    directory = Path(spec["preparation_root"]) / circuit_id
    receipt = directory / "result.json"
    archive = directory / "result.blocked_autoconfig_original.json"
    if (directory.is_symlink() or not receipt.is_file() or receipt.is_symlink()
            or (OUTPUT_ROOT / spec["batch_id"] / "native" / circuit_id).exists()
            or (OUTPUT_ROOT / spec["batch_id"] / "jobs" / circuit_id).exists()):
        raise ValueError("SCIntegrator retry has an unsafe directory or physical history")
    deployment_archive = directory / "result.blocked_missing_resource_original.json"
    claim = directory / "retry_missing_resource_claim.json"
    allowed = {"result.json", archive.name, deployment_archive.name, claim.name}
    if {path.name for path in directory.iterdir()} - allowed:
        raise ValueError("SCIntegrator retry has existing Preparation artifacts")
    original = receipt.read_bytes()
    saved = json.loads(original)
    expected = {"status": "BLOCKED", "batch_id": spec["batch_id"],
                "circuit_id": circuit_id, "reason": _AUTOCONFIG_ERROR}
    if saved == expected:
        if deployment_archive.exists() or claim.exists():
            raise ValueError("SCIntegrator deployment retry history conflicts with AutoConfig")
        _archive_exact(archive, original)
        return
    if (set(saved) != {"status", "batch_id", "circuit_id", "reason"}
            or any(saved.get(key) != expected[key] for key in ("status", "batch_id", "circuit_id"))
            or not _deployment_missing_path(saved.get("reason"))):
        raise ValueError("SCIntegrator retry is limited to the original AutoConfig failure or a repaired deployment resource")
    if claim.exists() or claim.is_symlink():
        raise ValueError("SCIntegrator deployment resource retry was already claimed")
    if not archive.is_file() or archive.is_symlink() or json.loads(archive.read_bytes()) != expected:
        raise ValueError("SCIntegrator original AutoConfig receipt is missing or changed")
    source_hashes = _verify_historical_sources(circuit_id)
    deployment_hashes = _verify_deployment_resources()
    _archive_exact(deployment_archive, original)
    claim_data = {"batch_id": spec["batch_id"], "circuit_id": circuit_id,
                  "candidate_manifest_sha256": spec["candidate_manifest_sha256"],
                  "master_sha256": spec["master_sha256"],
                  "source_sha256": source_hashes,
                  "deployment_resource_sha256": deployment_hashes,
                  "previous_receipt_sha256": _sha(deployment_archive)}
    with claim.open("xb") as stream:
        stream.write((json.dumps(claim_data, sort_keys=True) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def _archive_exact(path: Path, original: bytes) -> None:
    if path.exists() or path.is_symlink():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != original:
            raise ValueError("SCIntegrator original BLOCKED receipt archive changed")
        return
    with path.open("xb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())


def _deployment_missing_path(reason: object) -> str | None:
    if not isinstance(reason, str) or not reason.startswith("FileNotFoundError: "):
        return None
    detail = reason.removeprefix("FileNotFoundError: ")
    for relative in _DEPLOYMENT_RESOURCES:
        if detail in (relative, f"[Errno 2] No such file or directory: '{ROOT / relative}'"):
            return relative
    return None


def _verify_historical_sources(circuit_id: str) -> dict[str, str]:
    master = json.loads(MASTER.read_text(encoding="utf-8"))
    rows = [row for row in master["candidates"] if row.get("circuit_id") == circuit_id]
    if len(rows) != 1 or not rows[0].get("resources"):
        raise ValueError("SCIntegrator historical source identity missing")
    hashes = {}
    for relative, record in rows[0]["resources"].items():
        path = ROOT / relative
        if (record.get("status") != "COPIED_AND_VERIFIED" or not path.is_file()
                or path.is_symlink()):
            raise ValueError("SCIntegrator historical source changed: " + relative)
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if not _matches_archived_text(data, record.get("sha256")):
            raise ValueError("SCIntegrator historical source changed: " + relative)
        hashes[relative] = actual
    return hashes


def _matches_archived_text(data: bytes, archived_sha256: object) -> bool:
    # The archive hashed Windows CRLF bytes; Git stores these text files as LF.
    crlf = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return archived_sha256 in {hashlib.sha256(data).hexdigest(),
                               hashlib.sha256(crlf).hexdigest()}


def _verify_deployment_resources() -> dict[str, str]:
    hashes = {}
    for relative in _DEPLOYMENT_RESOURCES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise ValueError("SCIntegrator deployment resource still missing: " + relative)
        hashes[relative] = _sha(path)
    fixtures = json.loads((ROOT / _DEPLOYMENT_RESOURCES[0]).read_text(encoding="utf-8"))
    if (fixtures.get("schema_version") != "sc_integrator_bulk_fixture_overrides_v1"
            or not isinstance(fixtures.get("circuits"), dict)):
        raise ValueError("SCIntegrator deployment fixture invalid")
    import yaml
    template = yaml.safe_load((ROOT / _DEPLOYMENT_RESOURCES[1]).read_text(encoding="utf-8"))
    if (template.get("schema_version") != "performance_template_v1"
            or template.get("performance_template", {}).get("circuit_type") != "sc_integrator"):
        raise ValueError("SCIntegrator deployment performance template invalid")
    return hashes


def _preserve_autoconfig_blocked_summary(spec: dict) -> None:
    root = Path(spec["preparation_root"])
    summary = root / "summary.json"
    archive = root / "summary.blocked_autoconfig_original.json"
    if not summary.exists():
        return
    if summary.is_symlink() or not summary.is_file():
        raise ValueError("SCIntegrator Preparation summary is unsafe")
    original = summary.read_bytes()
    if json.loads(original).get("batch_id") != spec["batch_id"]:
        raise ValueError("SCIntegrator Preparation summary identity changed")
    if not archive.exists():
        with archive.open("xb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        if (archive.is_symlink() or not archive.is_file()
                or json.loads(archive.read_bytes()).get("batch_id") != spec["batch_id"]):
            raise ValueError("SCIntegrator original BLOCKED summary archive changed")
        if archive.read_bytes() != original:
            _archive_exact(root / "summary.blocked_missing_resource_original.json", original)


def preparation(batch_id: str, *, allow_spice: bool, allow_llm: bool,
                retry_autoconfig_blocked: bool = False,
                prediction_prompt_version: str = "v1") -> dict:
    _linux()
    if prediction_prompt_version not in {"v1", "compact_v2"}:
        raise ValueError("unsupported SCIntegrator prediction prompt version")
    if prediction_prompt_version == "compact_v2":
        for relative in (
            "ActiveLearning/performance_prediction/sc_integrator_compact_v2.py",
            "ActiveLearning/performance_prediction/prompts/circuit_types/sc_integrator_compact_v2.txt",
            "ActiveLearning/performance_prediction/sample_hold_compact.py",
        ):
            if not (ROOT / relative).is_file():
                raise FileNotFoundError(relative)
    if not allow_spice or not allow_llm:
        raise PermissionError("physical Preparation requires --allow-spice --allow-llm")
    from .sample_hold_bulk_production import _bytecat_settings
    _bytecat_settings()
    from ActiveLearning.auto_target import prepare_experiment
    from ActiveLearning.d0_initialization import initialize_configured_d0
    from ActiveLearning.onboarding import build_active_learning_circuit_config
    from ActiveLearning.onboarding.sc_integrator_bulk_native import SCIntegratorPhysicalExecutor
    from ActiveLearning.onboarding.sc_integrator_bulk_core import derive_plan
    from .schema import canonical_sha256
    from .storage import atomic_json
    import yaml

    spec = inspect_manifest(batch_id)
    directory_root = Path(spec["preparation_root"])
    for circuit_id in spec["circuit_ids"]:
        receipt_path = directory_root / circuit_id / "result.json"
        if not receipt_path.is_file():
            continue
        receipt = _receipt(spec, circuit_id)
        if receipt.get("status") == "PREPARATION_READY":
            existing_config = yaml.safe_load((directory_root / circuit_id /
                receipt["config_path"]).read_text(encoding="utf-8"))
            if (existing_config.get("active_learning", {}).get(
                    "prediction_prompt_version") != prediction_prompt_version):
                raise ValueError("existing Preparation uses another prediction prompt version")
    if (directory_root.is_relative_to(ROOT) or OUTPUT_ROOT.is_relative_to(ROOT)
            or directory_root.is_relative_to(OUTPUT_ROOT)):
        raise ValueError("SCIntegrator output roots overlap repository or each other")
    directory_root.mkdir(parents=True, exist_ok=True)
    if retry_autoconfig_blocked:
        _preserve_autoconfig_blocked_summary(spec)
    results = {}
    for circuit_id in spec["circuit_ids"]:
        directory = directory_root / circuit_id
        receipt_path = directory / "result.json"
        if directory.exists() and not retry_autoconfig_blocked:
            try:
                results[circuit_id] = _receipt(spec, circuit_id)
            except Exception as error:
                results[circuit_id] = {"status": "BLOCKED", "circuit_id": circuit_id,
                                       "reason": f"existing Preparation incomplete/changed: {error}"}
            continue
        if directory.exists():
            try:
                existing = _receipt(spec, circuit_id)
                if existing.get("status") == "PREPARATION_READY":
                    results[circuit_id] = existing
                    continue
                _preserve_autoconfig_blocked_receipt(spec, circuit_id)
            except Exception as error:
                results[circuit_id] = {"status": "BLOCKED", "circuit_id": circuit_id,
                                       "reason": f"existing Preparation incomplete/changed: {error}"}
                continue
        else:
            directory.mkdir()
        try:
            runtime = build_active_learning_circuit_config("SCIntegrator", circuit_id)
            plan = derive_plan(circuit_id)
            common = {"candidate_manifest_sha256": spec["candidate_manifest_sha256"]}
            runtime.testbench_config["_bulk_executor"] = SCIntegratorPhysicalExecutor(
                runtime, directory / "physical_profile", stage="profile", limit=40,
                extra_identity=common)
            target = prepare_experiment(
                "SCIntegrator", circuit_id, runtime=runtime, allow_spice=True,
                allow_llm=True, provider="openai", model=None,
                output_root=directory / "auto_target", profile_samples=12,
                max_profile_evaluations=20, additional_samples=0,
                max_anchor_evaluations=12, max_llm_calls=3,
                seed=42, d0_mode="independent")
            if target["status"] != "READY_FOR_D0":
                raise RuntimeError(f"AutoTarget {target['status']}: {target.get('reason')}")
            profile_count = runtime.testbench_config["_bulk_executor"].execution_statistics()[
                "physical_attempts"]
            config_path = Path(target["artifact_paths"]["config"]).resolve(strict=True)
            if not config_path.is_relative_to(directory.resolve()):
                raise ValueError("generated SCIntegrator AL config escaped Preparation")
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["circuit_info"] = {"circuit_type": "SCIntegrator", "circuit_id": circuit_id}
            config.setdefault("active_learning", {}).update({
                "max_iterations": 5, "stop_on_meets_spec": True,
                "prediction_prompt_version": prediction_prompt_version})
            data = config["data"]
            data.update({"d0_mode": "independent", "d0_path_relative_to": "config",
                         "d0_path": "d0/d0_v4_seed42.jsonl", "initialize_if_missing": True,
                         "sampling_mode": "v4", "seed": 42, "n_samples": 20,
                         "n_local": 10, "n_global": 9, "local_radius": .05})
            data.pop("local_factor", None)
            config["sc_integrator_bulk"] = {
                "batch_id": batch_id, "candidate_manifest_sha256": spec["candidate_manifest_sha256"],
                "receipt_path": str(receipt_path),
                "al_native_root": str(OUTPUT_ROOT / batch_id / "native" / circuit_id),
                "fixture_sha256": plan["fixture_sha256"],
                "stimulus_sha256": plan["stimulus_sha256"]}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            runtime.testbench_config["_bulk_executor"] = SCIntegratorPhysicalExecutor(
                runtime, directory / "physical_d0", stage="d0", limit=20,
                extra_identity={**common, "config_sha256": _sha(config_path)})
            d0_path = config_path.parent / data["d0_path"]
            records = initialize_configured_d0(runtime, config, config["target_specs"], d0_path)["records"]
            valid = sum(row.get("status", {}).get("functional_valid") is True for row in records)
            if len(records) != 20 or valid < 1:
                raise ValueError("independent D0 has no verified functional-valid physical point")
            profile_path = Path(target["artifact_paths"]["profiling"]).resolve(strict=True)
            artifacts = [config_path, d0_path, d0_path.with_suffix(".csv"),
                         d0_path.with_name(d0_path.stem + "_initialization.json"),
                         *(Path(target["artifact_paths"][name]).resolve(strict=True)
                           for name in ("anchor", "profiling", "target_generation", "result"))]
            for stage in ("profile", "d0"):
                artifacts.extend(directory / f"physical_{stage}" / name for name in
                                 ("identity.json", "physical_attempt_ledger.jsonl"))
            if any(not path.is_file() or not path.is_relative_to(directory.resolve())
                   for path in artifacts):
                raise ValueError("SCIntegrator Preparation artifacts incomplete/escaped")
            saved = {"status": "PREPARATION_READY", "batch_id": batch_id,
                     "circuit_id": circuit_id,
                     "candidate_manifest_sha256": spec["candidate_manifest_sha256"],
                     "source_sha256": plan["source_sha256"],
                     "fixture_sha256": plan["fixture_sha256"],
                     "stimulus_sha256": plan["stimulus_sha256"],
                     "target_spec_sha256": canonical_sha256(config["target_specs"]),
                     "config_path": config_path.relative_to(directory).as_posix(),
                     "d0_path": d0_path.relative_to(directory).as_posix(),
                     "profile_path": profile_path.relative_to(directory).as_posix(),
                     "functional_valid_count": valid,
                     "physical_attempts": {"profile": profile_count,
                         "d0": runtime.testbench_config["_bulk_executor"].execution_statistics()[
                             "physical_attempts"]},
                     "artifacts": {path.relative_to(directory).as_posix(): _sha(path)
                                   for path in artifacts}}
            atomic_json(receipt_path, saved)
            results[circuit_id] = saved
        except Exception as error:
            saved = {"status": "BLOCKED", "batch_id": batch_id,
                     "circuit_id": circuit_id,
                     "reason": f"{type(error).__name__}: {error}"}
            atomic_json(receipt_path, saved)
            results[circuit_id] = saved
    summary = {"batch_id": batch_id, "status": "PREPARATION_COMPLETE",
               "ready": sum(row["status"] == "PREPARATION_READY" for row in results.values()),
               "blocked": sum(row["status"] == "BLOCKED" for row in results.values()),
               "circuits": results}
    atomic_json(directory_root / "summary.json", summary)
    return summary


def preflight(batch_id: str) -> tuple[dict, dict]:
    _linux()
    from .sample_hold_bulk_production import _bytecat_settings
    from ActiveLearning.onboarding import build_active_learning_circuit_config
    from ActiveLearning.onboarding.sc_integrator_bulk_native import SCIntegratorPhysicalExecutor
    from .schema import canonical_sha256, configuration_identity, verify_job
    import yaml

    spec = inspect_manifest(batch_id)
    model, base_url, transport = _bytecat_settings()
    jobs, blocked = [], {}
    for circuit_id in spec["circuit_ids"]:
        try:
            saved = _receipt(spec, circuit_id)
            if saved["status"] != "PREPARATION_READY":
                blocked[circuit_id] = saved.get("reason", "Preparation BLOCKED")
                continue
            directory = Path(spec["preparation_root"]) / circuit_id
            config_path = directory / saved["config_path"]
            d0_path = directory / saved["d0_path"]
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            runtime = build_active_learning_circuit_config("SCIntegrator", circuit_id)
            executor = SCIntegratorPhysicalExecutor(
                runtime, OUTPUT_ROOT / batch_id / "native" / circuit_id,
                stage="al", limit=5,
                extra_identity={"batch_id": batch_id, "config_sha256": _sha(config_path),
                                "d0_sha256": _sha(d0_path)})
            executor.verify_existing()
            source = f"dataset/{circuit_id}/topology/{circuit_id}.py"
            jobs.append({"job_id": circuit_id, "circuit_type": "SCIntegrator",
                         "circuit_id": circuit_id,
                         "configuration_id": configuration_identity(config, _sha(config_path)),
                         "config_path": str(config_path), "config_sha256": _sha(config_path),
                         "d0_path": str(d0_path), "d0_sha256": _sha(d0_path),
                         "source_path": source, "source_sha256": _sha(ROOT / source),
                         "target_spec_sha256": saved["target_spec_sha256"],
                         "max_iterations": 5, "seed": 42, "execution_mode": "al_only",
                         "sc_integrator_execution_id": f"native/{circuit_id}"})
        except Exception as error:
            blocked[circuit_id] = f"{type(error).__name__}: {error}"
    if not jobs:
        return {"batch_id": batch_id, "status": "NO_READY_JOBS"}, blocked
    sources = {name: _sha(ROOT / name) for name in (
        "ActiveLearning/onboarding/sc_integrator_bulk_core.py",
        "ActiveLearning/onboarding/sc_integrator_bulk_child.py",
        "ActiveLearning/onboarding/sc_integrator_bulk_native.py",
        "ActiveLearning/onboarding/sample_hold_bulk_core.py",
        "ActiveLearning/onboarding/sample_hold.py",
        "ActiveLearning/sample_hold_posix.py",
        "ActiveLearning/sample_hold_linux_canary.py",
        "run/single_stage_amplifier/run_ideal_dataset_simulations.py",
        "ActiveLearning/onboarding/sc_integrator_bulk_fixtures.json",
        "ActiveLearning/onboarding/sample_hold_bulk_native.py",
        "ActiveLearning/batch_runner_v2/sc_integrator_production.py",
        "ActiveLearning/batch_runner_v2/runner.py",
        "ActiveLearning/batch_runner_v2/schema.py",
        "ActiveLearning/main.py", "ActiveLearning/auto_target.py",
        "ActiveLearning/d0_initialization.py",
        "ActiveLearning/InitializeD0/d0_builder.py",
        "ActiveLearning/InitializeD0/validation/parameter_validator.py",
        "ActiveLearning/InitializeD0/validation/spec_validator.py",
        "ActiveLearning/active_learning.py",
        "ActiveLearning/experiment_logging.py",
        "ActiveLearning/real_evaluation/real_evaluator.py",
        "ActiveLearning/circuit_types/SCIntegrator/performance_template.yaml",
        "ActiveLearning/onboarding/runtime_provider.py",
        "ActiveLearning/onboarding/auto_config.py",
        "ActiveLearning/inverse_design/llm_client.py",
        "ActiveLearning/inverse_design/prompt_builder.py",
        "ActiveLearning/inverse_design/prompts/circuit_types/sc_integrator_bulk_v1.txt",
        "ActiveLearning/performance_prediction/performance_predictor.py",
        "ActiveLearning/performance_prediction/prompts/circuit_types/sc_integrator_bulk_v1.txt")}
    if any(yaml.safe_load(Path(job["config_path"]).read_text(encoding="utf-8"))
           .get("active_learning", {}).get("prediction_prompt_version") == "compact_v2"
           for job in jobs):
        for name in (
            "ActiveLearning/performance_prediction/sample_hold_compact.py",
            "ActiveLearning/performance_prediction/sc_integrator_compact_v2.py",
            "ActiveLearning/performance_prediction/prompts/circuit_types/sc_integrator_compact_v2.txt",
        ):
            sources[name] = _sha(ROOT / name)
    manifest = {"schema_version": "sc_integrator_bulk_runtime_v1",
                "batch_id": batch_id, "provider": "openai", "model": model,
                "provider_base_url": base_url, "transport": transport,
                "credential_scope": "bytecat_ubuntu",
                "resource_limits": {"native_attempts_max_total": len(jobs) * 5,
                                    "per_circuit_native_attempts_max": 5,
                                    "worker_timeout_seconds": 2400},
                "retry_policy": {"automatic_native_retries": 0},
                "jobs": jobs, "source_closure": sources,
                "repository_root": str(ROOT),
                "preparation_root": spec["preparation_root"],
                "candidate_manifest_sha256": spec["candidate_manifest_sha256"]}
    manifest["manifest_sha256"] = canonical_sha256({"runtime": manifest})
    for job in jobs:
        verify_job(manifest, job)
    return manifest, blocked


def _child_result(manifest: dict, job: dict, attempt: Path) -> dict:
    from ActiveLearning import main as al_main
    from .storage import atomic_json, read_json, sha256_file
    batch_root = attempt.parents[3]
    if (attempt != batch_root / "jobs" / job["job_id"] / "attempts" / attempt.name
            or read_json(attempt / "child_spawn_intent.json") != {
                "manifest_sha256": manifest["manifest_sha256"],
                "job_id": job["job_id"], "attempt_id": attempt.name}):
        raise PermissionError("SCIntegrator child escaped scheduled attempt")
    state = read_json(batch_root / "status.json")
    row = state["jobs"][job["job_id"]]
    if (state["manifest_sha256"] != manifest["manifest_sha256"]
            or row["state"] != "RUNNING"
            or row["attempts"][-1]["attempt_id"] != attempt.name):
        raise PermissionError("SCIntegrator child scheduler identity changed")
    context = {}

    def capture(value):
        context.update(value)
        atomic_json(attempt / "al_run_context.json", {
            "run_id": value["run_id"], "run_directory": str(value["run_directory"])})

    result = al_main.run_configured_active_learning(
        "SCIntegrator", job["circuit_id"], config_path=Path(job["config_path"]),
        runs_root=attempt / "al_runs", run_context_callback=capture,
        require_existing_d0=True, expected_config_sha256=job["config_sha256"],
        expected_d0_sha256=job["d0_sha256"])
    run_dir = Path(context["run_directory"]).resolve(strict=True)
    if not run_dir.is_relative_to(attempt.resolve()) or run_dir.is_symlink():
        raise ValueError("SCIntegrator AL run escaped Batch attempt")
    summary = run_dir / "summary.json"
    completed = read_json(summary)
    if any(completed.get(key) != result.get(key) for key in
           ("iterations_completed", "stop_reason", "llm_calls", "total_tokens")):
        raise ValueError("SCIntegrator AL summary differs from returned result")
    ledger = batch_root / job["sc_integrator_execution_id"] / "physical_attempt_ledger.jsonl"
    outcome = {"state": "COMPLETED", "run_id": context["run_id"],
               "stop_reason": completed["stop_reason"],
               "iterations_completed": completed["iterations_completed"],
               "parameter_valid": completed["parameter_valid"],
               "simulation_valid": completed["simulation_valid"],
               "functional_valid": completed["functional_valid"],
               "meets_spec": completed["meets_spec"],
               "final_metrics": completed["final_metrics"],
               "d0_record_count": sum(bool(line.strip()) for line in Path(job["d0_path"]).read_text(
                   encoding="utf-8").splitlines()),
               "result_path": str(summary), "al_result_sha256": sha256_file(summary),
               "llm_calls": completed["llm_calls"], "total_tokens": completed["total_tokens"],
               "usage_unknown": completed.get("usage_unknown", False),
               "total_timing": completed["total_timing"],
               "physical_ledger_path": str(ledger)}
    atomic_json(attempt / "worker_result.json", outcome)
    return outcome


def _failed_usage(manifest: dict, job: dict, attempt: Path) -> dict:
    """Recover only complete, attributable usage rows after a confirmed child exit."""
    from .storage import read_json

    recovered = {"usage_unknown": True}
    context_path = attempt / "al_run_context.json"
    if (any(path.is_symlink() for path in (attempt, *attempt.parents[:4]))
            or attempt.parent.name != "attempts"
            or attempt.parent.parent.name != job["job_id"]
            or attempt.parent.parent.parent.name != "jobs"
            or attempt.parents[3].name != manifest["batch_id"]
            or not context_path.is_file() or context_path.is_symlink()):
        recovered["usage_recovery_issue"] = "USAGE_CONTEXT_MISSING_OR_UNOWNED"
        return recovered
    try:
        reaped = read_json(attempt / "child_reaped.json")
    except (OSError, TypeError, ValueError):
        recovered["usage_recovery_issue"] = "USAGE_REAP_UNCONFIRMED"
        return recovered
    if reaped.get("exit_confirmed") is not True or reaped.get("attempt_id") != attempt.name:
        recovered["usage_recovery_issue"] = "USAGE_REAP_UNCONFIRMED"
        return recovered
    try:
        context = read_json(context_path)
    except (OSError, TypeError, ValueError):
        recovered["usage_recovery_issue"] = "USAGE_CONTEXT_INVALID"
        return recovered
    run_id = context.get("run_id")
    raw_value = context.get("run_directory")
    if (not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id
            or not isinstance(raw_value, str) or not Path(raw_value).is_absolute()):
        recovered["usage_recovery_issue"] = "USAGE_IDENTITY_MISMATCH"
        return recovered
    raw_dir = Path(raw_value)
    expected = attempt / "al_runs" / "SCIntegrator" / job["circuit_id"] / run_id
    try:
        if (any(path.is_symlink() for path in (
                expected.parent.parent.parent, expected.parent.parent,
                expected.parent, expected, expected / "llm_usage.jsonl"))
                or raw_dir.resolve(strict=True) != expected.resolve(strict=True)
                or not expected.resolve(strict=True).is_relative_to(attempt.resolve(strict=True))):
            recovered["usage_recovery_issue"] = "USAGE_IDENTITY_MISMATCH"
            return recovered
        usage_path = expected / "llm_usage.jsonl"
        if not usage_path.is_file():
            recovered["usage_recovery_issue"] = "USAGE_LOG_MISSING"
            return recovered
        data = usage_path.read_bytes()
    except (OSError, TypeError, ValueError):
        recovered["usage_recovery_issue"] = "USAGE_READ_ERROR"
        return recovered

    fields = ("input_tokens", "output_tokens", "total_tokens")
    totals = {field: 0 for field in fields}
    count = 0
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            recovered["usage_recovery_issue"] = "USAGE_TRAILING_PARTIAL"
            break
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            recovered["usage_recovery_issue"] = "USAGE_CORRUPT_RECORD"
            break
        if not isinstance(row, dict) or any(
                type(row.get(field)) is not int or row[field] < 0 for field in fields):
            recovered["usage_recovery_issue"] = "USAGE_CORRUPT_RECORD"
            break
        if (row.get("run_id", run_id) != run_id
                or row.get("job_id", job["job_id"]) != job["job_id"]
                or row.get("attempt_id", attempt.name) != attempt.name):
            recovered["usage_recovery_issue"] = "USAGE_RECORD_IDENTITY_MISMATCH"
            break
        count += 1
        for field in fields:
            totals[field] += row[field]
    if count:
        recovered.update({"llm_calls": count, "total_tokens": totals})
    return recovered


def _worker(manifest: dict, job: dict, attempt: Path) -> dict:
    from ActiveLearning.sample_hold_posix import ProcessGroupUncertain, run_isolated
    from .runner import UnverifiedOrphanError
    from .storage import atomic_json, read_json, sha256_file
    atomic_json(attempt / "child_spawn_intent.json", {
        "manifest_sha256": manifest["manifest_sha256"],
        "job_id": job["job_id"], "attempt_id": attempt.name})
    command = [sys.executable, "-m", "ActiveLearning.batch_runner_v2.sc_integrator_production",
               "child", "--batch-id", manifest["batch_id"], "--job-id", job["job_id"],
               "--attempt", str(attempt)]
    try:
        completed = run_isolated(command, cwd=ROOT, timeout=2400,
                                 environment=dict(os.environ))
    except ProcessGroupUncertain as error:
        raise UnverifiedOrphanError("SCIntegrator Batch child reaping uncertain") from error
    except subprocess.TimeoutExpired:
        atomic_json(attempt / "child_reaped.json", {"attempt_id": attempt.name,
                    "exit_confirmed": True, "exit_code": 124})
        return {"state": "FAILED_TERMINAL", "error_code": "WORKER_TIMEOUT_REAPED",
                **_failed_usage(manifest, job, attempt)}
    atomic_json(attempt / "child_reaped.json", {"attempt_id": attempt.name,
                "exit_confirmed": True, "exit_code": completed.returncode})
    (attempt / "worker_stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (attempt / "worker_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        return {"state": "FAILED_TERMINAL",
                "error_code": f"CHILD_EXIT_{completed.returncode}",
                **_failed_usage(manifest, job, attempt)}
    result = read_json(attempt / "worker_result.json")
    path = Path(result["result_path"]).resolve(strict=True)
    if (result.get("state") != "COMPLETED" or not path.is_relative_to(attempt.resolve())
            or sha256_file(path) != result.get("al_result_sha256")):
        raise ValueError("SCIntegrator child result provenance mismatch")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SCIntegrator 82-Batch Preparation + AL")
    parser.add_argument("command", choices=("inspect", "preparation", "preflight", "run",
                                             "status", "report", "child"))
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--allow-spice", action="store_true")
    parser.add_argument("--allow-llm", action="store_true")
    parser.add_argument("--retry-autoconfig-blocked", action="store_true")
    parser.add_argument("--prediction-prompt-version", choices=("v1", "compact_v2"),
                        default="v1", help="Preparation only; default preserves V1")
    parser.add_argument("--job-id")
    parser.add_argument("--attempt", type=Path)
    args = parser.parse_args(argv)
    if args.command != "preparation" and args.prediction_prompt_version != "v1":
        parser.error("--prediction-prompt-version is only valid for preparation")
    if args.command == "inspect":
        result = inspect_manifest(args.batch_id)
    elif args.command == "preparation":
        result = preparation(args.batch_id, allow_spice=args.allow_spice,
                             allow_llm=args.allow_llm,
                             retry_autoconfig_blocked=args.retry_autoconfig_blocked,
                             prediction_prompt_version=args.prediction_prompt_version)
    elif args.command in {"status", "report"}:
        from .runner import report_batch, status_batch
        spec = inspect_manifest(args.batch_id)
        prep = Path(spec["preparation_root"]) / "summary.json"
        result = {"batch_id": args.batch_id,
                  "preparation": json.loads(prep.read_text(encoding="utf-8"))
                  if prep.is_file() else None}
        batch_dir = OUTPUT_ROOT / args.batch_id
        if (batch_dir / "status.json").is_file():
            result["batch"] = (status_batch(args.batch_id, OUTPUT_ROOT)
                               if args.command == "status" else
                               report_batch(args.batch_id, OUTPUT_ROOT))
    else:
        manifest, blocked = preflight(args.batch_id)
        if args.command == "preflight":
            result = {"batch_id": args.batch_id,
                      "status": manifest.get("status", "READY"),
                      "ready_jobs": [job["job_id"] for job in manifest.get("jobs", [])],
                      "blocked_jobs": blocked,
                      "manifest_sha256": manifest.get("manifest_sha256")}
        elif manifest.get("status") == "NO_READY_JOBS":
            raise RuntimeError("no PREPARATION_READY SCIntegrator circuit")
        elif args.command == "child":
            if not args.job_id or args.attempt is None:
                parser.error("child requires --job-id and --attempt")
            job = next((item for item in manifest["jobs"] if item["job_id"] == args.job_id), None)
            if job is None:
                raise ValueError("SCIntegrator child job not in READY Batch")
            result = _child_result(manifest, job, args.attempt.resolve(strict=True))
        else:
            from .runner import _run_batch
            result = _run_batch(manifest, OUTPUT_ROOT,
                                lambda job, attempt: _worker(manifest, job, attempt),
                                offline_only=False, execution_scope="sc_integrator_production")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
