"""Strict, read-only validation of an AL-only offline batch manifest."""

from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import yaml
except ImportError:  # The bundled offline test runtime has no PyYAML.
    yaml = None

from .storage import sha256_file

_LABEL = re.compile(r"[A-Za-z0-9_-]+\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_JOB_KEYS = {
    "job_id", "circuit_type", "circuit_id", "configuration_id",
    "config_path", "config_sha256", "d0_path", "d0_sha256",
    "source_path", "source_sha256", "max_iterations", "seed",
    "execution_mode",
}


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ValueError(f"{name} must be a safe nonempty label")
    return value


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _asset(root: Path, name: object, digest: object) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute() or \
            Path(name).drive or "\\" in name:
        raise ValueError("asset path must be a relative POSIX path")
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"asset escapes repository or is not a file: {name}")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError(f"invalid SHA-256 for {name}")
    if sha256_file(path) != digest:
        raise ValueError(f"asset hash mismatch: {name}")
    return path


def verify_job(manifest: dict, job: dict) -> None:
    root = Path(manifest["repository_root"])
    config_path = _asset(root, job["config_path"], job["config_sha256"])
    d0_path = _asset(root, job["d0_path"], job["d0_sha256"])
    _asset(root, job["source_path"], job["source_sha256"])
    source = config_path.read_text(encoding="utf-8")
    try:
        config = json.loads(source)  # JSON is a YAML subset; useful offline.
    except json.JSONDecodeError:
        if yaml is None:
            raise RuntimeError("PyYAML is required to validate non-JSON AL configs")
        config = yaml.safe_load(source)
    if not isinstance(config, dict):
        raise ValueError("AL config must be a mapping")
    if config.get("configuration_id") != job["configuration_id"]:
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
    if job["circuit_type"] == "VoltageReference" and job["circuit_id"] == "circuit_0490":
        raise ValueError("AG626 one-time AL authorization is not supported by V2-A")


def load_manifest(path: str | Path) -> dict:
    source = Path(path).resolve(strict=True)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "batch_id", "provider", "model", "credential_scope",
        "resource_limits", "retry_policy", "jobs",
    }:
        raise ValueError("invalid V2-A manifest fields")
    if payload["schema_version"] != "batch_runner_v2a_offline_v1":
        raise ValueError("unsupported V2-A manifest version")
    _label(payload["batch_id"], "batch_id")
    if payload["provider"] != "gemini":
        raise ValueError("V2-A recovery adapter supports only mocked Gemini")
    for name in ("model", "credential_scope"):
        _label(payload[name], name)
    limits = payload["resource_limits"]
    if not isinstance(limits, dict) or set(limits) != {"max_physical_attempts"}:
        raise ValueError("V2-A supports only an enforceable physical-attempt cap")
    _positive(limits["max_physical_attempts"], "max_physical_attempts")
    retry = payload["retry_policy"]
    if not isinstance(retry, dict) or set(retry) != {
        "request_timeout_seconds", "max_attempts", "max_elapsed_seconds"
    }:
        raise ValueError("invalid retry policy")
    for name in retry:
        _positive(retry[name], name)
    jobs = payload["jobs"]
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("batch needs at least one job")
    seen = set()
    root = source.parent
    result = {**payload, "repository_root": str(root),
              "manifest_path": str(source), "manifest_sha256": sha256_file(source)}
    for job in jobs:
        if not isinstance(job, dict) or set(job) != _JOB_KEYS:
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
        verify_job(result, job)
    return result
