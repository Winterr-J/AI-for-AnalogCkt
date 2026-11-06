"""Strict, read-only validation of an AL-only offline batch manifest."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

try:
    import yaml
except ImportError:  # The bundled offline test runtime has no PyYAML.
    yaml = None

from .storage import sha256_file
from ActiveLearning.inverse_design.qwen_provider import (
    QWEN_FLASH_MODEL, validate_qwen_endpoint,
)

_LABEL = re.compile(r"[A-Za-z0-9_-]+\Z")
_MODEL = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40,64}\Z")
_V2A_JOB_KEYS = {
    "job_id", "circuit_type", "circuit_id", "configuration_id",
    "config_path", "config_sha256", "d0_path", "d0_sha256",
    "source_path", "source_sha256", "max_iterations", "seed",
    "execution_mode",
}
_B2A_JOB_KEYS = _V2A_JOB_KEYS | {"target_spec_sha256"}
_V2A_MANIFEST_KEYS = {
    "schema_version", "batch_id", "provider", "model", "credential_scope",
    "resource_limits", "retry_policy", "jobs",
}
_B2A_MANIFEST_KEYS = _V2A_MANIFEST_KEYS | {"source_revision", "source_closure"}
_REQUIRED_REAL_AL_CLOSURE = {
    "ActiveLearning/main.py",
    "ActiveLearning/active_learning.py",
    "ActiveLearning/d0_initialization.py",
    "ActiveLearning/experiment_logging.py",
    "ActiveLearning/real_evaluation/real_evaluator.py",
    "ActiveLearning/InitializeD0/evaluator/current_mirror_evaluator.py",
    "ActiveLearning/InitializeD0/validation/current_mirror_functional_validator.py",
    "ActiveLearning/InitializeD0/validation/parameter_validator.py",
    "ActiveLearning/inverse_design/llm_client.py",
    "ActiveLearning/inverse_design/qwen_provider.py",
    "ActiveLearning/batch_runner_v2/al_process.py",
    "ActiveLearning/batch_runner_v2/authorization.py",
    "ActiveLearning/batch_runner_v2/gemini_recovery.py",
    "ActiveLearning/batch_runner_v2/llm_bridge.py",
    "ActiveLearning/batch_runner_v2/real_al_child.py",
    "ActiveLearning/batch_runner_v2/runner.py",
    "ActiveLearning/batch_runner_v2/schema.py",
    "ActiveLearning/batch_runner_v2/storage.py",
}
_REQUIRED_SAMPLE_HOLD_CLOSURE = {
    "ActiveLearning/onboarding/sample_hold.py",
    "ActiveLearning/onboarding/sample_hold_batch.py",
    "ActiveLearning/onboarding/sample_hold_batch_child.py",
    "ActiveLearning/onboarding/sample_hold_phase_a.py",
    "ActiveLearning/onboarding/sample_hold_ubuntu.py",
    "ActiveLearning/onboarding/models.py",
    "ActiveLearning/onboarding/runtime_provider.py",
    "ActiveLearning/sample_hold_posix.py",
}


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ValueError(f"{name} must be a safe nonempty label")
    return value


def _model(value: object) -> str:
    if not isinstance(value, str) or not _MODEL.fullmatch(value):
        raise ValueError("model must be a safe nonempty model ID")
    return value


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def canonical_sha256(value: object) -> str:
    """Hash a JSON-compatible identity without depending on file formatting."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _asset(root: Path, name: object, digest: object) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute() or \
            Path(name).drive or "\\" in name:
        raise ValueError("asset path must be a relative POSIX path")
    candidate = root / name
    if candidate.is_symlink():
        raise ValueError(f"asset must not be a symbolic link: {name}")
    path = candidate.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"asset escapes repository or is not a file: {name}")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError(f"invalid SHA-256 for {name}")
    if sha256_file(path) != digest:
        raise ValueError(f"asset hash mismatch: {name}")
    return path


def _load_config(config_path: Path) -> dict:
    source = config_path.read_text(encoding="utf-8")
    try:
        config = json.loads(source)  # JSON is a YAML subset; useful offline.
    except json.JSONDecodeError:
        if yaml is None:
            raise RuntimeError("PyYAML is required to validate non-JSON AL configs")
        config = yaml.safe_load(source)
    if not isinstance(config, dict):
        raise ValueError("AL config must be a mapping")
    return config


def configuration_identity(config: dict, config_sha256: str) -> str:
    """Use an explicit identity when present, otherwise a hash-bound fallback."""
    for name in ("configuration_id", "benchmark_version"):
        value = config.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return f"sha256-{config_sha256[:16]}"


def verify_job(manifest: dict, job: dict) -> None:
    if manifest.get("schema_version") == "sc_integrator_bulk_runtime_v1":
        from .sc_integrator_production import _receipt, inspect_manifest
        spec = inspect_manifest(manifest["batch_id"])
        if (manifest.get("preparation_root") != spec["preparation_root"]
                or manifest.get("candidate_manifest_sha256") != spec["candidate_manifest_sha256"]
                or job.get("circuit_type") != "SCIntegrator"
                or job.get("circuit_id") not in spec["circuit_ids"]
                or job.get("job_id") != job.get("circuit_id")
                or job.get("max_iterations") != 5 or job.get("seed") != 42
                or job.get("execution_mode") != "al_only"
                or job.get("sc_integrator_execution_id") != "native/" + job["circuit_id"]):
            raise ValueError("SCIntegrator bulk job identity/policy mismatch")
        saved = _receipt(spec, job["circuit_id"])
        if saved.get("status") != "PREPARATION_READY":
            raise ValueError("SCIntegrator job has no READY Preparation")
        prep = Path(spec["preparation_root"]) / job["circuit_id"]
        config_path = (prep / saved["config_path"]).resolve(strict=True)
        d0_path = (prep / saved["d0_path"]).resolve(strict=True)
        root = Path(manifest["repository_root"]).resolve(strict=True)
        source_path = (root / job["source_path"]).resolve(strict=True)
        config = _load_config(config_path)
        if (Path(job["config_path"]).resolve(strict=True) != config_path
                or Path(job["d0_path"]).resolve(strict=True) != d0_path
                or not source_path.is_relative_to(root)
                or sha256_file(config_path) != job["config_sha256"]
                or sha256_file(d0_path) != job["d0_sha256"]
                or sha256_file(source_path) != job["source_sha256"]
                or canonical_sha256(config.get("target_specs")) != job["target_spec_sha256"]
                or config.get("active_learning", {}).get("max_iterations") != 5
                or config["active_learning"].get("stop_on_meets_spec") is not False
                or config.get("data", {}).get("d0_mode") != "independent"
                or config["data"].get("seed") != 42
                or config["data"].get("n_samples") != 20):
            raise ValueError("SCIntegrator bulk config/D0/source hash mismatch")
        for name, digest in manifest["source_closure"].items():
            _asset(root, name, digest)
        return
    if manifest.get("schema_version") == "sample_hold_bulk_runtime_v1":
        from .sample_hold_bulk_production import EXPECTED, _receipt, inspect_manifest

        spec = inspect_manifest(Path(manifest["manifest_path"]))
        if (manifest.get("batch_id") != spec["batch_id"]
                or manifest.get("preparation_root") != spec["preparation_root"]
                or job.get("circuit_type") != "SampleHold"
                or job.get("circuit_id", "")[-4:] not in EXPECTED[spec["batch_id"]]
                or job.get("job_id") != "sc" + job["circuit_id"][-4:]
                or job.get("max_iterations") != 5 or job.get("seed") != 42
                or job.get("execution_mode") != "al_only"
                or job.get("sample_hold_execution_id") != "native/" + job["job_id"]):
            raise ValueError("SampleHold bulk job identity/policy mismatch")
        item = next(row for row in spec["jobs"] if row["job_id"] == job["job_id"])
        saved = _receipt(spec, item)
        if saved.get("status") != "PREPARATION_READY":
            raise ValueError("SampleHold bulk job has no READY preparation")
        prep = Path(spec["preparation_root"]) / job["circuit_id"]
        config_path = (prep / saved["config_path"]).resolve(strict=True)
        d0_path = (prep / saved["d0_path"]).resolve(strict=True)
        root = Path(manifest["repository_root"]).resolve(strict=True)
        source_path = (root / job["source_path"]).resolve(strict=True)
        config = _load_config(config_path)
        if (Path(job["config_path"]).resolve(strict=True) != config_path
                or Path(job["d0_path"]).resolve(strict=True) != d0_path
                or not source_path.is_relative_to(root)
                or sha256_file(config_path) != job["config_sha256"]
                or sha256_file(d0_path) != job["d0_sha256"]
                or sha256_file(source_path) != job["source_sha256"]
                or canonical_sha256(config.get("target_specs")) != job["target_spec_sha256"]
                or config.get("active_learning", {}).get("max_iterations") != 5
                or config["active_learning"].get("stop_on_meets_spec") is not False
                or config.get("data", {}).get("d0_mode") != "independent"
                or config["data"].get("seed") != 42
                or config["data"].get("n_samples") != 20):
            raise ValueError("SampleHold bulk job config/D0/source hash mismatch")
        for name, digest in manifest["source_closure"].items():
            _asset(root, name, digest)
        return
    root = Path(manifest["repository_root"])
    config_path = _asset(root, job["config_path"], job["config_sha256"])
    d0_path = _asset(root, job["d0_path"], job["d0_sha256"])
    _asset(root, job["source_path"], job["source_sha256"])
    config = _load_config(config_path)
    expected_configuration = (config.get("configuration_id")
                              if manifest["schema_version"] == "batch_runner_v2a_offline_v1"
                              else configuration_identity(config, job["config_sha256"]))
    if expected_configuration != job["configuration_id"]:
        raise ValueError("configuration identity mismatch")
    settings = config.get("active_learning") or {}
    if settings.get("max_iterations") != job["max_iterations"]:
        raise ValueError("AL iteration limit differs from frozen config")
    if settings.get("data_expansion_seed", 42) != job["seed"]:
        raise ValueError("AL seed differs from frozen config")
    data = config.get("data") or {}
    raw = data.get("d0_path")
    if not isinstance(raw, str) or not raw:
        raise ValueError("configured D0 path missing")
    base = (config_path.parent if data.get("d0_path_relative_to", "circuit") == "config"
            else root / "ActiveLearning/InitializeD0/examples" /
            job["circuit_type"] / job["circuit_id"])
    if data.get("d0_path_relative_to", "circuit") not in {"config", "circuit"} or \
            Path(raw).is_absolute() or Path(raw).drive or "\\" in raw:
        raise ValueError("configured D0 path must be relative")
    if (base / raw).resolve() != d0_path:
        raise ValueError("manifest D0 differs from configured D0")
    if manifest["schema_version"] in {
            "batch_runner_v2b2a_candidate_v1", "sample_hold_batch_runtime_v1"}:
        target_specs = config.get("target_specs")
        if not isinstance(target_specs, dict) or not target_specs:
            raise ValueError("B2A requires a fixed nonempty performance specification")
        if canonical_sha256(target_specs) != job["target_spec_sha256"]:
            raise ValueError("performance specification identity mismatch")
    if job["circuit_type"] == "VoltageReference" and job["circuit_id"] == "circuit_0490":
        raise ValueError("AG626 one-time AL authorization is not supported by V2-A")
    if job["circuit_type"] == "SampleHold" and job["circuit_id"] not in {
            "circuit_0259", "circuit_0263"}:
        raise ValueError("Batch Runner V2 admits only reviewed SampleHold 0259/0263")


def _verify_source_closure(payload: dict, code_root: Path) -> None:
    closure = payload.get("source_closure")
    if not isinstance(closure, dict) or not closure:
        raise ValueError("B2A source closure must be a nonempty mapping")
    if not _REQUIRED_REAL_AL_CLOSURE.issubset(closure):
        missing = sorted(_REQUIRED_REAL_AL_CLOSURE - set(closure))
        raise ValueError(f"B2A source closure is incomplete: {', '.join(missing)}")
    if any(job.get("circuit_type") == "SampleHold" for job in payload["jobs"]):
        missing = sorted(_REQUIRED_SAMPLE_HOLD_CLOSURE - set(closure))
        if missing:
            raise ValueError(
                "B2A SampleHold source closure is incomplete: "
                + ", ".join(missing)
            )
    if payload.get("retry_policy", {}).get("rate_limit_mode") == "job_deadline" and (
            "ActiveLearning/batch_runner_v2/time_recovery.py" not in closure):
        raise ValueError("time-driven source closure is incomplete")
    for name, digest in closure.items():
        _asset(code_root, name, digest)


def load_manifest(path: str | Path) -> dict:
    source = Path(path).resolve(strict=True)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid V2-A manifest fields")
    version = payload.get("schema_version")
    if version == "batch_runner_v2a_offline_v1":
        expected_manifest_keys = _V2A_MANIFEST_KEYS
        expected_job_keys = _V2A_JOB_KEYS
    elif version == "batch_runner_v2b2a_candidate_v1":
        expected_manifest_keys = _B2A_MANIFEST_KEYS
        expected_job_keys = _B2A_JOB_KEYS
        if payload.get("provider") == "qwen":
            expected_manifest_keys = expected_manifest_keys | {"provider_base_url"}
        if "repository_root" in payload:
            expected_manifest_keys = expected_manifest_keys | {"repository_root"}
    else:
        raise ValueError("unsupported V2-A manifest version")
    if set(payload) != expected_manifest_keys:
        raise ValueError("invalid V2-A manifest fields")
    _label(payload["batch_id"], "batch_id")
    if payload["provider"] == "qwen":
        if version != "batch_runner_v2b2a_candidate_v1" or (
                payload["model"] != QWEN_FLASH_MODEL):
            raise ValueError("Qwen requires the approved B2A model")
        validate_qwen_endpoint(payload["provider_base_url"])
    elif payload["provider"] != "gemini":
        raise ValueError("unsupported V2 provider")
    _model(payload["model"])
    _label(payload["credential_scope"], "credential_scope")
    limits = payload["resource_limits"]
    expected_limits = ({"max_physical_attempts"} if version == "batch_runner_v2a_offline_v1"
                       else {"max_physical_attempts", "worker_timeout_seconds"})
    if not isinstance(limits, dict) or not expected_limits.issubset(limits) or (
            set(limits) - expected_limits - (
                {"per_job_physical_attempt_cap"} if version == "batch_runner_v2b2a_candidate_v1"
                else set())):
        raise ValueError("invalid enforceable V2 resource limits")
    _positive(limits["max_physical_attempts"], "max_physical_attempts")
    if "worker_timeout_seconds" in limits:
        _positive(limits["worker_timeout_seconds"], "worker_timeout_seconds")
    if "per_job_physical_attempt_cap" in limits:
        _positive(limits["per_job_physical_attempt_cap"], "per_job_physical_attempt_cap")
        if limits["per_job_physical_attempt_cap"] > limits["max_physical_attempts"]:
            raise ValueError("per-job physical cap exceeds batch cap")
    retry = payload["retry_policy"]
    base_retry = {"request_timeout_seconds", "max_attempts", "max_elapsed_seconds"}
    extra_retry = {"rate_limit_mode", "rate_limit_initial_backoff_seconds",
                   "rate_limit_max_backoff_seconds"}
    if not isinstance(retry, dict) or not base_retry.issubset(retry) or (
            set(retry) - base_retry - (
                extra_retry if version == "batch_runner_v2b2a_candidate_v1" else set())):
        raise ValueError("invalid retry policy")
    for name in base_retry:
        _positive(retry[name], name)
    if "rate_limit_mode" in retry and retry["rate_limit_mode"] != "job_deadline":
        raise ValueError("unsupported rate-limit recovery mode")
    if set(retry) & (extra_retry - {"rate_limit_mode"}):
        if retry.get("rate_limit_mode") != "job_deadline":
            raise ValueError("rate-limit backoff requires job_deadline mode")
        for name in extra_retry - {"rate_limit_mode"}:
            if name in retry:
                _positive(retry[name], name)
    if ("per_job_physical_attempt_cap" in limits
            and retry.get("rate_limit_mode") != "job_deadline"):
        raise ValueError("per-job physical cap requires job_deadline mode")
    if retry.get("rate_limit_mode") == "job_deadline":
        if "per_job_physical_attempt_cap" not in limits:
            raise ValueError("job_deadline mode requires a per-job physical cap")
        if (retry["request_timeout_seconds"] > 120
                or retry["max_attempts"] > 12
                or retry["max_elapsed_seconds"] > 1800
                or limits["worker_timeout_seconds"] > 4800):
            raise ValueError("job_deadline mode exceeds approved first-version time bounds")
        initial = retry.get("rate_limit_initial_backoff_seconds", 30)
        maximum = retry.get("rate_limit_max_backoff_seconds", 300)
        if not (1 <= initial <= maximum <= 300):
            raise ValueError("rate-limit backoff must be within 1..300 seconds")
    elif payload["provider"] == "qwen":
        raise ValueError("Qwen requires job_deadline recovery and per-job attempt cap")
    jobs = payload["jobs"]
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("batch needs at least one job")
    seen = set()
    root = source.parent
    code_root = Path(__file__).resolve().parents[2]
    if version == "batch_runner_v2b2a_candidate_v1":
        if not isinstance(payload["source_revision"], str) or not \
                _REVISION.fullmatch(payload["source_revision"]):
            raise ValueError("source_revision must be a full hexadecimal revision")
        if "repository_root" in payload:
            explicit_root = payload["repository_root"]
            if (not isinstance(explicit_root, str)
                    or not Path(explicit_root).is_absolute()
                    or Path(path).is_symlink()
                    or explicit_root != str(code_root)):
                raise ValueError("external manifest repository_root must be the executing release root")
            from .authorization import current_source_revision
            if current_source_revision(code_root) != payload["source_revision"]:
                raise ValueError("external manifest source revision differs from release HEAD")
            root = code_root
        _verify_source_closure(payload, code_root)
    result = {**payload, "repository_root": str(root),
              "code_root": str(code_root), "manifest_path": str(source),
              "manifest_sha256": sha256_file(source)}
    if version == "batch_runner_v2b2a_candidate_v1" and "repository_root" in payload:
        from .authorization import verify_published_source_closure
        verify_published_source_closure(result)
    for job in jobs:
        if not isinstance(job, dict) or set(job) != expected_job_keys:
            raise ValueError("invalid job fields")
        for name in ("job_id", "circuit_type", "circuit_id"):
            _label(job[name], name)
        if job["job_id"] in seen:
            raise ValueError("duplicate job ID")
        seen.add(job["job_id"])
        if not isinstance(job["configuration_id"], str) or not job["configuration_id"]:
            raise ValueError("configuration_id is required")
        if job["execution_mode"] != "al_only":
            raise ValueError("V2-A accepts AL-only jobs")
        _positive(job["max_iterations"], "max_iterations")
        if type(job["seed"]) is not int or job["seed"] < 0:
            raise ValueError("seed must be nonnegative")
        if version == "batch_runner_v2b2a_candidate_v1" and (
                not isinstance(job["target_spec_sha256"], str)
                or not _HASH.fullmatch(job["target_spec_sha256"])):
            raise ValueError("invalid target specification SHA-256")
        verify_job(result, job)
    return result
