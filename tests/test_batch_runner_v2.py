"""Offline V2-A scheduler tests; no production worker is imported."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ActiveLearning.batch_runner_v2 import plan_batch, report_batch, run_mock_batch, status_batch
from ActiveLearning.batch_runner_v2.__main__ import main as cli_main
from ActiveLearning.batch_runner_v2.storage import sha256_file


class BatchRunnerV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "outputs"
        (self.root / "cfg" / "d0").mkdir(parents=True)
        (self.root / "src").mkdir()
        self.jobs = []
        for number in range(3):
            circuit_id = f"circuit_{number:04d}"
            config_path = self.root / "cfg" / f"{circuit_id}.json"
            d0_path = self.root / "cfg" / "d0" / f"{circuit_id}.jsonl"
            source_path = self.root / "src" / f"{circuit_id}.py"
            config_path.write_text(json.dumps({
                "configuration_id": f"frozen_{number}",
                "active_learning": {"max_iterations": 2, "data_expansion_seed": 42},
                "data": {"d0_path_relative_to": "config",
                         "d0_path": f"d0/{circuit_id}.jsonl",
                         "initialize_if_missing": False},
                "target_specs": {"gain": {"operator": ">=", "value": 1}},
            }), encoding="utf-8")
            d0_path.write_text(json.dumps({"sample_id": "d0_0", "data_source": "independent_d0_new_evaluation"}) + "\n", encoding="utf-8")
            source_path.write_text("# frozen topology\n", encoding="utf-8")
            self.jobs.append({
                "job_id": f"job_{number}", "circuit_type": "OpAmp",
                "circuit_id": circuit_id, "configuration_id": f"frozen_{number}",
                "config_path": f"cfg/{circuit_id}.json",
                "config_sha256": sha256_file(config_path),
                "d0_path": f"cfg/d0/{circuit_id}.jsonl",
                "d0_sha256": sha256_file(d0_path),
                "source_path": f"src/{circuit_id}.py",
                "source_sha256": sha256_file(source_path),
                "max_iterations": 2, "seed": 42, "execution_mode": "al_only",
            })
        self.manifest = self.root / "batch.json"
        self.payload = {
            "schema_version": "batch_runner_v2a_offline_v1", "batch_id": "offline_batch_1",
            "provider": "gemini", "model": "gemini-test", "credential_scope": "offline-only",
            "resource_limits": {"max_physical_attempts": 10},
            "retry_policy": {"request_timeout_seconds": 120, "max_attempts": 8,
                             "max_elapsed_seconds": 1800}, "jobs": self.jobs,
        }
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.payload), encoding="utf-8")

    @staticmethod
    def success(_job, _attempt):
        return {"stop_reason": "max_iterations", "meets_spec": False,
                "iterations_completed": 2}

    def test_three_sequential_jobs_and_distinct_attempts(self):
        order = []

        def worker(job, attempt):
            order.append(job["job_id"])
            return self.success(job, attempt)

        result = run_mock_batch(self.manifest, self.output, worker)
        self.assertEqual(order, ["job_0", "job_1", "job_2"])
        self.assertEqual((result["completed"], result["performance_success"]), (3, 0))
        attempts = [row["attempts"][0]["attempt_id"] for row in status_batch("offline_batch_1", self.output)["jobs"].values()]
        self.assertEqual(len(set(attempts)), 3)

    def test_terminal_second_job_does_not_block_third(self):
        order = []

        def worker(job, attempt):
            order.append(job["job_id"])
            if job["job_id"] == "job_1":
                return {"state": "FAILED_TERMINAL", "error_code": "BAD_CONFIGURATION"}
            return self.success(job, attempt)

        result = run_mock_batch(self.manifest, self.output, worker)
        self.assertEqual(order, ["job_0", "job_1", "job_2"])
        self.assertEqual((result["completed"], result["failed_terminal"]), (2, 1))

    def test_read_only_plan_and_no_outputs(self):
        result = plan_batch(self.manifest)
        self.assertEqual(result["status"], "READY_FOR_OFFLINE_MOCK")
        self.assertFalse(self.output.exists())

    def test_restart_skips_completed(self):
        run_mock_batch(self.manifest, self.output, self.success)
        before = status_batch("offline_batch_1", self.output)
        result = run_mock_batch(self.manifest, self.output,
                                lambda *_: self.fail("completed job replayed"))
        self.assertEqual(result["completed"], 3)
        self.assertEqual(before["jobs"], status_batch("offline_batch_1", self.output)["jobs"])

    def test_mid_iteration_crash_is_uncertain_then_other_jobs_continue(self):
        def crashing(job, attempt):
            if job["job_id"] == "job_1":
                (attempt / "partial_iteration.jsonl").write_text("{", encoding="utf-8")
                raise KeyboardInterrupt()
            return self.success(job, attempt)

        with self.assertRaises(KeyboardInterrupt):
            run_mock_batch(self.manifest, self.output, crashing)
        self.assertEqual(status_batch("offline_batch_1", self.output)["jobs"]["job_1"]["state"], "RUNNING")
        called = []
        result = run_mock_batch(self.manifest, self.output,
                                lambda job, attempt: (called.append(job["job_id"]), self.success(job, attempt))[1])
        self.assertEqual(called, ["job_2"])
        self.assertEqual(result["interrupted_uncertain"], 1)
        row = status_batch("offline_batch_1", self.output)["jobs"]["job_1"]
        self.assertEqual(len(row["attempts"]), 1)
        self.assertTrue((Path(row["attempts"][0]["attempt_directory"]) / "partial_iteration.jsonl").exists())

    def test_unexpected_worker_exception_is_not_replayed(self):
        def worker(job, attempt):
            if job["job_id"] == "job_1":
                raise RuntimeError("outcome uncertain")
            return self.success(job, attempt)

        first = run_mock_batch(self.manifest, self.output, worker)
        self.assertEqual(first["interrupted_uncertain"], 1)
        again = run_mock_batch(self.manifest, self.output,
                               lambda *_: self.fail("no completed or uncertain replay"))
        self.assertEqual(again["interrupted_uncertain"], 1)

    def test_corrupted_completed_artifact_becomes_blocked(self):
        run_mock_batch(self.manifest, self.output, self.success)
        row = status_batch("offline_batch_1", self.output)["jobs"]["job_1"]
        path = Path(row["attempts"][0]["attempt_directory"]) / "result.json"
        path.write_text("{", encoding="utf-8")
        report = run_mock_batch(self.manifest, self.output,
                                lambda *_: self.fail("corrupted completion replayed"))
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(report["completed"], 2)

    def test_frozen_assets_unchanged_and_mutation_blocked(self):
        originals = {name: (self.root / name).read_bytes() for job in self.jobs for name in (
            job["config_path"], job["d0_path"], job["source_path"])}
        run_mock_batch(self.manifest, self.output, self.success)
        self.assertTrue(all((self.root / name).read_bytes() == data for name, data in originals.items()))
        (self.root / self.jobs[0]["d0_path"]).write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            plan_batch(self.manifest)

    def test_manifest_scope_and_unsupported_guards(self):
        self.payload["resource_limits"]["max_tokens"] = 100
        self.write_manifest()
        with self.assertRaises(ValueError):
            plan_batch(self.manifest)
        self.payload["resource_limits"].pop("max_tokens")
        self.jobs[0]["circuit_type"] = "VoltageReference"
        self.jobs[0]["circuit_id"] = "circuit_0490"
        self.write_manifest()
        with self.assertRaises(ValueError):
            plan_batch(self.manifest)

    def test_legacy_location_is_not_touched(self):
        legacy = self.root / "ActiveLearning" / "runs" / "legacy.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"old": true}', encoding="utf-8")
        before = sha256_file(legacy)
        run_mock_batch(self.manifest, self.output, self.success)
        self.assertEqual(sha256_file(legacy), before)

    def test_no_external_api_or_spice_in_mock_run(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network")), \
             patch("subprocess.Popen", side_effect=AssertionError("SPICE")):
            result = run_mock_batch(self.manifest, self.output, self.success)
        self.assertEqual(result["completed"], 3)

    def test_read_only_status_report(self):
        run_mock_batch(self.manifest, self.output, self.success)
        status_path = self.output / "offline_batch_1" / "status.json"
        before = sha256_file(status_path)
        self.assertEqual(report_batch("offline_batch_1", self.output)["completed"], 3)
        self.assertEqual(status_batch("offline_batch_1", self.output)["batch_id"], "offline_batch_1")
        self.assertEqual(sha256_file(status_path), before)

    def test_cli_plan_and_synthetic_mock_run(self):
        fixture = self.root / "fixtures.json"
        fixture.write_text(json.dumps({job["job_id"]: self.success(job, None)
                                       for job in self.jobs}), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["plan", "--manifest", str(self.manifest)]), 0)
            self.assertEqual(cli_main(["mock-run", "--manifest", str(self.manifest),
                                       "--output-root", str(self.output),
                                       "--fixtures", str(fixture)]), 0)
        self.assertEqual(report_batch("offline_batch_1", self.output)["completed"], 3)

    def test_preexisting_unknown_batch_directory_not_adopted(self):
        directory = self.output / "offline_batch_1"
        directory.mkdir(parents=True)
        old = directory / "research_result.json"
        old.write_text("immutable", encoding="utf-8")
        with self.assertRaises(ValueError):
            run_mock_batch(self.manifest, self.output, self.success)
        self.assertEqual(old.read_text(encoding="utf-8"), "immutable")

    def test_failure_after_worker_before_commit_is_uncertain(self):
        from ActiveLearning.batch_runner_v2 import runner

        original = runner.atomic_json

        def fail_result(path, value):
            if path.name == "result.json":
                raise OSError("simulated disk failure")
            return original(path, value)

        with patch.object(runner, "atomic_json", side_effect=fail_result):
            with self.assertRaises(OSError):
                run_mock_batch(self.manifest, self.output, self.success)
        self.assertEqual(status_batch("offline_batch_1", self.output)["jobs"]["job_0"]["state"], "RUNNING")
        calls = []
        run_mock_batch(self.manifest, self.output,
                       lambda job, attempt: (calls.append(job["job_id"]), self.success(job, attempt))[1])
        self.assertEqual(calls, ["job_1", "job_2"])

    def test_mock_worker_and_recovery_share_batch_attempt_ledger(self):
        from ActiveLearning.batch_runner_v2.gemini_recovery import GeminiRecovery

        def worker(job, attempt):
            client = GeminiRecovery(
                self.output / "offline_batch_1", provider="gemini",
                model="gemini-test", credential_scope="offline-only",
                max_physical_attempts=10, job_id=job["job_id"],
                attempt_id=attempt.name,
            )
            client.call("mock prompt", lambda *_: {"usage": {
                "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})
            return self.success(job, attempt)

        result = run_mock_batch(self.manifest, self.output, worker)
        self.assertEqual(result["api_usage"]["physical_attempts"], 3)
        self.assertEqual(result["api_usage"]["reported_total_tokens"], 9)
        self.assertFalse(result["api_usage"]["usage_unknown"])
        self.assertTrue(all(row["api_usage"]["physical_attempts"] == 1
                            for row in result["jobs"].values()))

    def test_provider_scope_block_prevents_next_job_rotation(self):
        from ActiveLearning.batch_runner_v2.gemini_recovery import GeminiRecovery

        calls = []

        class Quota(Exception):
            status_code = 429
            body = {"error": {"message": "daily quota exhausted"}}

        def worker(job, attempt):
            calls.append(job["job_id"])
            client = GeminiRecovery(
                self.output / "offline_batch_1", provider="gemini",
                model="gemini-test", credential_scope="offline-only",
                max_physical_attempts=10, job_id=job["job_id"], attempt_id=attempt.name)
            client.call("mock", lambda *_: (_ for _ in ()).throw(Quota()))
            return self.success(job, attempt)

        report = run_mock_batch(self.manifest, self.output, worker)
        self.assertEqual(calls, ["job_0"])
        self.assertEqual(report["interrupted_uncertain"], 1)
        self.assertEqual(report["blocked"], 2)


if __name__ == "__main__":
    unittest.main()
