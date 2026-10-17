"""Deterministic mocked transport tests; zero provider SDK construction."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ActiveLearning.batch_runner_v2.gemini_recovery import (
    GeminiRecovery, PhysicalAttemptLimitError, ProviderQuotaError,
    ProviderTerminalError, RetryExhaustedError,
)
from ActiveLearning.batch_runner_v2.storage import read_jsonl_strict


class ProviderError(Exception):
    def __init__(self, status, headers=None, body=None):
        super().__init__("mock provider error")
        self.status_code = status
        self.headers = headers or {}
        self.body = body or {}


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class GeminiRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = FakeClock()

    def client(self, **kwargs):
        return GeminiRecovery(
            self.root, provider="gemini", model="model-a",
            credential_scope="offline-key", max_physical_attempts=kwargs.pop("cap", 20),
            request_timeout=kwargs.pop("timeout", 120),
            max_attempts=kwargs.pop("max_attempts", 8),
            max_elapsed=kwargs.pop("max_elapsed", 1800),
            clock=self.clock.monotonic, sleep=self.clock.sleep,
            wall_time=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
            jitter=lambda: 0.5, **kwargs,
        )

    def events(self, event):
        return [row for row in read_jsonl_strict(self.root / "api_usage.jsonl")
                if row.get("event") == event]

    @staticmethod
    def success():
        return {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}

    def test_429_recovers_same_logical_request(self):
        calls = 0

        def send(payload, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderError(429)
            return self.success()

        self.client().call({"prompt": "safe"}, send)
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.events("logical_started")), 1)
        self.assertEqual(len(self.events("attempt_started")), 2)
        self.assertEqual(self.events("wait_finished")[0]["seconds"], 1.0)

    def test_retry_after_respected(self):
        calls = 0

        def send(payload, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderError(429, headers={"Retry-After": "11"})
            return self.success()

        self.client().call("prompt", send)
        self.assertEqual(self.clock.waits, [11.0])
        self.assertIn("retry_after", {row["source"] for row in self.events("wait_started")})

    def test_repeated_timeout_is_bounded_and_unknown(self):
        client = self.client(max_attempts=3)
        with self.assertRaises(RetryExhaustedError):
            client.call("prompt", lambda *_: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(len(self.events("attempt_started")), 3)
        self.assertTrue(all(row["usage_unknown"] and row["total_tokens"] is None
                            for row in self.events("attempt_finished")))

    def test_503_recovers(self):
        calls = 0

        def send(payload, timeout):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ProviderError(503)
            return self.success()

        self.client().call("prompt", send)
        self.assertEqual(calls, 3)
        self.assertEqual(len(self.events("attempt_started")), 3)

    def test_bad_key_is_terminal(self):
        with self.assertRaises(ProviderTerminalError):
            self.client().call("prompt", lambda *_: (_ for _ in ()).throw(ProviderError(401)))
        self.assertEqual(len(self.events("attempt_started")), 1)
        self.assertFalse(self.clock.waits)

    def test_daily_quota_is_not_retried(self):
        with self.assertRaises(ProviderQuotaError):
            self.client().call("prompt", lambda *_: (_ for _ in ()).throw(
                ProviderError(429, body={"error": {"message": "daily quota exhausted"}})))
        self.assertEqual(len(self.events("attempt_started")), 1)
        with self.assertRaises(ProviderQuotaError):
            self.client().call("next circuit", lambda *_: self.fail("must not rotate quota"))
        self.assertEqual(len(self.events("attempt_started")), 1)

    def test_shared_cooldown_before_next_logical_request(self):
        with self.assertRaises(RetryExhaustedError):
            self.client(max_attempts=1).call("a", lambda *_: (_ for _ in ()).throw(
                ProviderError(429, headers={"Retry-After": "17"})))
        called_at = []
        self.client().call("b", lambda *_: (called_at.append(self.clock.now), self.success())[1])
        self.assertEqual(called_at, [17.0])
        self.assertIn(17.0, self.clock.waits)

    def test_physical_attempt_cap_includes_retries(self):
        calls = 0

        def send(payload, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderError(503)
            return self.success()

        self.client(cap=2).call("first", send)
        with self.assertRaises(PhysicalAttemptLimitError):
            self.client(cap=2).call("second", send)
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.events("attempt_started")), 2)

    def test_missing_success_usage_remains_unknown(self):
        self.client().call("prompt", lambda *_: {"text": "ok"})
        finish = self.events("attempt_finished")[0]
        self.assertTrue(finish["usage_unknown"])
        self.assertIsNone(finish["total_tokens"])

    def test_request_timeout_respects_deadline(self):
        timeouts = []

        def send(payload, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise ProviderError(503)
            return self.success()

        self.client(timeout=120, max_elapsed=1.5).call("prompt", send)
        self.assertEqual(timeouts, [1.5, 0.5])

    def test_journal_does_not_contain_prompt(self):
        self.client().call("SECRET_PROMPT_CANARY", lambda *_: self.success())
        self.assertNotIn("SECRET_PROMPT_CANARY", (self.root / "api_usage.jsonl").read_text())

    def test_corrupt_journal_fails_closed(self):
        (self.root / "api_usage.jsonl").write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.client().call("prompt", lambda *_: self.fail("must not send"))

    def test_provider_policy_cannot_be_relaxed_on_restart(self):
        self.client(cap=2)
        with self.assertRaises(ValueError):
            self.client(cap=20)

    def test_report_retains_partial_usage(self):
        from ActiveLearning.batch_runner_v2.runner import report_batch
        from ActiveLearning.batch_runner_v2.storage import atomic_json

        batch = self.root / "offline_report"
        batch.mkdir()
        atomic_json(batch / "status.json", {"batch_id": "offline_report", "jobs": {}})
        self.client().call("prompt", lambda *_: {"text": "no usage"})
        # The adapter journal is at the same batch root in this focused test.
        from shutil import copyfile
        copyfile(self.root / "api_usage.jsonl", batch / "api_usage.jsonl")
        usage = report_batch("offline_report", self.root)["api_usage"]
        self.assertTrue(usage["usage_unknown"])
        self.assertIsNone(usage["total_tokens"])
        self.assertEqual(usage["physical_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
