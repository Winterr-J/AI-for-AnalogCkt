"""Opt-in time-driven recovery and safe scheduling; all workers/transports are fake."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ActiveLearning.batch_runner_v2.al_process import _verified_safe_stop
from ActiveLearning.batch_runner_v2.gemini_recovery import (
    GeminiRecovery, JobDeadlineExpired, JobPhysicalAttemptLimitError,
    PhysicalAttemptLimitError, ProviderQuotaError, ProviderTerminalError,
    RetryExhaustedError,
)
from ActiveLearning.batch_runner_v2.llm_bridge import (
    GeminiALClient, build_gemini_chat_transport,
)
from ActiveLearning.batch_runner_v2.runner import _run_batch
from ActiveLearning.batch_runner_v2.storage import (
    append_jsonl, atomic_json, read_jsonl_strict, sha256_file,
)


class ProviderError(Exception):
    def __init__(self, status, headers=None, body=None):
        super().__init__("synthetic provider failure")
        self.status_code = status
        self.headers = headers or {}
        self.body = body or {}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class TimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def client(self, *, job="job_a", cap=40, job_cap=30, deadline=4800,
               max_elapsed=1800, max_attempts=5):
        return GeminiRecovery(
            self.root, provider="gemini", model="offline-model",
            credential_scope="offline-only", max_physical_attempts=cap,
            job_id=job, attempt_id="a" * 32,
            rate_limit_mode="job_deadline", job_deadline_monotonic=deadline,
            per_job_physical_attempt_cap=job_cap,
            request_timeout=120, max_attempts=max_attempts,
            max_elapsed=max_elapsed, clock=self.clock.monotonic,
            sleep=self.clock.sleep, jitter=lambda: 0.5,
            wall_time=lambda: datetime(2026, 10, 19, tzinfo=timezone.utc),
        )

    def rows(self, event):
        return [row for row in read_jsonl_strict(self.root / "api_usage.jsonl")
                if row.get("event") == event]

    @staticmethod
    def success():
        return {"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}

    def test_twelve_429s_ignore_five_attempt_and_logical_elapsed_limits(self):
        calls = 0

        def send(_payload, timeout):
            nonlocal calls
            calls += 1
            self.assertLessEqual(timeout, 120)
            if calls <= 12:
                raise ProviderError(429, {"Retry-After": "1"})
            return self.success()

        self.client(max_elapsed=3).call("secret prompt", send)
        self.assertEqual(calls, 13)
        self.assertEqual(self.clock.waits, [1.0] * 12)
        self.assertEqual(len(self.rows("attempt_started")), 13)
        self.assertNotIn("secret prompt", (self.root / "api_usage.jsonl").read_text())

    def test_missing_retry_after_uses_30_to_300_exponential_backoff(self):
        calls = 0

        def send(*_):
            nonlocal calls
            calls += 1
            if calls <= 6:
                raise ProviderError(429)
            return self.success()

        self.client().call("x", send)
        self.assertEqual(self.clock.waits, [30, 60, 120, 240, 300, 300])

    def test_retry_after_exceeding_remaining_deadline_is_safe_stop(self):
        with self.assertRaises(JobDeadlineExpired):
            self.client(deadline=10).call(
                "x", lambda *_: (_ for _ in ()).throw(
                    ProviderError(429, {"Retry-After": "11"})))
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(self.rows("logical_finished")[-1]["status"], "job_deadline")
        self.assertEqual(len(self.rows("attempt_started")), len(self.rows("attempt_finished")))

    def test_4800_deadline_and_request_timeout_are_shared_across_calls(self):
        self.clock.now = 4790
        timeouts = []

        def send(_payload, timeout):
            timeouts.append(timeout)
            raise ProviderError(429, {"Retry-After": "10"})

        with self.assertRaises(JobDeadlineExpired):
            self.client(deadline=4800).call("x", send)
        self.assertEqual(timeouts, [10])
        self.assertEqual(len(self.rows("attempt_started")), 1)
        with self.assertRaises(RetryExhaustedError):
            self.client(deadline=4800).call("next", lambda *_: self.fail("late send"))
        self.assertEqual(len(self.rows("attempt_started")), 1)

    def test_deadline_is_rechecked_after_journal_admission(self):
        recovery = self.client(deadline=10)
        original = recovery._admit

        def slow_admission():
            original()
            self.clock.now = 10

        recovery._admit = slow_admission
        with self.assertRaises(RetryExhaustedError):
            recovery.call("x", lambda *_: self.fail("must not send after deadline"))
        self.assertEqual(len(self.rows("attempt_started")), 0)

    def test_expired_claim_after_journal_write_fails_closed_without_send(self):
        recovery = self.client(deadline=10)
        original = recovery._record

        def slow_journal(event):
            original(event)
            if event["event"] == "attempt_started":
                self.clock.now = 10

        recovery._record = slow_journal
        with self.assertRaisesRegex(RuntimeError, "after physical attempt claim"):
            recovery.call("x", lambda *_: self.fail("must not send after deadline"))
        self.assertEqual(len(self.rows("attempt_started")), 1)
        self.assertEqual(len(self.rows("attempt_finished")), 0)
        with self.assertRaisesRegex(RuntimeError, "unclosed"):
            self.client(job="job_b", deadline=100).call(
                "next", lambda *_: self.fail("uncertain request must block"))

    def test_five_non429_errors_and_mixed_errors_remain_bounded(self):
        sequence = [429, 429, 503, 429, 503, 503, 503, 503]
        calls = 0

        def send(*_):
            nonlocal calls
            status = sequence[calls]
            calls += 1
            raise ProviderError(status, {"Retry-After": "1"} if status == 429 else None)

        with self.assertRaises(RetryExhaustedError):
            self.client().call("x", send)
        self.assertEqual(calls, 8)
        self.assertEqual(sum(row["outcome"] == "service_unavailable"
                             for row in self.rows("attempt_finished")), 5)

    def test_r8_mixed_429_503_and_readtimeout_recovers_after_five_to_seven_failures(self):
        for failures in (5, 6, 7):
            with self.subTest(non429_failures=failures):
                before = len(self.rows("attempt_started"))
                sequence = [ProviderError(429, {"Retry-After": "1"})]
                sequence += [ProviderError(503) if n % 2 == 0 else TimeoutError()
                             for n in range(failures)]
                sequence += [self.success()]

                def send(_payload, timeout):
                    self.assertLessEqual(timeout, 120)
                    outcome = sequence.pop(0)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                self.assertEqual(self.client(job=f"r8_{failures}", cap=80, job_cap=60,
                                             max_attempts=8).call("x", send), self.success())
                self.assertEqual(len(sequence), 0)
                self.assertEqual(len(self.rows("attempt_started")) - before, failures + 2)
                self.assertEqual(self.rows("logical_finished")[-1]["status"], "success")


    def test_r12_recovers_after_eleven_503s(self):
        calls = 0

        def send(_payload, timeout):
            nonlocal calls
            calls += 1
            self.assertLessEqual(timeout, 120)
            if calls <= 11:
                raise ProviderError(503)
            return self.success()

        result = self.client(max_attempts=12).call("x", send)
        self.assertEqual(result, self.success())
        self.assertEqual(calls, 12)
        self.assertEqual(len(self.rows("attempt_started")), 12)
        self.assertEqual(len(self.rows("attempt_finished")), 12)
        self.assertEqual(
            self.rows("logical_finished")[-1]["status"], "success"
        )

    def test_r12_twelfth_503_exhausts(self):
        calls = 0

        def send(_payload, timeout):
            nonlocal calls
            calls += 1
            self.assertLessEqual(timeout, 120)
            raise ProviderError(503)

        with self.assertRaises(RetryExhaustedError):
            self.client(max_attempts=12).call("x", send)

        self.assertEqual(calls, 12)
        self.assertEqual(len(self.rows("attempt_started")), 12)
        self.assertEqual(len(self.rows("attempt_finished")), 12)
        self.assertEqual(
            self.rows("logical_finished")[-1]["status"],
            "retry_exhausted"
        )

    def test_r8_eighth_non429_failure_exhausts_without_ninth_send(self):
        sequence = [429, 503, 503, 429, 503, 503, 503, 503, 503, 503]

        def send(*_):
            status = sequence.pop(0)
            if status == 429:
                raise ProviderError(status, {"Retry-After": "1"})
            raise ProviderError(status)

        with self.assertRaises(RetryExhaustedError):
            self.client(max_attempts=8).call("x", send)
        self.assertEqual(sequence, [])
        self.assertEqual(len(self.rows("attempt_started")), 10)
        self.assertEqual(len(self.rows("attempt_finished")), 10)
        self.assertEqual(sum(row["outcome"] == "rate_limit"
                             for row in self.rows("attempt_finished")), 2)
        self.assertEqual(self.rows("logical_finished")[-1]["status"], "retry_exhausted")
        self.assertTrue(all(row["usage_unknown"] and row["total_tokens"] is None
                            for row in self.rows("attempt_finished")))

    def test_non429_window_begins_per_logical_request_not_at_recovery_creation(self):
        recovery = self.client(max_attempts=8, max_elapsed=10)
        for instant in (0, 2000):
            self.clock.now = instant
            attempts = 0

            def send(*_):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise TimeoutError()
                return self.success()

            recovery.call("x", send)
            self.assertEqual(attempts, 2)
        self.assertEqual(len(self.rows("attempt_started")), 4)
        self.assertEqual([row["status"] for row in self.rows("logical_finished")],
                         ["success", "success"])

    def test_1800_non429_window_stops_before_independent_4800_job_deadline(self):
        self.clock.now = 100
        calls = 0

        def send(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderError(503)
            self.clock.now = 1899
            raise ProviderError(429, {"Retry-After": "2"})

        with self.assertRaises(RetryExhaustedError):
            self.client(max_attempts=8, max_elapsed=1800, deadline=4800).call("x", send)
        self.assertEqual(calls, 2)
        self.assertEqual(self.rows("logical_finished")[-1]["status"], "retry_exhausted")
        self.assertEqual(len(self.rows("attempt_started")), 2)
        self.clock.now = 2000
        self.client(job="next", max_attempts=8, deadline=4800).call(
            "next", lambda *_: self.success())
        self.assertEqual(self.rows("logical_finished")[-1]["status"], "success")

    def test_r8_uses_manifest_style_sixty_per_job_and_batch_caps(self):
        for n in range(60):
            self.client(job="a", cap=61, job_cap=60, max_attempts=8).call(
                f"a{n}", lambda *_: self.success())
        with self.assertRaises(JobPhysicalAttemptLimitError):
            self.client(job="a", cap=61, job_cap=60, max_attempts=8).call(
                "a61", lambda *_: self.fail("must not send"))
        self.client(job="b", cap=61, job_cap=60, max_attempts=8).call(
            "b1", lambda *_: self.success())
        with self.assertRaises(PhysicalAttemptLimitError):
            self.client(job="b", cap=61, job_cap=60, max_attempts=8).call(
                "b2", lambda *_: self.fail("must not send"))
        self.assertEqual(len(self.rows("attempt_started")), 61)

    def test_readtimeout_and_hard_errors(self):
        with self.assertRaises(RetryExhaustedError):
            self.client().call("x", lambda *_: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(len(self.rows("attempt_started")), 5)
        with self.assertRaises(ProviderTerminalError):
            self.client().call("y", lambda *_: (_ for _ in ()).throw(ProviderError(401)))
        with self.assertRaises(ProviderTerminalError):
            self.client().call("z", lambda *_: (_ for _ in ()).throw(ProviderError(403)))
        with self.assertRaises(ProviderQuotaError):
            self.client().call("q", lambda *_: (_ for _ in ()).throw(
                ProviderError(429, body={"error": {"message": "daily quota exhausted"}})))

    def test_ambiguous_quota_429_is_not_assumed_to_be_daily(self):
        calls = 0

        def send(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderError(429, body={"error": {"code": "quota_exhausted"}})
            return self.success()

        self.client().call("x", send)
        self.assertEqual(calls, 2)

    def test_shared_429_cooldown_is_respected_by_next_job(self):
        with self.assertRaises(JobDeadlineExpired):
            self.client(deadline=10).call(
                "first", lambda *_: (_ for _ in ()).throw(
                    ProviderError(429, {"Retry-After": "11"})))
        called_at = []
        self.client(job="job_b", deadline=100).call(
            "next", lambda *_: (called_at.append(self.clock.now), self.success())[1])
        self.assertEqual(called_at, [11.0])
        self.assertEqual(len(self.rows("attempt_started")), 2)

    def test_mixed_429_cannot_reset_non429_elapsed_window(self):
        calls = 0

        def send(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderError(503)
            raise ProviderError(429, {"Retry-After": "5"})

        with self.assertRaises(RetryExhaustedError):
            self.client(deadline=100, max_elapsed=3).call("x", send)
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.rows("attempt_started")), 2)

    def test_per_job_and_batch_caps_are_independent(self):
        self.client(cap=3, job_cap=2).call("a1", lambda *_: self.success())
        self.client(cap=3, job_cap=2).call("a2", lambda *_: self.success())
        with self.assertRaises(JobPhysicalAttemptLimitError):
            self.client(cap=3, job_cap=2).call("a3", lambda *_: self.fail("job over cap"))
        self.client(job="job_b", cap=3, job_cap=2).call(
            "b1", lambda *_: self.success())
        with self.assertRaises(PhysicalAttemptLimitError):
            self.client(job="job_b", cap=3, job_cap=2).call(
                "b2", lambda *_: self.fail("batch over cap"))
        self.assertEqual(len(self.rows("attempt_started")), 3)
        statuses = [row["status"] for row in self.rows("logical_finished")]
        self.assertIn("job_budget", statuses)
        self.assertIn("batch_budget", statuses)

    def test_unclosed_attempt_blocks_next_job_without_replay(self):
        def uncertain(*_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.client().call("x", uncertain)
        with self.assertRaisesRegex(RuntimeError, "unclosed"):
            self.client(job="job_b").call("y", lambda *_: self.fail("must not send"))


class SafeSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = [
            {"job_id": name, "circuit_type": "CurrentMirror",
             "circuit_id": f"circuit_{index:04d}", "configuration_id": "mock",
             "config_sha256": "a" * 64, "d0_sha256": "b" * 64,
             "source_sha256": "c" * 64, "max_iterations": 5, "seed": 42}
            for index, name in enumerate(("job_a", "job_b", "job_c"), 1)
        ]
        self.manifest = {
            "batch_id": "offline_three_jobs", "manifest_sha256": "d" * 64,
            "source_revision": "e" * 40, "provider": "gemini",
            "model": "offline-model", "credential_scope": "offline-only",
            "resource_limits": {"max_physical_attempts": 30,
                                "per_job_physical_attempt_cap": 10,
                                "worker_timeout_seconds": 4800},
            "retry_policy": {"rate_limit_mode": "job_deadline"},
            "jobs": self.jobs,
        }

    def run_batch(self, worker):
        with patch("ActiveLearning.batch_runner_v2.runner.verify_job"):
            return _run_batch(self.manifest, self.root, worker, offline_only=True)

    @staticmethod
    def completion():
        return {"stop_reason": "max_iterations", "meets_spec": False,
                "simulation_valid": True, "iterations_completed": 5}

    def test_verified_timeout_skips_job_b_and_runs_job_c(self):
        called = []

        def worker(job, attempt):
            called.append(job["job_id"])
            if job["job_id"] != "job_b":
                return self.completion()
            (attempt / "partial_iteration.jsonl").write_text('{"iteration": 1}\n')
            result = {"state": "SKIPPED_JOB_TIMEOUT",
                      "batch_id": self.manifest["batch_id"],
                      "job_id": job["job_id"], "attempt_id": attempt.name,
                      "safe_point": "rate_limit_wait"}
            atomic_json(attempt / "worker_result.json", result)
            atomic_json(attempt / "child_reaped.json", {
                "attempt_id": attempt.name, "exit_confirmed": True, "exit_code": 3})
            return {"state": result["state"],
                    "worker_result_sha256": sha256_file(attempt / "worker_result.json")}

        report = self.run_batch(worker)
        self.assertEqual(called, ["job_a", "job_b", "job_c"])
        self.assertEqual((report["completed"], report["skipped_job_timeout"]), (2, 1))
        self.assertIsNone(report["jobs"]["job_b"]["meets_spec"])
        self.run_batch(lambda *_: self.fail("safe stop or completion replayed"))
        attempt = next((self.root / self.manifest["batch_id"] / "jobs" /
                        "job_b" / "attempts").iterdir())
        self.assertEqual((attempt / "partial_iteration.jsonl").read_text(),
                         '{"iteration": 1}\n')
        atomic_json(attempt / "worker_result.json", {"state": "tampered"})
        corrupt = self.run_batch(lambda *_: self.fail("tampered safe stop must halt"))
        self.assertEqual(corrupt["interrupted_uncertain"], 1)

    def test_unknown_worker_stops_batch_and_reports_unknown_with_zero_calls(self):
        called = []

        def worker(job, _attempt):
            called.append(job["job_id"])
            raise RuntimeError("synthetic worker crash")

        report = self.run_batch(worker)
        self.assertEqual(called, ["job_a"])
        self.assertEqual(report["jobs"]["job_b"]["state"], "PENDING")
        self.assertTrue(report["api_usage"]["usage_unknown"])
        self.assertIsNone(report["api_usage"]["total_tokens"])
        self.assertEqual(report["api_usage"]["physical_attempts"], 0)

    def test_unexpected_child_error_diagnostic_excludes_secret_text(self):
        from ActiveLearning.batch_runner_v2 import real_al_child

        attempt = self.root / "diagnostic_attempt"
        attempt.mkdir()
        identity = {"attempt_id": attempt.name}
        with patch.object(real_al_child, "_read_invocation",
                          return_value=(identity, attempt)), patch.object(
                              real_al_child, "execute_invocation",
                              side_effect=RuntimeError("SECRET_PROMPT_AND_KEY")):
            with self.assertRaisesRegex(RuntimeError, "SECRET_PROMPT_AND_KEY"):
                real_al_child.main([
                    "--invocation", str(attempt / "invocation.json"),
                    "--authorization", str(attempt / "approval.json"),
                    "--authorization-sha256", "0" * 64,
                ])
        diagnostic = (attempt / "worker_diagnostic.json").read_text()
        self.assertNotIn("SECRET_PROMPT_AND_KEY", diagnostic)
        self.assertEqual(json.loads(diagnostic)["error_type"], "RuntimeError")
        self.assertFalse((attempt / "worker_result.json").exists())

        attempt = (self.root / "batch" / "jobs" / "job_b" / "attempts" / ("a" * 32))
        attempt.mkdir(parents=True)
        journal = attempt.parents[3] / "api_usage.jsonl"
        identity = {"job_id": "job_b", "attempt_id": attempt.name,
                    "logical_id": "logical", "attempt": 1}
        append_jsonl(journal, {**identity, "event": "attempt_started"})
        append_jsonl(journal, {**identity, "event": "attempt_finished",
                               "outcome": "rate_limit"})
        append_jsonl(journal, {**identity, "event": "logical_finished",
                               "status": "job_deadline"})
        result = {"state": "SKIPPED_JOB_TIMEOUT", "safe_point": "rate_limit_wait",
                  "job_deadline_monotonic": 4800,
                  "journal_sha256": sha256_file(journal)}
        atomic_json(attempt / "worker_result.json", result)
        invocation = {"job_deadline_monotonic": 4800}
        verified = _verified_safe_stop(self.manifest, self.jobs[1], attempt,
                                       invocation, result)
        self.assertEqual(verified["state"], "SKIPPED_JOB_TIMEOUT")
        append_jsonl(journal, {**identity, "attempt": 2,
                               "event": "attempt_started"})
        result["journal_sha256"] = sha256_file(journal)
        atomic_json(attempt / "worker_result.json", result)
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            _verified_safe_stop(self.manifest, self.jobs[1], attempt,
                                invocation, result)


class SDKMockTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def transport(self, handler):
        try:
            import openai
            from openai import _base_client
        except ImportError:
            self.skipTest("OpenAI SDK is required for offline transport tests")

        httpx = (
            getattr(_base_client, "httpx2", None)
            or getattr(_base_client, "httpx", None)
        )
        if httpx is None:
            self.fail("OpenAI SDK HTTP transport binding not found")
        from ActiveLearning.batch_runner_v2.llm_bridge import build_gemini_chat_transport

        def client(*, follow_redirects):
            self.assertFalse(follow_redirects)
            return httpx.Client(transport=httpx.MockTransport(handler),
                                follow_redirects=follow_redirects)

        with patch.object(openai, "DefaultHttpxClient", side_effect=client):
            return build_gemini_chat_transport(
                model="gemini-test", api_key="OFFLINE_TEST_KEY",
                base_url="https://offline.invalid/v1beta/openai/")

    def recovery(self):
        return GeminiRecovery(
            self.root, provider="gemini", model="gemini-test",
            credential_scope="offline-only", max_physical_attempts=10,
            job_id="offline_job", attempt_id="a" * 32,
            rate_limit_mode="job_deadline", job_deadline_monotonic=4800,
            per_job_physical_attempt_cap=10, request_timeout=120,
            max_attempts=8, max_elapsed=1800,
            clock=lambda: 0.0, sleep=lambda _: None, jitter=lambda: 0.5)

    def test_sdk_mock_transport_does_not_follow_redirect(self):
        from openai import _base_client
        httpx = getattr(_base_client, 'httpx2', None) or getattr(_base_client, 'httpx', None)
        assert httpx is not None, 'SDK HTTP transport not found'
        requests = []

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(307, headers={"Location":
                                  "https://offline.invalid/hidden_second_request"})

        client = GeminiALClient(self.recovery(), self.transport(handler), "gemini-test")
        with self.assertRaises(ProviderTerminalError):
            client("offline prompt")
        self.assertEqual(len(requests), 1)
        rows = read_jsonl_strict(self.root / "api_usage.jsonl")
        self.assertEqual(sum(row["event"] == "attempt_started" for row in rows), 1)
        self.assertEqual(sum(row["event"] == "attempt_finished" for row in rows), 1)

    def test_sdk_mock_transport_503_has_no_hidden_retry(self):
        from openai import _base_client
        httpx = getattr(_base_client, 'httpx2', None) or getattr(_base_client, 'httpx', None)
        assert httpx is not None, 'SDK HTTP transport not found'
        requests = []

        def handler(request):
            requests.append(str(request.url))
            if len(requests) == 1:
                return httpx.Response(503, json={"error": {"message": "temporary"}})
            return httpx.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 1,
                "model": "gemini-test", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "recovered"},
                    "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1,
                          "total_tokens": 3},
            })

        client = GeminiALClient(self.recovery(), self.transport(handler), "gemini-test")
        self.assertEqual(client("offline prompt"), "recovered")
        self.assertEqual(len(requests), 2)
        rows = read_jsonl_strict(self.root / "api_usage.jsonl")
        self.assertEqual(sum(row["event"] == "attempt_started" for row in rows), 2)
        self.assertEqual(sum(row["event"] == "attempt_finished" for row in rows), 2)


if __name__ == "__main__":
    unittest.main()
