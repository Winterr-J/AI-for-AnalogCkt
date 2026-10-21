"""Qwen V1 provider checks; all transports and clocks are local fakes."""

from __future__ import annotations

import json
import importlib
import time
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ActiveLearning.batch_runner_v2.al_process import _child_environment
from ActiveLearning.batch_runner_v2.authorization import (
    AuthorizationError, authorization_identity, current_source_revision,
    validate_authorization,
)
from ActiveLearning.batch_runner_v2.gemini_recovery import (
    GeminiRecovery, JobDeadlineExpired, JobPhysicalAttemptLimitError,
    PhysicalAttemptLimitError, ProviderQuotaError, ProviderTerminalError,
    RetryExhaustedError,
)
from ActiveLearning.batch_runner_v2.llm_bridge import (
    QwenALClient, build_qwen_chat_transport,
)
from ActiveLearning.batch_runner_v2.real_al_child import _production_run
from ActiveLearning.batch_runner_v2.schema import load_manifest
from ActiveLearning.batch_runner_v2.runner import _run_batch
from ActiveLearning.batch_runner_v2.storage import atomic_json, read_jsonl_strict, sha256_file
from ActiveLearning.inverse_design.inverse_designer import _parse_x_seed
from ActiveLearning.inverse_design.llm_client import call_inverse_design_llm
from ActiveLearning.inverse_design.qwen_provider import (
    QWEN_FLASH_MODEL, qwen_failure_kind, validate_qwen_endpoint,
)
from ActiveLearning.performance_prediction.performance_predictor import _parse_predictions


ENDPOINT = "https://workspace123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


def response(content='{"x_seed": {"A": 1.0}}', *, finish="stop", usage=True):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish, message=SimpleNamespace(content=content))],
        usage=(SimpleNamespace(prompt_tokens=10, completion_tokens=3,
                               total_tokens=13) if usage else None),
    )


class ProviderError(Exception):
    def __init__(self, status: int, code: str = "", retry_after: str | None = None):
        self.status_code = status
        self.body = {"error": {"code": code}}
        self.response = SimpleNamespace(
            headers={"Retry-After": retry_after} if retry_after else {})


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class QwenClientTests(unittest.TestCase):
    def test_child_only_inherits_the_selected_provider_key(self):
        with patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "QWEN_FAKE_KEY", "GEMINI_API_KEY": "GEMINI_FAKE_KEY",
            "OPENAI_API_KEY": "OPENAI_FAKE_KEY", "QWEN_BASE_URL": "https://wrong.invalid",
        }, clear=True):
            approval = {"authorization_sha256": "a" * 64}
            qwen = _child_environment({"provider": "qwen"}, approval, True)
            gemini = _child_environment({"provider": "gemini"}, approval, True)
        self.assertEqual(qwen["DASHSCOPE_API_KEY"], "QWEN_FAKE_KEY")
        for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "QWEN_BASE_URL"):
            self.assertNotIn(name, qwen)
        self.assertEqual(gemini["GEMINI_API_KEY"], "GEMINI_FAKE_KEY")
        self.assertNotIn("DASHSCOPE_API_KEY", gemini)

    def test_endpoint_is_exact_beijing_workspace_and_model_is_fixed(self):
        self.assertEqual(validate_qwen_endpoint(ENDPOINT), ENDPOINT)
        for bad in (
            "http://workspace123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://workspace123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
            ENDPOINT + "/chat/completions", ENDPOINT + "?key=secret",
            ENDPOINT.replace("workspace123", "workspace123.evil"),
            "https://user@workspace123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_qwen_endpoint(bad)

    def test_preparation_chat_parameters_parsing_and_usage(self):
        chat = MagicMock(return_value=response())
        sdk = SimpleNamespace(OpenAI=MagicMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=chat)))))
        usage = []
        with patch.dict(sys.modules, {"openai": sdk}), patch.dict(os.environ, {
            "AL_LLM_PROVIDER": "qwen", "DASHSCOPE_API_KEY": "FAKE_ONLY",
            "QWEN_BASE_URL": ENDPOINT,
        }):
            raw = call_inverse_design_llm("Return JSON", usage_log=usage)
        self.assertEqual(_parse_x_seed(raw), {"A": 1.0})
        self.assertEqual(sdk.OpenAI.call_args.kwargs["max_retries"], 0)
        self.assertEqual(sdk.OpenAI.call_args.kwargs["base_url"], ENDPOINT)
        self.assertEqual(chat.call_args.kwargs["model"], QWEN_FLASH_MODEL)
        self.assertEqual(chat.call_args.kwargs["extra_body"], {"enable_thinking": False})
        self.assertEqual(chat.call_args.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(usage[0]["total_tokens"], 13)
        self.assertEqual(usage[0]["provider"], "qwen")
        self.assertNotIn("FAKE_ONLY", repr(usage))

    def test_missing_truncated_and_invalid_json_are_rejected(self):
        for bad in (response("", finish="stop"), response(finish="length"),
                    response(finish=None)):
            chat = MagicMock(return_value=bad)
            sdk = SimpleNamespace(OpenAI=MagicMock(return_value=SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=chat)))))
            with self.subTest(finish=bad.choices[0].finish_reason), patch.dict(
                    sys.modules, {"openai": sdk}), self.assertRaises(RuntimeError):
                call_inverse_design_llm("Return JSON", provider="qwen",
                                        api_key="FAKE_ONLY", base_url=ENDPOINT)
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            _parse_x_seed("not JSON")
        with self.assertRaises(ValueError):
            _parse_x_seed('{}')
        with self.assertRaises(ValueError):
            _parse_predictions('{"predictions": []}', ["candidate"], ["gain"])

    def test_tracked_transport_disables_redirects_and_sdk_retries(self):
        sdk = SimpleNamespace(
            OpenAI=MagicMock(return_value=SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=MagicMock(
                    return_value=response()))))),
            DefaultHttpxClient=MagicMock(return_value=object()),
        )
        with patch.dict(sys.modules, {"openai": sdk}):
            send = build_qwen_chat_transport(
                model=QWEN_FLASH_MODEL, base_url=ENDPOINT, api_key="FAKE_ONLY")
            send([{"role": "user", "content": "Return JSON"}], 12)
        sdk.DefaultHttpxClient.assert_called_once_with(follow_redirects=False)
        self.assertEqual(sdk.OpenAI.call_args.kwargs["max_retries"], 0)
        self.assertEqual(sdk.OpenAI.call_args.kwargs["base_url"], ENDPOINT)
        kwargs = sdk.OpenAI.return_value.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 12)
        self.assertEqual(kwargs["extra_body"], {"enable_thinking": False})
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_auto_target_explicit_qwen_model_uses_existing_validator(self):
        try:
            auto_target = importlib.import_module("ActiveLearning.auto_target")
        except ImportError as error:
            self.skipTest(f"AutoTarget dependencies are unavailable: {error.name}")
        calls = []
        def fake_llm(_prompt, **kwargs):
            calls.append(kwargs)
            return '{}'
        with patch.object(auto_target, "build_target_prompt", return_value="Return JSON"), \
                patch.object(auto_target, "parse_target_response", return_value={}), \
                patch.object(auto_target, "validate_target_recommendation",
                             return_value={"valid": True, "target_specs": {"gain": 1},
                                           "errors": []}):
            result = auto_target.recommend_target(
                object(), {}, {}, llm_client=fake_llm, provider="qwen")
        self.assertEqual(result["status"], "TARGET_VALIDATED")
        self.assertEqual(calls[0]["provider"], "qwen")
        self.assertEqual(calls[0]["model"], QWEN_FLASH_MODEL)
        self.assertEqual(calls[0]["max_attempts"], 1)


class QwenRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def recovery(self, *, job="job_a", cap=20, job_cap=10, deadline=4800):
        return GeminiRecovery(
            self.root, provider="qwen", model=QWEN_FLASH_MODEL,
            credential_scope="test_workspace", max_physical_attempts=cap,
            request_timeout=120, max_attempts=8, max_elapsed=1800,
            job_id=job, attempt_id="a" * 32, rate_limit_mode="job_deadline",
            job_deadline_monotonic=deadline, per_job_physical_attempt_cap=job_cap,
            clock=lambda: self.clock.now, sleep=self.clock.sleep, jitter=lambda: 0.5,
        )

    def rows(self, event):
        return [row for row in read_jsonl_strict(self.root / "api_usage.jsonl")
                if row["event"] == event]

    def test_qwen_throttling_503_timeout_recovery_and_unknown_failed_usage(self):
        sequence = [ProviderError(429, "Throttling.AllocationQuota", "2"),
                    ProviderError(503), TimeoutError(), response()]
        def send(_messages, timeout):
            self.assertGreater(timeout, 0)
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        usage = []
        result = QwenALClient(self.recovery(), send, QWEN_FLASH_MODEL)(
            "Return JSON", usage_log=usage)
        self.assertEqual(_parse_x_seed(result), {"A": 1.0})
        self.assertEqual(len(self.rows("attempt_started")), 4)
        self.assertEqual(len(self.rows("attempt_finished")), 4)
        self.assertEqual([row["outcome"] for row in self.rows("attempt_finished")],
                         ["rate_limit", "service_unavailable", "network_timeout", "success"])
        self.assertTrue(all(row["usage_unknown"] and row["total_tokens"] is None
                            for row in self.rows("attempt_finished")[:-1]))
        self.assertEqual(usage[0]["total_tokens"], 13)
        self.assertEqual(sequence, [])

    def test_terminal_billing_blocks_scope_but_allocation_quota_is_transient(self):
        self.assertEqual(qwen_failure_kind(ProviderError(429, "insufficient_quota")),
                         "rate_limit")
        self.assertEqual(qwen_failure_kind(ProviderError(400, "Arrearage")), "quota")
        self.assertEqual(qwen_failure_kind(ProviderError(401)), "terminal")
        with self.assertRaises(ProviderQuotaError):
            self.recovery().call("Return JSON", lambda *_: (_ for _ in ()).throw(
                ProviderError(429, "BudgetLimitExceeded")))
        self.assertEqual(len(self.rows("attempt_started")), 1)
        with self.assertRaises(ProviderQuotaError):
            self.recovery(job="job_b").call("Return JSON", lambda *_: self.fail(
                "blocked scope must not send"))

    def test_eighth_non429_failure_exhausts_but_429_does_not_count(self):
        sequence = [ProviderError(503)] * 7
        sequence.insert(3, ProviderError(429, "Throttling.RateQuota", "1"))
        sequence.append(response())
        def send(*_):
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        self.recovery().call("Return JSON", send)
        self.assertEqual(len(self.rows("attempt_started")), 9)
        self.assertEqual(sequence, [])

        with tempfile.TemporaryDirectory() as other:
            failed = GeminiRecovery(
                Path(other), provider="qwen", model=QWEN_FLASH_MODEL,
                credential_scope="test_workspace", max_physical_attempts=10,
                job_id="job_a", attempt_id="b" * 32,
                rate_limit_mode="job_deadline", job_deadline_monotonic=4800,
                per_job_physical_attempt_cap=10, max_attempts=8,
                clock=lambda: self.clock.now, sleep=self.clock.sleep,
                jitter=lambda: 0.5)
            with self.assertRaises(RetryExhaustedError):
                failed.call("Return JSON", lambda *_: (_ for _ in ()).throw(
                    ProviderError(503)))
            ledger = read_jsonl_strict(Path(other) / "api_usage.jsonl")
            self.assertEqual(sum(row["event"] == "attempt_started" for row in ledger), 8)

    def test_job_and_batch_caps_and_429_job_deadline(self):
        self.recovery(cap=2, job_cap=1).call("Return JSON", lambda *_: response())
        with self.assertRaises(JobPhysicalAttemptLimitError):
            self.recovery(cap=2, job_cap=1).call("Return JSON", lambda *_: self.fail(
                "job cap must not send"))
        self.assertEqual(len(self.rows("attempt_started")), 1)

        other = self.recovery(job="job_b", cap=2, job_cap=1, deadline=3)
        with self.assertRaises(JobDeadlineExpired):
            other.call("Return JSON", lambda *_: (_ for _ in ()).throw(
                ProviderError(429, "Throttling.RateQuota", "4")))
        self.assertEqual(len(self.rows("attempt_started")), 2)
        with self.assertRaises(PhysicalAttemptLimitError):
            self.recovery(job="job_c", cap=2, job_cap=1).call(
                "Return JSON", lambda *_: self.fail("batch cap must stop"))

    def test_unclosed_physical_request_prevents_next_send(self):
        with self.assertRaises(KeyboardInterrupt):
            self.recovery().call("Return JSON", lambda *_: (_ for _ in ()).throw(
                KeyboardInterrupt()))
        with self.assertRaisesRegex(RuntimeError, "unclosed"):
            self.recovery(job="job_b").call("Return JSON", lambda *_: self.fail(
                "unclosed request must not send"))

    def test_truncated_http_success_is_rejected_after_accounted_attempt(self):
        usage = []
        with self.assertRaisesRegex(RuntimeError, "finish normally"):
            QwenALClient(self.recovery(), lambda *_: response(finish="length"),
                         QWEN_FLASH_MODEL)("Return JSON", usage_log=usage)
        self.assertEqual(len(self.rows("attempt_started")), 1)
        self.assertEqual(len(self.rows("attempt_finished")), 1)
        self.assertEqual(self.rows("attempt_finished")[0]["total_tokens"], 13)
        self.assertEqual(usage, [])

    def test_missing_success_usage_remains_unknown_in_physical_ledger(self):
        text = QwenALClient(self.recovery(), lambda *_: response(usage=False),
                            QWEN_FLASH_MODEL)("Return JSON")
        self.assertIn("x_seed", text)
        finished = self.rows("attempt_finished")
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0]["usage_unknown"])
        self.assertIsNone(finished[0]["total_tokens"])


class QwenManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "manifest.json"
        self.manifest = {
            "schema_version": "batch_runner_v2b2a_candidate_v1", "batch_id": "qwen_offline",
            "provider": "qwen", "model": QWEN_FLASH_MODEL,
            "provider_base_url": ENDPOINT, "credential_scope": "test_workspace",
            "resource_limits": {"max_physical_attempts": 10,
                                "per_job_physical_attempt_cap": 5,
                                "worker_timeout_seconds": 4800},
            "retry_policy": {"request_timeout_seconds": 120, "max_attempts": 8,
                             "max_elapsed_seconds": 1800, "rate_limit_mode": "job_deadline"},
            "source_revision": current_source_revision(),
            "source_closure": {"placeholder": "0" * 64},
            "jobs": [{"job_id": "job_a", "circuit_type": "OpAmp",
                      "circuit_id": "circuit_0001", "configuration_id": "offline",
                      "config_path": "cfg.json", "config_sha256": "0" * 64,
                      "d0_path": "d0.jsonl", "d0_sha256": "0" * 64,
                      "source_path": "source.py", "source_sha256": "0" * 64,
                      "target_spec_sha256": "0" * 64, "max_iterations": 1,
                      "seed": 42, "execution_mode": "al_only"}],
        }

    def load(self):
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        with patch("ActiveLearning.batch_runner_v2.schema._verify_source_closure"), patch(
                "ActiveLearning.batch_runner_v2.schema.verify_job"):
            return load_manifest(self.path)

    def test_endpoint_is_authorized_identity_and_not_substitutable(self):
        loaded = self.load()
        self.assertEqual(authorization_identity(loaded, self.path.parent)[
            "provider_base_url"], ENDPOINT)
        approval_path = self.path.parent / "approval.json"
        atomic_json(approval_path, {
            "schema_version": "batch_runner_v2b2a_authorization_v1",
            "authorization_id": "qwen_offline_approval",
            "execution_scope": "offline_integration", "approved": True,
            "approved_by": "offline-test", "approved_at": "2026-10-21T00:00:00Z",
            **authorization_identity(loaded, self.path.parent),
        })
        digest = sha256_file(approval_path)
        validate_authorization(loaded, approval_path, digest, self.path.parent)
        self.manifest["provider_base_url"] = ENDPOINT.replace("workspace123", "other123")
        other = self.load()
        self.assertNotEqual(loaded["manifest_sha256"], other["manifest_sha256"])
        self.assertNotEqual(authorization_identity(loaded, self.path.parent),
                            authorization_identity(other, self.path.parent))
        with self.assertRaises(AuthorizationError):
            validate_authorization(other, approval_path, digest, self.path.parent)

    def test_wrong_region_model_or_legacy_mode_fails(self):
        for field, value in (("provider_base_url", ENDPOINT.replace("cn-beijing", "eu-central-1")),
                             ("model", "qwen-flash")):
            with self.subTest(field=field):
                self.manifest[field] = value
                with self.assertRaises(ValueError):
                    self.load()
                self.manifest[field] = ENDPOINT if field == "provider_base_url" else QWEN_FLASH_MODEL
        self.manifest["retry_policy"].pop("rate_limit_mode")
        self.manifest["resource_limits"].pop("per_job_physical_attempt_cap")
        with self.assertRaisesRegex(ValueError, "job_deadline"):
            self.load()


class QwenR13IsolationTests(unittest.TestCase):
    def test_production_boundary_injects_qwen_without_real_al_or_spice(self):
        import ActiveLearning

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "cfg.json").write_text('{}', encoding="utf-8")
            (root / "d0.jsonl").write_text('{"sample_id":"mock"}\n', encoding="utf-8")
            (root / "source.py").write_text('# mock topology\n', encoding="utf-8")
            attempt = root / "batch" / "jobs" / "job_a" / "attempts" / ("a" * 32)
            attempt.mkdir(parents=True)
            job = {"job_id": "job_a", "circuit_type": "OpAmp",
                   "circuit_id": "circuit_0001", "configuration_id": "mock",
                   "config_path": "cfg.json", "config_sha256": "a" * 64,
                   "d0_path": "d0.jsonl", "d0_sha256": "b" * 64,
                   "source_path": "source.py", "source_sha256": "c" * 64,
                   "target_spec_sha256": "d" * 64}
            manifest = {"repository_root": str(root), "provider": "qwen",
                        "model": QWEN_FLASH_MODEL, "provider_base_url": ENDPOINT,
                        "credential_scope": "test_workspace", "manifest_sha256": "e" * 64,
                        "source_revision": "f" * 40,
                        "resource_limits": {"max_physical_attempts": 10,
                                            "per_job_physical_attempt_cap": 5},
                        "retry_policy": {"request_timeout_seconds": 120,
                                         "max_attempts": 8, "max_elapsed_seconds": 1800,
                                         "rate_limit_mode": "job_deadline"}}
            approval = {"authorization_id": "offline", "authorization_sha256": "0" * 64}
            invocation = {"batch_id": "batch", "job_id": "job_a",
                          "attempt_id": attempt.name,
                          "job_deadline_monotonic": time.monotonic() + 100}
            chat = MagicMock(return_value=response())
            sdk = SimpleNamespace(
                OpenAI=MagicMock(return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=SimpleNamespace(create=chat)))),
                DefaultHttpxClient=MagicMock(return_value=object()),
            )
            def fake_al(*_args, llm_client, run_context_callback, runs_root, **_kwargs):
                self.assertEqual(_parse_x_seed(llm_client("Return JSON")), {"A": 1.0})
                run_dir = runs_root / "OpAmp" / "circuit_0001" / "offline_run"
                run_dir.mkdir(parents=True)
                (run_dir / "summary.json").write_text('{}', encoding="utf-8")
                run_context_callback({"run_id": "offline_run", "run_directory": str(run_dir)})
                return {"stop_reason": "max_iterations", "iterations_completed": 1,
                        "best_or_final_record": {"status": {
                            "simulation_valid": True, "meets_spec": False},
                            "metrics": {"gain": 1.0}}}
            fake_main = SimpleNamespace(
                _resolve_paths=lambda *_: {"topology": str(root / "source.py")},
                run_configured_active_learning=fake_al)
            with patch.dict(os.environ, {
                    "BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256": "0" * 64,
                    "DASHSCOPE_API_KEY": "FAKE_ONLY"}), patch.dict(
                    sys.modules, {"openai": sdk}), patch.object(
                    ActiveLearning, "main", fake_main, create=True):
                result = _production_run(invocation, manifest, job, approval, attempt)
            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(result["d0_record_count"], 1)
            self.assertEqual(chat.call_count, 1)
            self.assertEqual(len(read_jsonl_strict(attempt.parents[3] / "api_usage.jsonl")), 4)

    def test_reaped_failed_qwen_job_is_uncertain_and_next_job_may_continue(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = [{"job_id": name, "circuit_type": "OpAmp",
                     "circuit_id": f"circuit_{index:04d}", "configuration_id": "mock",
                     "config_sha256": "a" * 64, "d0_sha256": "b" * 64,
                     "source_sha256": "c" * 64, "max_iterations": 1, "seed": 42}
                    for index, name in enumerate(("failed", "next"), 1)]
            manifest = {
                "batch_id": "qwen_isolation", "manifest_sha256": "d" * 64,
                "provider": "qwen", "model": QWEN_FLASH_MODEL,
                "credential_scope": "test_workspace", "jobs": jobs,
                "resource_limits": {"max_physical_attempts": 10,
                                    "per_job_physical_attempt_cap": 5,
                                    "worker_timeout_seconds": 4800},
                "retry_policy": {"rate_limit_mode": "job_deadline"},
            }
            called = []
            def worker(job, attempt):
                called.append(job["job_id"])
                if job["job_id"] == "next":
                    return {"stop_reason": "max_iterations", "meets_spec": False,
                            "simulation_valid": True, "iterations_completed": 1}
                recovery = GeminiRecovery(
                    attempt.parents[3], provider="qwen", model=QWEN_FLASH_MODEL,
                    credential_scope="test_workspace", max_physical_attempts=10,
                    job_id=job["job_id"], attempt_id=attempt.name,
                    rate_limit_mode="job_deadline", job_deadline_monotonic=4800,
                    per_job_physical_attempt_cap=5, clock=lambda: 0.0,
                    sleep=lambda _: None)
                try:
                    recovery.call("Return JSON", lambda *_: (_ for _ in ()).throw(
                        ProviderError(401, "InvalidApiKey")))
                except ProviderTerminalError:
                    atomic_json(attempt / "child_reaped.json", {
                        "attempt_id": attempt.name, "exit_confirmed": True,
                        "exit_code": 1})
                    raise

            with patch("ActiveLearning.batch_runner_v2.runner.verify_job"):
                report = _run_batch(manifest, temporary, worker, offline_only=True)
            self.assertEqual(called, ["failed", "next"])
            self.assertEqual(report["jobs"]["failed"]["state"],
                             "INTERRUPTED_UNCERTAIN")
            self.assertEqual(report["jobs"]["next"]["state"], "COMPLETED")
            self.assertTrue(report["api_usage"]["usage_unknown"])
            self.assertIsNone(report["api_usage"]["total_tokens"])
            self.assertEqual(report["api_usage"]["physical_attempts"], 1)
            with patch("ActiveLearning.batch_runner_v2.runner.verify_job"):
                _run_batch(manifest, temporary, lambda *_: self.fail(
                    "uncertain job must not replay"), offline_only=True)


if __name__ == "__main__":
    unittest.main()
