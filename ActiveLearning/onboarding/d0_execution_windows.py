"""Versioned, append-only execution windows for reviewed D0 batch attempts.

This module contains no simulator or circuit-specific evaluation logic. An
extension is an additional operator approval, never a rewrite of a v1 budget.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ActiveLearning.onboarding.errors import AutoConfigError
from ActiveLearning.sc_sample_hold_generic import sha256_file


SCHEMA = "independent_d0_execution_window_extension_v1"
WINDOW_SCHEMA = "independent_d0_execution_window_activation_v1"
MAX_ACTIVATION_VALIDITY_S = 7 * 24 * 3600


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _prefix(path: Path, count: int | None) -> tuple[bytes, dict]:
    raw = path.read_bytes()
    if count is None:
        count = len(raw)
    if type(count) is not int or count < 1 or len(raw) < count:
        raise AutoConfigError("D0 extension evidence prefix is missing or truncated")
    prefix = raw[:count]
    if not prefix.endswith(b"\n"):
        raise AutoConfigError("D0 extension evidence prefix ends inside a record")
    return prefix, {"bytes": count, "sha256": hashlib.sha256(prefix).hexdigest()}


def _jsonl(raw: bytes, label: str) -> list[dict]:
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (UnicodeError, ValueError) as error:
        raise AutoConfigError(f"D0 extension {label} is unreadable") from error
    if not all(isinstance(row, dict) for row in rows):
        raise AutoConfigError(f"D0 extension {label} contains non-object rows")
    return rows


def extension_binding(*, circuit_id: str, experiment_id: str,
                      approval_path: Path, original_approval: Mapping[str, Any],
                      source_hashes: Mapping[str, str], identity_path: Path,
                      ledger_path: Path, checkpoint_path: Path, review_path: Path,
                      candidate_plan_path: Path, candidate_plan_sha256: str,
                      budget: Mapping[str, Any], ledger_bytes: int | None = None,
                      checkpoint_bytes: int | None = None) -> dict:
    """Bind immutable pre-extension prefixes, even after later appends."""
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        ledger_raw, ledger_fingerprint = _prefix(ledger_path, ledger_bytes)
        checkpoint_raw, checkpoint_fingerprint = _prefix(checkpoint_path, checkpoint_bytes)
        events = _jsonl(ledger_raw, "ledger")
        records = _jsonl(checkpoint_raw, "checkpoint")
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise AutoConfigError("D0 extension baseline evidence is unavailable") from error
    approval_sha = sha256_file(approval_path)
    if (identity.get("approval_sha256") != approval_sha
            or identity.get("experiment_id") != experiment_id
            or identity.get("circuit_id") != circuit_id
            or type(identity.get("started_unix_s")) not in (int, float)
            or not math.isfinite(identity["started_unix_s"])):
        raise AutoConfigError("D0 extension original experiment identity mismatch")
    reserved = [row for row in events if row.get("event") == "native_attempt_reserved"]
    finished = [row for row in events if row.get("event") == "native_attempt_finished"]
    persisted = [row for row in events if row.get("event") == "d0_record_persisted"]
    completed_ids = [record.get("sample_id") for record in records]
    if (not completed_ids or len(completed_ids) != len(set(completed_ids))
            or any(row.get("event") not in {
                "native_attempt_reserved", "native_attempt_finished", "d0_record_persisted"
            } for row in events)
            or len(reserved) != len(finished) or len(finished) != len(persisted)
            or len(persisted) != len(records)
            or [row.get("attempt") for row in reserved] != list(range(1, len(reserved) + 1))
            or {row.get("vector_sha256") for row in reserved}
            != {row.get("vector_sha256") for row in finished}
            or any(row.get("outcome") != "complete" for row in finished)
            or [row.get("sample_id") for row in persisted] != completed_ids
            or len(reserved) >= budget["physical_attempts_max"]):
        raise AutoConfigError("D0 extension baseline is incomplete or has uncertain attempts")
    expected_review = {
        "schema": "sc_independent_d0_pilot_review_v1",
        "decision": "continue",
        "circuit_id": circuit_id,
        "experiment_id": experiment_id,
        "batch_approval_sha256": approval_sha,
        "candidate_plan_sha256": candidate_plan_sha256,
        "pilot_sample_ids": completed_ids[:1],
        "pilot_records_sha256": _sha(records[:1]),
    }
    if (set(review) != set(expected_review) | {"reviewed_by", "reviewed_at"}
            or any(review.get(key) != value for key, value in expected_review.items())
            or not isinstance(review.get("reviewed_by"), str)
            or not review["reviewed_by"].strip()):
        raise AutoConfigError("D0 extension continuation review mismatch")
    try:
        reviewed_at = datetime.fromisoformat(review["reviewed_at"])
    except (TypeError, ValueError) as error:
        raise AutoConfigError("D0 extension continuation review timestamp invalid") from error
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise AutoConfigError("D0 extension continuation review needs timezone")
    plan = json.loads(candidate_plan_path.read_text(encoding="utf-8"))
    candidates = plan.get("candidates")
    if (not isinstance(candidates, list)
            or _sha(candidates) != candidate_plan_sha256
            or completed_ids != [row.get("sample_id") for row in candidates[:len(records)]]
            or len(candidates) < budget["physical_attempts_max"]):
        raise AutoConfigError("D0 extension frozen candidate plan mismatch")
    return {
        "schema": SCHEMA,
        "circuit_id": circuit_id,
        "experiment_id": experiment_id,
        "original_approval_sha256": approval_sha,
        "original_source_closure_sha256": _sha(original_approval["source_hashes"]),
        "new_source_closure_sha256": _sha(source_hashes),
        "new_source_hashes": dict(source_hashes),
        "config_sha256": original_approval["config_sha256"],
        "candidate_plan_sha256": candidate_plan_sha256,
        "candidate_plan_file_sha256": sha256_file(candidate_plan_path),
        "identity_sha256": sha256_file(identity_path),
        "ledger_snapshot": ledger_fingerprint,
        "checkpoint_snapshot": checkpoint_fingerprint,
        "continuation_review_sha256": sha256_file(review_path),
        "completed_candidate_ids": completed_ids,
        "physical_attempts_consumed": len(reserved),
        "physical_attempts_remaining": budget["physical_attempts_max"] - len(reserved),
        "previous_deadline_unix_s": identity["started_unix_s"] + budget["overall_timeout_s"],
        "execution_window_policy": {
            "continuation_window_s": budget["overall_timeout_s"],
            "single_attempt_timeout_s": budget["single_attempt_timeout_s"],
            "automatic_retries": 0,
            "additional_physical_attempts_max": budget["physical_attempts_max"] - len(reserved),
        },
    }


def validate_extension(path: Path, *, expected: Mapping[str, Any], now: float) -> dict:
    """Validate an operator-issued file; never create or amend that file."""
    actual = json.loads(path.read_text(encoding="utf-8"))
    operator_fields = {"approved_by", "approved_at", "authorization_evidence",
                       "activation_not_after"}
    if (set(actual) != set(expected) | operator_fields
            or any(actual.get(key) != value for key, value in expected.items())):
        raise AutoConfigError("D0 execution-window extension binding mismatch")
    if (not isinstance(actual["approved_by"], str) or not actual["approved_by"].strip()
            or not isinstance(actual["authorization_evidence"], str)
            or not actual["authorization_evidence"].strip()):
        raise AutoConfigError("D0 execution-window extension needs explicit operator authorization")
    try:
        approved = datetime.fromisoformat(actual["approved_at"])
        expires = datetime.fromisoformat(actual["activation_not_after"])
    except (TypeError, ValueError) as error:
        raise AutoConfigError("D0 execution-window extension timestamp invalid") from error
    if (approved.tzinfo is None or approved.utcoffset() is None
            or expires.tzinfo is None or expires.utcoffset() is None
            or not 0 < expires.timestamp() - approved.timestamp() <= MAX_ACTIVATION_VALIDITY_S
            or approved.timestamp() > now + 60):
        raise AutoConfigError("D0 execution-window extension activation validity invalid")
    return actual


def read_window(window_path: Path, ledger_path: Path, *, extension_sha256: str,
                continuation_window_s: float) -> dict | None:
    """The first activation is durable and cannot be reset by restarting."""
    events = _jsonl(ledger_path.read_bytes(), "ledger")
    activations = [row for row in events if row.get("event") == "execution_window_activated"]
    if not window_path.exists():
        if activations:
            raise AutoConfigError("D0 execution window activation file missing")
        return None
    window = json.loads(window_path.read_text(encoding="utf-8"))
    if (window.get("schema") != WINDOW_SCHEMA
            or window.get("extension_sha256") != extension_sha256
            or type(window.get("started_unix_s")) not in (int, float)
            or not math.isfinite(window["started_unix_s"])
            or window.get("deadline_unix_s") != window["started_unix_s"] + continuation_window_s
            or len(activations) != 1
            or any(activations[0].get(key) != window.get(key) for key in
                   ("extension_sha256", "started_unix_s", "deadline_unix_s"))):
        raise AutoConfigError("D0 execution-window activation evidence mismatch")
    return window
