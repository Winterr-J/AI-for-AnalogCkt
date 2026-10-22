"""Mock-only Batch 012 Qwen AL Recovery V2 safety checks."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ActiveLearning import batch012_qwen_recovery_v2 as recovery


class LedgerTests(unittest.TestCase):
    def test_each_circuit_and_global_cap_include_prior_1671(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = recovery.RequestLedger(Path(directory) / "attempts.jsonl")
            for number in recovery.ORDER:
                cid = f"circuit_{number}"
                for expected in range(1, recovery.LIMITS[cid] + 1):
                    self.assertEqual(ledger.reserve(cid), expected)
                with self.assertRaisesRegex(recovery.RecoveryError, "BUDGET_EXHAUSTED"):
                    ledger.reserve(cid)
            self.assertEqual(ledger.total_new, 1185)
            self.assertEqual(ledger.historical_1671 + ledger.total_new, 1200)
            rows = [json.loads(line) for line in ledger.path.read_text().splitlines()]
            self.assertEqual(len(rows), 1185)
            self.assertEqual(rows[-1]["circuit_id"], "circuit_1671")
            self.assertEqual(rows[-1]["attempt_number"], 225)
            self.assertEqual(rows[-1]["global_new_attempt_number"], 1185)
            with self.assertRaisesRegex(recovery.RecoveryError, "BUDGET_EXHAUSTED"):
                ledger.reserve("circuit_1671")
            self.assertEqual(len(ledger.path.read_text().splitlines()), 1185)

    def test_unknown_job_and_failed_persistence_never_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = recovery.RequestLedger(Path(directory) / "attempts.jsonl")
            with self.assertRaisesRegex(recovery.RecoveryError, "unapproved circuit"):
                ledger.reserve("circuit_1672")
            with mock.patch.object(ledger, "_append", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    ledger.reserve("circuit_1671")
            self.assertEqual(ledger.total_new, 0)
            self.assertEqual(ledger.by_circuit["circuit_1671"], 0)


class MockExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "new_batch"
        self.plan = {
            "output_root": str(self.output),
            "revision": "mock_revision",
            "closure_sha256": "1" * 64,
            "order": ["circuit_1671"],
            "jobs": [{
                "circuit_id": "circuit_1671",
                "config": Path(self.temp.name) / "existing_config.yaml",
                "d0": Path(self.temp.name) / "existing_d0.jsonl",
                "config_sha256": "2" * 64,
                "d0_sha256": "3" * 64,
            }],
        }
        self.env = {
            "AL_LLM_PROVIDER": "qwen",
            "QWEN_MODEL": recovery.MODEL,
            "QWEN_BASE_URL": recovery.ENDPOINT,
            "DASHSCOPE_API_KEY": "mock-only-not-a-real-key",
        }

    def test_reservation_is_durable_before_one_mock_request(self):
        calls = []

        def fake_provider(prompt, **kwargs):
            ledger = self.output / "physical_attempts.jsonl"
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual(rows[-1]["event"], "reserved")
            self.assertEqual(rows[-1]["circuit_id"], "circuit_1671")
            self.assertEqual(kwargs["provider"], "qwen")
            self.assertEqual(kwargs["model"], recovery.MODEL)
            self.assertEqual(kwargs["max_attempts"], 1)
            self.assertEqual(kwargs["request_timeout"], 120.0)
            calls.append(prompt)
            return '{"mock":true}'

        def fake_al(circuit_type, cid, **kwargs):
            self.assertEqual((circuit_type, cid), ("OpAmp", "circuit_1671"))
            self.assertTrue(kwargs["require_existing_d0"])
            self.assertEqual(kwargs["expected_d0_sha256"], "3" * 64)
            self.assertEqual(kwargs["llm_client"]("offline"), '{"mock":true}')
            return {"iterations_completed": 1, "stop_reason": "mock_only"}

        with mock.patch.dict(os.environ, self.env), mock.patch(
                "ActiveLearning.main.run_configured_active_learning", side_effect=fake_al
        ), mock.patch(
                "ActiveLearning.inverse_design.llm_client.call_inverse_design_llm",
                side_effect=fake_provider
        ):
            recovery.execute(self.plan)
        self.assertEqual(calls, ["offline"])
        rows = [json.loads(line) for line in
                (self.output / "physical_attempts.jsonl").read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["reserved", "succeeded"])
        self.assertEqual(recovery.sha256(recovery.OLD_LEDGER),
                         recovery.OLD_LEDGER_SHA256)
        result = json.loads((self.output / "circuit_1671/result.json").read_text())
        self.assertEqual(result["physical_attempts_reserved"], 1)
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(FileExistsError):
                recovery.execute(self.plan)

    def test_missing_key_rejected_before_output_or_provider_import(self):
        env = {**self.env, "DASHSCOPE_API_KEY": ""}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(recovery.RecoveryError):
                recovery.execute(self.plan)
        self.assertFalse(self.output.exists())

    def test_failed_mock_request_counts_and_fails_stop(self):
        def fake_provider(prompt, **kwargs):
            raise RuntimeError("mock network failure")

        def fake_al(circuit_type, cid, **kwargs):
            kwargs["llm_client"]("offline")

        with mock.patch.dict(os.environ, self.env), mock.patch(
                "ActiveLearning.main.run_configured_active_learning", side_effect=fake_al
        ), mock.patch(
                "ActiveLearning.inverse_design.llm_client.call_inverse_design_llm",
                side_effect=fake_provider
        ):
            with self.assertRaisesRegex(RuntimeError, "mock network failure"):
                recovery.execute(self.plan)
        rows = [json.loads(line) for line in
                (self.output / "physical_attempts.jsonl").read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["reserved", "failed"])
        self.assertEqual(json.loads((self.output / "circuit_1671/result.json").read_text())
                         ["physical_attempts_reserved"], 1)


if __name__ == "__main__":
    unittest.main()
