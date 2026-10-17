"""Injectable, offline Gemini recovery policy; never constructs an API client."""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from .storage import append_jsonl, atomic_json, batch_lock, read_json, read_jsonl_strict


class ProviderTerminalError(RuntimeError):
    pass


class ProviderQuotaError(ProviderTerminalError):
    pass


class RetryExhaustedError(RuntimeError):
    pass


class PhysicalAttemptLimitError(RuntimeError):
    pass


def _get(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _retry_after(error: Exception, now: datetime) -> float | None:
    headers = _get(_get(error, "response"), "headers") or _get(error, "headers") or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    detail = _get(_get(error, "body"), "error") or _get(error, "body")
    if raw is None:
        raw = _get(detail, "retryDelay")
    if raw is None:
        for item in _get(detail, "details", []) or []:
            raw = _get(item, "retryDelay")
            if raw is not None:
                break
    try:
        if isinstance(raw, str):
            try:
                seconds = float(raw.strip().removesuffix("s"))
            except ValueError:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = (date - now).total_seconds()
        elif type(raw) in {int, float}:
            seconds = float(raw)
        else:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _failure_kind(error: Exception) -> str:
    status = _get(error, "status_code")
    detail = _get(_get(error, "body"), "error") or _get(error, "body")
    code = str(_get(detail, "code", _get(error, "code", "")) or "").casefold()
    message = str(_get(detail, "message", "") or "").casefold()
    quota_ids = " ".join(str(_get(row, "quotaId", "")) for row in _get(detail, "details", []) or []).casefold()
    if status == 429:
        if any(word in f"{code} {message} {quota_ids}" for word in (
            "perday", "per_day", "daily quota", "per day", "billing_hard_limit",
            "insufficient_quota", "quota_exhausted"
        )):
            return "quota"
        return "rate_limit"
    if status in {401, 403, 400, 404, 422}:
        return "terminal"
    if status in {500, 502, 503, 504}:
        return "service_unavailable"
    if isinstance(error, (TimeoutError, ConnectionError)) or type(error).__name__ in {
        "ReadTimeout", "ConnectTimeout", "ConnectError", "APITimeoutError",
        "APIConnectionError", "RemoteProtocolError",
    }:
        return "network_timeout"
    return "terminal"


def _usage(response: Any) -> dict[str, int | None]:
    value = _get(response, "usage") or _get(response, "usage_metadata")
    result = {}
    for key, names in {
        "input_tokens": ("prompt_tokens", "prompt_token_count", "input_tokens"),
        "output_tokens": ("completion_tokens", "candidates_token_count", "output_tokens"),
        "total_tokens": ("total_tokens", "total_token_count"),
    }.items():
        found = next((_get(value, name) for name in names if type(_get(value, name)) is int), None)
        result[key] = found if found is not None and found >= 0 else None
    return result


class GeminiRecovery:
    """One physical-attempt ledger and cooldown for one offline batch scope.

    `send(payload, timeout_seconds)` is injected. No SDK construction, network
    access, or production execution is available in this module.
    """

    def __init__(
        self, root: Path, *, provider: str, model: str, credential_scope: str,
        max_physical_attempts: int, request_timeout: float = 120,
        max_attempts: int = 8, max_elapsed: float = 1800,
        job_id: str | None = None, attempt_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_time: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        jitter: Callable[[], float] = random.random,
    ):
        if provider != "gemini" or not model or not credential_scope:
            raise ValueError("a named Gemini provider scope is required")
        if (type(max_physical_attempts) is not int or max_physical_attempts < 1
                or type(max_attempts) is not int or max_attempts < 1
                or any(type(v) not in {int, float} or not math.isfinite(v) or v <= 0
                       for v in (request_timeout, max_elapsed))):
            raise ValueError("retry limits must be positive")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.scope = f"{provider}/{model}/{credential_scope}"
        self.job_id, self.attempt_id = job_id, attempt_id
        self.cap = int(max_physical_attempts)
        self.timeout = float(request_timeout)
        self.max_attempts = int(max_attempts)
        self.max_elapsed = float(max_elapsed)
        self.clock, self.sleep, self.wall_time, self.jitter = clock, sleep, wall_time, jitter
        self.journal = self.root / "api_usage.jsonl"
        self.cooldown = self.root / "cooldown.json"
        self.provider_block = self.root / "provider_block.json"
        policy_path = self.root / "api_policy.json"
        policy = {"scope": self.scope, "max_physical_attempts": self.cap}
        if policy_path.exists():
            if read_json(policy_path) != policy:
                raise ValueError("provider scope or physical cap changed for this batch")
        else:
            atomic_json(policy_path, policy)

    def _record(self, event: dict) -> None:
        append_jsonl(self.journal, {"scope": self.scope, "job_id": self.job_id,
                                    "attempt_id": self.attempt_id, **event})

    def _admit(self) -> None:
        if self.provider_block.exists():
            block = read_json(self.provider_block)
            if block.get("scope") == self.scope:
                if block.get("reason") == "quota":
                    raise ProviderQuotaError("provider quota previously exhausted")
                raise PhysicalAttemptLimitError("provider scope is blocked")
        rows = read_jsonl_strict(self.journal)
        started = sum(row.get("event") == "attempt_started" for row in rows)
        if started >= self.cap:
            atomic_json(self.provider_block, {"scope": self.scope, "reason": "attempt_cap"})
            raise PhysicalAttemptLimitError("physical attempt cap reached")

    def _cooldown_wait(self, deadline: float) -> None:
        if not self.cooldown.exists():
            return
        state = read_json(self.cooldown)
        if state.get("scope") != self.scope:
            return
        remaining = float(state.get("until", 0)) - self.clock()
        if remaining > 0:
            if self.clock() + remaining >= deadline:
                raise RetryExhaustedError("shared cooldown exceeds request deadline")
            self._record({"event": "wait_started", "seconds": remaining, "source": "shared_cooldown"})
            start = self.clock()
            self.sleep(remaining)
            self._record({"event": "wait_finished", "seconds": self.clock() - start,
                          "source": "shared_cooldown"})

    def call(self, payload: Any, send: Callable[[Any, float], Any]) -> Any:
        logical_id = uuid.uuid4().hex
        request_hash = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
        start, attempts = self.clock(), 0
        deadline = start + self.max_elapsed
        self._record({"event": "logical_started", "logical_id": logical_id,
                      "request_sha256": request_hash})
        while True:
            self._cooldown_wait(deadline)
            remaining = deadline - self.clock()
            if remaining <= 0 or attempts >= self.max_attempts:
                self._record({"event": "logical_finished", "logical_id": logical_id,
                              "status": "retry_exhausted", "attempts": attempts})
                raise RetryExhaustedError("Gemini retry bounds exhausted")
            with batch_lock(self.root / ".api_usage.lock"):
                self._admit()
                attempts += 1
                self._record({"event": "attempt_started", "logical_id": logical_id,
                              "attempt": attempts, "request_sha256": request_hash})
            sent_at = self.clock()
            try:
                response = send(payload, min(self.timeout, remaining))
            except Exception as error:
                kind = _failure_kind(error)
                status = _get(error, "status_code")
                self._record({"event": "attempt_finished", "logical_id": logical_id,
                              "attempt": attempts, "outcome": kind, "http_status": status,
                              "latency_seconds": self.clock() - sent_at,
                              "usage_unknown": True, "input_tokens": None,
                              "output_tokens": None, "total_tokens": None})
                if kind == "quota":
                    atomic_json(self.provider_block, {"scope": self.scope, "reason": "quota"})
                    self._record({"event": "logical_finished", "logical_id": logical_id,
                                  "status": "quota_exhausted", "attempts": attempts})
                    raise ProviderQuotaError("provider quota exhausted") from error
                if kind == "terminal":
                    self._record({"event": "logical_finished", "logical_id": logical_id,
                                  "status": "terminal", "attempts": attempts})
                    raise ProviderTerminalError(f"nonretryable provider status {status}") from error
                wait = (_retry_after(error, self.wall_time()) if kind == "rate_limit" else None)
                random_unit = self.jitter()
                if type(random_unit) not in {int, float} or not math.isfinite(random_unit):
                    raise ValueError("invalid jitter source")
                delay = wait if wait is not None else min(2 ** (attempts - 1), 30) * (
                    0.8 + 0.4 * max(0.0, min(1.0, random_unit)))
                if kind == "rate_limit":
                    atomic_json(self.cooldown, {"scope": self.scope,
                                                "until": self.clock() + delay})
                if attempts >= self.max_attempts or self.clock() + delay >= deadline:
                    self._record({"event": "logical_finished", "logical_id": logical_id,
                                  "status": "retry_exhausted", "attempts": attempts,
                                  "last_failure_kind": kind})
                    raise RetryExhaustedError(f"Gemini {kind} retry bounds exhausted") from error
                self._record({"event": "wait_started", "logical_id": logical_id,
                              "seconds": delay, "source": "retry_after" if wait else "backoff"})
                wait_started = self.clock()
                self.sleep(delay)
                self._record({"event": "wait_finished", "logical_id": logical_id,
                              "seconds": self.clock() - wait_started})
                continue
            counts = _usage(response)
            self._record({"event": "attempt_finished", "logical_id": logical_id,
                          "attempt": attempts, "outcome": "success",
                          "latency_seconds": self.clock() - sent_at,
                          "usage_unknown": any(v is None for v in counts.values()), **counts})
            self._record({"event": "logical_finished", "logical_id": logical_id,
                          "status": "success", "attempts": attempts,
                          "wall_seconds": self.clock() - start})
            return response
