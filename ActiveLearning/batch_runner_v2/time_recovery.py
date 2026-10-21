"""Opt-in monotonic, job-scoped Gemini 429 recovery.

The caller supplies the transport; this module never constructs a provider client.
"""
from __future__ import annotations

import hashlib
import math
import uuid

from .gemini_recovery import (
    JobDeadlineExpired, JobPhysicalAttemptLimitError, PhysicalAttemptLimitError,
    ProviderQuotaError, ProviderTerminalError, RetryExhaustedError,
    _get, _retry_after, _usage,
)
from .storage import atomic_json, batch_lock, read_json


def call_with_job_deadline(recovery, payload, send):
    logical_id = uuid.uuid4().hex
    request_hash = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
    start = recovery.clock()
    attempts = non429_failures = rate_limits = 0
    ordinary_deadline = None
    last_rate_limit = False
    recovery._record({"event": "logical_started", "logical_id": logical_id,
                      "request_sha256": request_hash})

    def finish(status, **fields):
        recovery._record({"event": "logical_finished", "logical_id": logical_id,
                          "status": status, "attempts": attempts, **fields})

    while True:
        now = recovery.clock()
        deadline = min(recovery.job_deadline, ordinary_deadline or recovery.job_deadline)
        if now >= deadline:
            if last_rate_limit and deadline == recovery.job_deadline:
                finish("job_deadline", last_failure_kind="rate_limit")
                raise JobDeadlineExpired("429 wait reached job deadline")
            finish("retry_exhausted", last_failure_kind="non429")
            raise RetryExhaustedError("request deadline reached outside safe 429 wait")
        if recovery.cooldown.exists():
            state = read_json(recovery.cooldown)
            if state.get("scope") == recovery.scope:
                remaining_cooldown = float(state.get("until", 0)) - now
                if remaining_cooldown > 0:
                    if remaining_cooldown >= deadline - now:
                        if last_rate_limit and deadline == recovery.job_deadline:
                            finish("job_deadline", last_failure_kind="rate_limit")
                            raise JobDeadlineExpired("429 cooldown exceeds job deadline")
                        finish("retry_exhausted", last_failure_kind="shared_cooldown")
                        raise RetryExhaustedError("shared cooldown exceeds request deadline")
                    recovery._record({"event": "wait_started", "logical_id": logical_id,
                                      "seconds": remaining_cooldown,
                                      "source": "shared_cooldown"})
                    before = recovery.clock()
                    recovery.sleep(remaining_cooldown)
                    recovery._record({"event": "wait_finished", "logical_id": logical_id,
                                      "seconds": recovery.clock() - before,
                                      "source": "shared_cooldown"})
                    continue
        remaining = deadline - recovery.clock()
        if remaining <= 0:
            continue
        with batch_lock(recovery.root / ".api_usage.lock"):
            try:
                recovery._admit()
            except JobPhysicalAttemptLimitError:
                finish("job_budget", last_failure_kind="per_job_cap")
                raise
            except PhysicalAttemptLimitError:
                finish("batch_budget", last_failure_kind="batch_cap")
                raise
            # Lock acquisition and journal admission can consume deadline time.
            # Do not claim a physical request if no time remains to send it.
            if recovery.clock() >= deadline:
                continue
            attempts += 1
            recovery._record({"event": "attempt_started", "logical_id": logical_id,
                              "attempt": attempts, "request_sha256": request_hash})
        timeout_seconds = min(recovery.timeout, deadline - recovery.clock())
        if timeout_seconds <= 0:
            # The durable claim remains unclosed, forcing an uncertain stop.
            # No transport call is made after the deadline.
            raise RuntimeError("request deadline expired after physical attempt claim")
        sent_at = recovery.clock()
        try:
            response = send(payload, timeout_seconds)
        except Exception as error:
            failed_at = recovery.clock()
            kind = recovery.classify_failure(error, strict_hard_quota=True)
            status = _get(error, "status_code")
            recovery._record({"event": "attempt_finished", "logical_id": logical_id,
                              "attempt": attempts, "outcome": kind,
                              "failure_kind": kind, "http_status": status,
                              "latency_seconds": recovery.clock() - sent_at,
                              "usage_unknown": True, "input_tokens": None,
                              "output_tokens": None, "total_tokens": None})
            if kind == "quota":
                atomic_json(recovery.provider_block,
                            {"scope": recovery.scope, "reason": "quota"})
                finish("quota_exhausted", last_failure_kind=kind)
                raise ProviderQuotaError("provider quota exhausted") from error
            if kind == "terminal":
                finish("terminal", last_failure_kind=kind)
                raise ProviderTerminalError(f"nonretryable provider status {status}") from error
            last_rate_limit = kind == "rate_limit"
            if last_rate_limit:
                rate_limits += 1
                header_wait = _retry_after(error, recovery.wall_time())
                random_unit = recovery.jitter()
                if type(random_unit) not in {int, float} or not math.isfinite(random_unit):
                    raise ValueError("invalid jitter source")
                delay = (header_wait if header_wait is not None else min(
                    recovery.rate_limit_initial_backoff * 2 ** min(rate_limits - 1, 20),
                    recovery.rate_limit_max_backoff) * (
                        0.8 + 0.4 * max(0.0, min(1.0, random_unit))))
                atomic_json(recovery.cooldown, {"scope": recovery.scope,
                                                "until": recovery.clock() + delay})
            else:
                non429_failures += 1
                if ordinary_deadline is None:
                    ordinary_deadline = failed_at + recovery.max_elapsed
                delay = min(2 ** min(non429_failures - 1, 20), 30)
            deadline = min(recovery.job_deadline, ordinary_deadline or recovery.job_deadline)
            if non429_failures >= recovery.max_attempts:
                finish("retry_exhausted", last_failure_kind=kind)
                raise RetryExhaustedError("non-429 attempt bound exhausted") from error
            if recovery.clock() + delay >= deadline:
                if last_rate_limit and deadline == recovery.job_deadline:
                    finish("job_deadline", last_failure_kind=kind)
                    raise JobDeadlineExpired("429 wait exceeds job deadline") from error
                finish("retry_exhausted", last_failure_kind=kind)
                raise RetryExhaustedError("non-429 recovery deadline exhausted") from error
            recovery._record({"event": "wait_started", "logical_id": logical_id,
                              "seconds": delay,
                              "source": "retry_after" if last_rate_limit and header_wait
                              else "backoff"})
            before = recovery.clock()
            recovery.sleep(delay)
            recovery._record({"event": "wait_finished", "logical_id": logical_id,
                              "seconds": recovery.clock() - before})
            continue
        counts = _usage(response)
        recovery._record({"event": "attempt_finished", "logical_id": logical_id,
                          "attempt": attempts, "outcome": "success",
                          "latency_seconds": recovery.clock() - sent_at,
                          "usage_unknown": any(v is None for v in counts.values()),
                          **counts})
        if recovery.clock() > recovery.job_deadline:
            raise RuntimeError("provider response completed after job deadline")
        finish("success", wall_seconds=recovery.clock() - start)
        return response
