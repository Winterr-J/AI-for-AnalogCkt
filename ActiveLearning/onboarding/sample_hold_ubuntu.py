"""Fail-closed admission for an operator-reviewed Ubuntu SampleHold runtime."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from .errors import AutoConfigError


SCHEMA = "sample_hold_ubuntu_acceptance_v1"
ENVIRONMENT_VARIABLE = "SAMPLE_HOLD_UBUNTU_ACCEPTANCE_FILE"
_KEYS = {
    "schema", "status", "circuit_id", "config_sha256", "d0_sha256",
    "repository_root", "python_executable", "output_root", "accepted_by",
    "accepted_at",
}


def _is_ubuntu() -> bool:
    return os.name == "posix" and sys.platform.startswith("linux")


def acceptance_environment_variable(circuit_id: str) -> str:
    return f"{ENVIRONMENT_VARIABLE}_{circuit_id.removeprefix('circuit_')}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_ubuntu_acceptance(runtime: Any, config_path: Path,
                            d0_path: Path) -> dict[str, Any]:
    """Load exact operator evidence; absence or drift keeps physical AL closed."""
    if not _is_ubuntu():
        raise AutoConfigError("SampleHold physical AL requires Ubuntu acceptance")
    specific_variable = acceptance_environment_variable(runtime.circuit_id)
    raw_path = os.getenv(specific_variable) or os.getenv(ENVIRONMENT_VARIABLE)
    if not raw_path or not Path(raw_path).is_absolute():
        raise AutoConfigError(
            "SampleHold physical AL requires "
            f"{specific_variable} or {ENVIRONMENT_VARIABLE}"
        )
    acceptance_path = Path(raw_path)
    if acceptance_path.is_symlink():
        raise AutoConfigError("SampleHold Ubuntu acceptance must not be a symlink")
    try:
        acceptance_path = acceptance_path.resolve(strict=True)
        payload = json.loads(acceptance_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise AutoConfigError("SampleHold Ubuntu acceptance is unreadable") from error
    if not isinstance(payload, dict):
        raise AutoConfigError("SampleHold Ubuntu acceptance must be an object")
    root = Path(__file__).resolve().parents[2]
    config_path = Path(config_path).resolve(strict=True)
    d0_path = Path(d0_path).resolve(strict=True)
    output_root = Path(payload.get("output_root") or "")
    try:
        accepted_at = datetime.fromisoformat(payload.get("accepted_at"))
    except (TypeError, ValueError) as error:
        raise AutoConfigError("SampleHold Ubuntu acceptance timestamp invalid") from error
    if (set(payload) != _KEYS
            or payload.get("schema") != SCHEMA
            or payload.get("status") != "ubuntu_physical_accepted"
            or payload.get("circuit_id") != runtime.circuit_id
            or runtime.circuit_id not in {"circuit_0259", "circuit_0263"}
            or payload.get("config_sha256") != _sha256(config_path)
            or payload.get("d0_sha256") != _sha256(d0_path)
            or payload.get("repository_root") != str(root)
            or payload.get("python_executable") != str(Path(sys.executable).resolve())
            or not output_root.is_absolute()
            or output_root.resolve().is_relative_to(root)
            or not isinstance(payload.get("accepted_by"), str)
            or not payload["accepted_by"].strip()
            or accepted_at.tzinfo is None or accepted_at.utcoffset() is None):
        raise AutoConfigError(
            "SampleHold Ubuntu acceptance does not bind the exact runtime"
        )
    return {
        **payload,
        "acceptance_path": str(acceptance_path),
        "acceptance_sha256": _sha256(acceptance_path),
        "acceptance_environment_variable": (
            specific_variable if os.getenv(specific_variable)
            else ENVIRONMENT_VARIABLE
        ),
        "output_root": str(output_root.resolve()),
    }
