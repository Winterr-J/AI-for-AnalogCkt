"""A2 subprocess and LLM boundary tests; no AL, SPICE, or network."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from ActiveLearning.batch_runner_v2.al_process import (
    _stop_posix_group, production_invocation_contract, run_offline_fake_child,
)
from ActiveLearning.batch_runner_v2.gemini_recovery import GeminiRecovery
from ActiveLearning.batch_runner_v2.llm_bridge import (
    GeminiALClient, build_gemini_chat_transport,
)
from ActiveLearning.batch_runner_v2.runner import (
    UnverifiedOrphanError, run_mock_batch, status_batch,
)
from ActiveLearning.batch_runner_v2.storage import sha256_file


class A2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "outputs"
        (self.root / "cfg" / "d0").mkdir(parents=True)
        (self.root / "src").mkdir()
        jobs = []
        for n in range(2):
            ident = f"circuit_{n:04d}"
            cfg = self.root / "cfg" / f"{ident}.json"
            d0 = self.root / "cfg" / "d0" / f"{ident}.jsonl"
            src = self.root / "src" / f"{ident}.py"
            cfg.write_text(json.dumps({"configuration_id": f"frozen_{n}",
                 "active_learning": {"max_iterations": 2, "data_expansion_seed": 42},
                 "data": {"d0_path_relative_to": "config", "d0_path": f"d0/{ident}.jsonl"}}), encoding="utf-8")
            d0.write_text('{"sample_id":"d0"}\n', encoding="utf-8")
            src.write_text("# fake\n", encoding="utf-8")
            jobs.append({"job_id": f"job_{n}", "circuit_type": "OpAmp", "circuit_id": ident,
                         "configuration_id": f"frozen_{n}", "config_path": f"cfg/{ident}.json",
                         "config_sha256": sha256_file(cfg), "d0_path": f"cfg/d0/{ident}.jsonl",
                         "d0_sha256": sha256_file(d0), "source_path": f"src/{ident}.py",
                         "source_sha256": sha256_file(src), "max_iterations": 2,
                         "seed": 42, "execution_mode": "al_only"})
        self.jobs = jobs
        self.manifest = self.root / "batch.json"
        self.manifest.write_text(json.dumps({"schema_version": "batch_runner_v2a_offline_v1",
            "batch_id": "offline_a2", "provider": "gemini", "model": "gemini-test",
            "credential_scope": "offline-only", "resource_limits": {"max_physical_attempts": 10},
            "retry_policy": {"request_timeout_seconds": 120, "max_attempts": 8,
                             "max_elapsed_seconds": 1800}, "jobs": jobs}), encoding="utf-8")

    def worker(self, scenarios):
        def run(job, attempt):
            scenario = scenarios[job["job_id"]]
            if scenario == "sleep":
                return run_offline_fake_child(
                    job, attempt, scenario=scenario, timeout_seconds=0.2)
            return run_offline_fake_child(job, attempt, scenario=scenario)

        return run

    def test_completion_identity_and_restart(self):
        report = run_mock_batch(self.manifest, self.output, self.worker({"job_0": "success", "job_1": "invalid"}))
        self.assertEqual(report["completed"], 2)
        rows = status_batch("offline_a2", self.output)["jobs"]
        self.assertTrue(rows["job_0"]["simulation_valid"])
        self.assertFalse(rows["job_1"]["simulation_valid"])
        self.assertFalse(rows["job_0"]["meets_spec"])
        self.assertEqual(rows["job_0"]["attempts"][0]["attempt_id"],
                         Path(rows["job_0"]["attempts"][0]["attempt_directory"]).name)
        run_mock_batch(self.manifest, self.output, lambda *_: self.fail("replayed"))
        self.assertEqual(rows, status_batch("offline_a2", self.output)["jobs"])

    def test_terminal_then_next(self):
        report = run_mock_batch(self.manifest, self.output, self.worker({"job_0": "terminal", "job_1": "success"}))
        self.assertEqual((report["failed_terminal"], report["completed"]), (1, 1))

    def test_completed_child_artifact_corruption_blocks_on_restart(self):
        run_mock_batch(self.manifest, self.output,
                       self.worker({"job_0": "success", "job_1": "success"}))
        row = status_batch("offline_a2", self.output)["jobs"]["job_0"]
        result_path = Path(row["attempts"][0]["attempt_directory"]) / "result.json"
        child_path = Path(json.loads(result_path.read_text(encoding="utf-8"))["result_path"])
        child_path.write_text("{}", encoding="utf-8")
        report = run_mock_batch(self.manifest, self.output,
                                lambda *_: self.fail("corrupt completion replayed"))
        self.assertEqual((report["completed"], report["blocked"]), (1, 1))

    def test_crash_nonzero_partial_corrupt_remain_uncertain(self):
        for scenario in ("crash", "nonzero", "partial", "corrupt"):
            with self.subTest(scenario=scenario):
                output = self.root / scenario
                report = run_mock_batch(self.manifest, output,
                                        self.worker({"job_0": scenario, "job_1": "success"}))
                self.assertEqual((report["interrupted_uncertain"], report["completed"]), (1, 1))
                before = status_batch("offline_a2", output)["jobs"]["job_0"]["attempts"]
                run_mock_batch(self.manifest, output, lambda *_: self.fail("replayed"))
                self.assertEqual(before, status_batch("offline_a2", output)["jobs"]["job_0"]["attempts"])

    def test_confirmed_timeout_is_uncertain_but_next_job_runs(self):
        report = run_mock_batch(self.manifest, self.output,
                                self.worker({"job_0": "sleep", "job_1": "success"}))
        self.assertEqual((report["interrupted_uncertain"], report["completed"]), (1, 1))

    def test_unverified_orphan_stops_batch_and_restart(self):
        with patch("ActiveLearning.batch_runner_v2.al_process._stop_child", return_value=False):
            # A fake process is needed here: patching termination on a real sleeper
            # would deliberately leave an orphan test process behind.
            class FakeProcess:
                def wait(self, timeout):
                    raise subprocess.TimeoutExpired("fake", timeout)

            with patch("ActiveLearning.batch_runner_v2.al_process.subprocess.Popen",
                       return_value=FakeProcess()) as spawned:
                report = run_mock_batch(self.manifest, self.output,
                                        self.worker({"job_0": "sleep", "job_1": "success"}))
        self.assertEqual(spawned.call_args.kwargs["start_new_session"], os.name == "posix")
        self.assertEqual(report["completed"], 0)
        state = status_batch("offline_a2", self.output)
        self.assertTrue(state["orphan_unverified"])
        self.assertEqual(state["jobs"]["job_1"]["state"], "PENDING")
        with self.assertRaises(UnverifiedOrphanError):
            run_mock_batch(self.manifest, self.output, lambda *_: self.fail("orphan restart"))

    def test_posix_group_termination_is_bounded_and_escalates(self):
        process = MagicMock(pid=24680)
        process.poll.return_value = 0
        with patch.object(signal, "SIGKILL", 9, create=True), \
             patch.object(os, "killpg", create=True) as killpg, \
             patch("ActiveLearning.batch_runner_v2.al_process._wait_group_empty",
                   side_effect=[False, True]) as wait_group:
            self.assertTrue(_stop_posix_group(process))
        self.assertEqual(killpg.call_args_list,
                         [call(24680, signal.SIGTERM), call(24680, 9)])
        self.assertEqual(wait_group.call_count, 2)

    def test_posix_group_unverified_after_kill_is_not_cleared(self):
        process = MagicMock(pid=24680)
        process.poll.return_value = 0
        with patch.object(signal, "SIGKILL", 9, create=True), \
             patch.object(os, "killpg", create=True), \
             patch("ActiveLearning.batch_runner_v2.al_process._wait_group_empty",
                   return_value=False):
            self.assertFalse(_stop_posix_group(process))

    def test_wait_os_error_with_unverified_exit_stops_next_job(self):
        class FakeProcess:
            def wait(self, timeout):
                raise OSError("mock wait failure")

        with patch("ActiveLearning.batch_runner_v2.al_process.subprocess.Popen",
                   return_value=FakeProcess()), \
             patch("ActiveLearning.batch_runner_v2.al_process._stop_child",
                   return_value=False):
            report = run_mock_batch(self.manifest, self.output,
                                    self.worker({"job_0": "success", "job_1": "success"}))
        self.assertEqual(report["completed"], 0)
        self.assertTrue(status_batch("offline_a2", self.output)["orphan_unverified"])
        self.assertEqual(status_batch("offline_a2", self.output)["jobs"]["job_1"]["state"], "PENDING")

    def test_spawn_os_error_keeps_intent_and_stops_batch(self):
        with patch("ActiveLearning.batch_runner_v2.al_process.subprocess.Popen",
                   side_effect=OSError("mock spawn failure")):
            report = run_mock_batch(self.manifest, self.output,
                                    self.worker({"job_0": "success", "job_1": "success"}))
        self.assertEqual(report["completed"], 0)
        row = status_batch("offline_a2", self.output)["jobs"]["job_0"]
        self.assertEqual(row["error_code"], "ORPHAN_UNVERIFIED")
        self.assertTrue((Path(row["attempts"][0]["attempt_directory"]) /
                         "child_spawn_intent.json").is_file())

    def test_contract_is_non_executable(self):
        contract = production_invocation_contract(self.jobs[0], self.root)
        self.assertFalse(contract["real_execution_enabled"])
        self.assertEqual(contract["configuration_id"], "frozen_0")
        self.assertNotIn("command", contract)

    def test_restart_with_unreaped_spawn_intent_stops_next_job(self):
        def interrupted(job, attempt):
            if job["job_id"] == "job_0":
                (attempt / "child_spawn_intent.json").write_text("{}", encoding="utf-8")
                raise KeyboardInterrupt()
            self.fail("next job started while child may be alive")

        with self.assertRaises(KeyboardInterrupt):
            run_mock_batch(self.manifest, self.output, interrupted)
        report = run_mock_batch(self.manifest, self.output,
                                lambda *_: self.fail("unverified restart spawned work"))
        self.assertEqual(report["completed"], 0)
        state = status_batch("offline_a2", self.output)
        self.assertTrue(state["orphan_unverified"])
        self.assertEqual(state["jobs"]["job_1"]["state"], "PENDING")

    def test_gemini_bridge_one_retry_owner_and_unknown_usage(self):
        root = self.root / "ledger"
        recovery = GeminiRecovery(root, provider="gemini", model="gemini-test",
                                  credential_scope="offline-only", max_physical_attempts=3,
                                  sleep=lambda _: None, jitter=lambda: 0)
        attempts = []

        class Error(Exception):
            status_code = 503

        def send(messages, timeout):
            attempts.append((messages, timeout))
            if len(attempts) == 1:
                raise Error()
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}

        from ActiveLearning.batch_runner_v2 import gemini_recovery
        with patch.object(gemini_recovery, "_failure_kind", wraps=gemini_recovery._failure_kind):
            usage = []
            self.assertEqual(GeminiALClient(recovery, send, "gemini-test")("hello", usage_log=usage), "ok")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(usage[0]["total_tokens"], 5)
        events = [json.loads(line) for line in (root / "api_usage.jsonl").read_text().splitlines()]
        self.assertEqual(sum(e["event"] == "logical_started" for e in events), 1)
        self.assertEqual(sum(e["event"] == "attempt_started" for e in events), 2)
        self.assertTrue(any(e.get("usage_unknown") for e in events))

    def test_sdk_transport_disables_internal_retries_and_keeps_credentials_out_of_journal(self):
        response = {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                              "total_tokens": 3}}
        create = MagicMock(return_value=response)
        constructor = MagicMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=constructor)}):
            send = build_gemini_chat_transport(
                model="gemini-test", api_key="SECRET_KEY_CANARY",
                base_url="https://offline.invalid/v1beta/openai/")
        recovery = GeminiRecovery(self.root / "transport_ledger", provider="gemini",
                                  model="gemini-test", credential_scope="offline-only",
                                  max_physical_attempts=2)
        self.assertEqual(GeminiALClient(recovery, send, "gemini-test")("SECRET_PROMPT_CANARY"), "ok")
        constructor.assert_called_once_with(
            api_key="SECRET_KEY_CANARY",
            base_url="https://offline.invalid/v1beta/openai/", max_retries=0)
        self.assertEqual(create.call_args.kwargs["timeout"], 120.0)
        self.assertEqual(create.call_args.kwargs["model"], "gemini-test")
        ledger = (self.root / "transport_ledger" / "api_usage.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("SECRET_KEY_CANARY", ledger)
        self.assertNotIn("SECRET_PROMPT_CANARY", ledger)

    def test_sdk_transport_429_retry_after_counts_two_physical_attempts(self):
        class RateLimit(Exception):
            status_code = 429
            headers = {"Retry-After": "0.01"}

        create = MagicMock(side_effect=[RateLimit(), {
            "choices": [{"message": {"content": "recovered"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}])
        constructor = MagicMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=constructor)}):
            send = build_gemini_chat_transport(model="gemini-test", api_key="offline-key",
                    base_url="https://offline.invalid/v1beta/openai/")
        recovery = GeminiRecovery(self.root / "retry_ledger", provider="gemini",
                                  model="gemini-test", credential_scope="offline-only",
                                  max_physical_attempts=2)
        self.assertEqual(GeminiALClient(recovery, send, "gemini-test")("hello"), "recovered")
        self.assertEqual(create.call_count, 2)
        events = [json.loads(line) for line in
                  (self.root / "retry_ledger" / "api_usage.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["event"] == "logical_started" for row in events), 1)
        self.assertEqual(sum(row["event"] == "attempt_started" for row in events), 2)


if __name__ == "__main__":
    unittest.main()
