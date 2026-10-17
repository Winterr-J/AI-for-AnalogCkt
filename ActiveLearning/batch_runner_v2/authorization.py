"""Fail-closed, externally pinned authorization for a B2A batch candidate."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from pathlib import Path

from .schema import canonical_sha256
from .storage import sha256_file


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9_-]+\Z")
_AUTHORIZATION_KEYS = {
    "schema_version", "authorization_id", "execution_scope", "approved",
    "approved_by", "approved_at", "manifest_sha256", "source_revision",
    "source_closure_sha256", "batch_id", "provider", "model",
    "credential_scope", "resource_limits", "retry_policy", "jobs",
    "output_root",
}
_BOUND_JOB_KEYS = (
    "job_id", "circuit_type", "circuit_id", "configuration_id",
    "config_sha256", "d0_sha256", "source_sha256", "target_spec_sha256",
    "max_iterations",
)


class AuthorizationError(RuntimeError):
    """The supplied batch approval is absent, untrusted, or mismatched."""


class ProductionExecutionDisabledError(AuthorizationError):
    """A production approval exists, but this caller did not enable production."""


def current_source_revision(code_root: str | Path | None = None) -> str:
    """Return the exact Git revision for the code root without changing Git state."""
    root = (Path(__file__).resolve().parents[2] if code_root is None
            else Path(code_root).resolve(strict=True))
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root),
             "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AuthorizationError("cannot determine the executing Git revision") from error
    revision = result.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise AuthorizationError("executing Git revision is not a full hash")
    return revision


def verify_published_source_closure(manifest: dict) -> None:
    """Require every authorized runtime file to exist byte-for-byte in the revision."""
    root = Path(manifest["code_root"])
    revision = manifest["source_revision"]
    for name, expected_digest in manifest["source_closure"].items():
        try:
            result = subprocess.run(
                ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root),
                 "show", f"{revision}:{name}"],
                check=True, capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise AuthorizationError(
                f"authorized source is not published in the revision: {name}") from error
        if hashlib.sha256(result.stdout).hexdigest() != expected_digest:
            raise AuthorizationError(
                f"authorized source bytes differ from the published revision: {name}")


def _bound_jobs(manifest: dict) -> list[dict]:
    return [
        {name: job[name] for name in _BOUND_JOB_KEYS}
        for job in manifest["jobs"]
    ]


def authorization_identity(manifest: dict, output_root: str | Path) -> dict:
    """Return all non-secret manifest fields an external approval must repeat."""
    if manifest.get("schema_version") != "batch_runner_v2b2a_candidate_v1":
        raise AuthorizationError("only a B2A candidate manifest can be authorized")
    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "source_revision": manifest["source_revision"],
        "source_closure_sha256": canonical_sha256(manifest["source_closure"]),
        "batch_id": manifest["batch_id"],
        "provider": manifest["provider"],
        "model": manifest["model"],
        "credential_scope": manifest["credential_scope"],
        "resource_limits": manifest["resource_limits"],
        "retry_policy": manifest["retry_policy"],
        "jobs": _bound_jobs(manifest),
        "output_root": str(Path(output_root).resolve()),
    }


def validate_authorization(
    manifest: dict,
    authorization_path: str | Path,
    expected_authorization_sha256: str,
    output_root: str | Path,
) -> dict:
    """Validate an approval whose exact file digest was pinned out of band.

    A self-declared digest in the approval would not prove authority. The caller
    must supply the expected SHA-256 from the separate human approval channel.
    """
    if not isinstance(expected_authorization_sha256, str) or not \
            _HASH.fullmatch(expected_authorization_sha256):
        raise AuthorizationError("an out-of-band authorization SHA-256 is required")
    path = Path(authorization_path)
    if path.is_symlink():
        raise AuthorizationError("authorization must not be a symbolic link")
    try:
        path = path.resolve(strict=True)
    except OSError as error:
        raise AuthorizationError("authorization file is missing") from error
    if not path.is_file() or sha256_file(path) != expected_authorization_sha256:
        raise AuthorizationError("authorization file fingerprint mismatch")
    try:
        approval = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuthorizationError("authorization is not valid JSON") from error
    if not isinstance(approval, dict) or set(approval) != _AUTHORIZATION_KEYS:
        raise AuthorizationError("invalid authorization fields")
    if approval["schema_version"] != "batch_runner_v2b2a_authorization_v1":
        raise AuthorizationError("unsupported authorization schema")
    if not isinstance(approval["authorization_id"], str) or not \
            _LABEL.fullmatch(approval["authorization_id"]):
        raise AuthorizationError("authorization_id must be a safe label")
    if approval["execution_scope"] not in {"offline_integration", "production"}:
        raise AuthorizationError("unsupported authorization execution scope")
    if approval["approved"] is not True:
        raise AuthorizationError("batch authorization is not approved")
    if not isinstance(approval["approved_by"], str) or not approval["approved_by"].strip() \
            or not isinstance(approval["approved_at"], str) or not approval["approved_at"].strip():
        raise AuthorizationError("authorization approver and timestamp are required")
    expected = authorization_identity(manifest, output_root)
    for name, value in expected.items():
        if approval.get(name) != value:
            raise AuthorizationError(f"authorization does not bind manifest field: {name}")
    revision = current_source_revision(manifest["code_root"])
    if revision != manifest["source_revision"]:
        raise AuthorizationError("executing source revision differs from authorization")
    return {
        **approval,
        "authorization_path": str(path),
        "authorization_sha256": expected_authorization_sha256,
    }


def _claim_path(authorization: dict) -> Path:
    path = Path(authorization["authorization_path"])
    return path.with_name(path.name + ".claim.json")


def authorization_claim_identity(authorization: dict) -> dict:
    return {
        "schema_version": "batch_runner_v2b2a_authorization_claim_v1",
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": authorization["authorization_sha256"],
        "manifest_sha256": authorization["manifest_sha256"],
        "batch_id": authorization["batch_id"],
        "output_root": authorization["output_root"],
    }


def claim_authorization(authorization: dict) -> Path:
    """Claim an approval once; an existing claim is valid only for restart."""
    path = _claim_path(authorization)
    if path.is_symlink():
        raise AuthorizationError("authorization claim must not be a symbolic link")
    expected = authorization_claim_identity(authorization)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AuthorizationError("authorization claim is unreadable") from error
        if existing != expected:
            raise AuthorizationError("authorization claim identity mismatch")
        status = Path(authorization["output_root"]) / authorization["batch_id"] / "status.json"
        if not status.is_file():
            raise AuthorizationError(
                "authorization was claimed but no restartable batch state exists")
        return path
    try:
        data = (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def validate_authorization_claim(authorization: dict) -> Path:
    path = _claim_path(authorization)
    if path.is_symlink() or not path.is_file():
        raise AuthorizationError("authorization has not been claimed by the scheduler")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuthorizationError("authorization claim is unreadable") from error
    if existing != authorization_claim_identity(authorization):
        raise AuthorizationError("authorization claim identity mismatch")
    return path
