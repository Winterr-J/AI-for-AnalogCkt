"""Offline reconciliation checks; no SPICE, LLM, D0 or AL processes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ActiveLearning.batch_runner_v2 import runner
from ActiveLearning.batch_runner_v2.storage import append_jsonl, atomic_json, read_jsonl_strict
from ActiveLearning.sc_sample_hold_generic import canonical_sha256


class VerifiedIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        verifier = patch.object(runner, "verify_job", return_value=None)
        verifier.start()
        self.addCleanup(verifier.stop)

    def manifest(self, circuit_type: str) -> dict:
        key = ("sample_hold_execution_id" if circuit_type == "SampleHold"
               else "sc_integrator_execution_id")
        return {"batch_id": "isolation_offline_1", "manifest_sha256": "d" * 64,
                "provider": "openai", "model": "offline", "credential_scope": "offline",
                "retry_policy": {"automatic_native_retries": 0},
                "resource_limits": {"per_circuit_native_attempts_max": 5},
                "jobs": [{"job_id": name, "circuit_id": name,
                          "circuit_type": circuit_type, "configuration_id": name,
                          "config_sha256": "a" * 64, "d0_sha256": "b" * 64,
                          "source_sha256": "c" * 64, "max_iterations": 5, "seed": 42,
                          key: "native/" + name}
                         for name in ("job_0", "job_1", "job_2")]}

    def evidence(self, manifest: dict, job: dict, attempt: Path, *,
                 reaped: bool = True, closed: bool = True, count: int = 1) -> None:
        atomic_json(attempt / "child_spawn_intent.json", {
            "manifest_sha256": manifest["manifest_sha256"],
            "job_id": job["job_id"], "attempt_id": attempt.name})
        if reaped:
            atomic_json(attempt / "child_reaped.json", {
                "attempt_id": attempt.name, "exit_confirmed": True, "exit_code": 0})
        execution_id = next(value for name, value in job.items()
                            if name.endswith("_execution_id"))
        native = self.output / manifest["batch_id"] / execution_id
        native.mkdir(parents=True, exist_ok=True)
        identity = {"circuit_id": job["circuit_id"], "stage": "al", "limit": 5,
                    "extra": {"batch_id": manifest["batch_id"],
                              "config_sha256": job["config_sha256"],
                              "d0_sha256": job["d0_sha256"]}}
        atomic_json(native / "identity.json", identity)
        digest = canonical_sha256(identity)
        for number in range(1, count + 1):
            append_jsonl(native / "physical_attempt_ledger.jsonl", {
                "event": "reserved", "attempt": number,
                "vector_sha256": f"vector-{number}", "identity_sha256": digest})
            if closed:
                append_jsonl(native / "physical_attempt_ledger.jsonl", {
                    "event": "finished", "attempt": number,
                    "vector_sha256": f"vector-{number}", "outcome": "complete"})

    @staticmethod
    def success() -> dict:
        return {"stop_reason": "max_iterations", "meets_spec": False,
                "parameter_valid": True, "simulation_valid": True,
                "functional_valid": True, "iterations_completed": 5,
                "llm_calls": 1, "total_tokens": {"total_tokens": 10},
                "total_timing": {"total_seconds": 1}}

    def test_fresh_failures_isolate_and_completed_jobs_never_replay(self):
        for circuit_type in ("SampleHold", "SCIntegrator"):
            with self.subTest(circuit_type=circuit_type):
                with tempfile.TemporaryDirectory() as directory:
                    self.output = Path(directory)
                    manifest = self.manifest(circuit_type)
                    calls = []

                    def worker(job, attempt):
                        calls.append(job["job_id"])
                        self.evidence(manifest, job, attempt)
                        if job["job_id"] == "job_0":
                            return {"state": "FAILED_TERMINAL", "error_code": "CHILD_EXIT_7"}
                        if job["job_id"] == "job_1":
                            return {"stop_reason": "not_a_valid_stop"}
                        return self.success()

                    first = runner._run_batch(manifest, self.output, worker,
                                              offline_only=False,
                                              execution_scope=("sample_hold_production"
                                                               if circuit_type == "SampleHold"
                                                               else "sc_integrator_production"))
                    self.assertEqual(calls, ["job_0", "job_1", "job_2"])
                    self.assertEqual(first["jobs"]["job_0"]["error_code"], "CHILD_EXIT_7")
                    self.assertEqual(first["jobs"]["job_1"]["error_code"],
                                     "INVALID_WORKER_RESULT")
                    self.assertTrue(first["jobs"]["job_1"]["isolation_verified"])
                    self.assertEqual(first["jobs"]["job_2"]["state"], "COMPLETED")
                    runner._run_batch(manifest, self.output, worker, offline_only=False,
                                      execution_scope=("sample_hold_production"
                                                       if circuit_type == "SampleHold"
                                                       else "sc_integrator_production"))
                    self.assertEqual(calls, ["job_0", "job_1", "job_2"])
                    events = read_jsonl_strict(self.output / manifest["batch_id"] /
                                               "events.jsonl")
                    self.assertEqual(sum(row["state"] == "ISOLATED_VERIFIED"
                                         for row in events), 2)

    def test_old_invalid_result_reconciles_only_after_reap_evidence(self):
        manifest = self.manifest("SCIntegrator")
        calls = []

        def worker(job, attempt):
            calls.append(job["job_id"])
            self.evidence(manifest, job, attempt, reaped=job["job_id"] != "job_1")
            return {"stop_reason": "invalid"} if job["job_id"] == "job_1" else self.success()

        first = runner._run_batch(manifest, self.output, worker, offline_only=False,
                                  execution_scope="sc_integrator_production")
        self.assertEqual(first["jobs"]["job_2"]["state"], "PENDING")
        row = runner.status_batch(manifest["batch_id"], self.output)["jobs"]["job_1"]
        attempt = Path(row["attempts"][0]["attempt_directory"])
        atomic_json(attempt / "child_reaped.json", {
            "attempt_id": attempt.name, "exit_confirmed": True, "exit_code": 0})
        resumed = runner._run_batch(manifest, self.output, worker, offline_only=False,
                                    execution_scope="sc_integrator_production")
        self.assertEqual(calls, ["job_0", "job_1", "job_2"])
        self.assertEqual(resumed["jobs"]["job_1"]["error_code"], "INVALID_WORKER_RESULT")
        self.assertEqual(resumed["jobs"]["job_2"]["state"], "COMPLETED")

    def test_unclosed_native_attempt_stops_following_jobs(self):
        manifest = self.manifest("SampleHold")
        calls = []

        def worker(job, attempt):
            calls.append(job["job_id"])
            self.evidence(manifest, job, attempt, closed=job["job_id"] != "job_1")
            return {"state": "FAILED_TERMINAL", "error_code": "CHILD_EXIT_8"}

        first = runner._run_batch(manifest, self.output, worker, offline_only=False,
                                  execution_scope="sample_hold_production")
        self.assertEqual(calls, ["job_0", "job_1"])
        self.assertEqual(first["jobs"]["job_2"]["state"], "PENDING")
        runner._run_batch(manifest, self.output, worker, offline_only=False,
                          execution_scope="sample_hold_production")
        self.assertEqual(calls, ["job_0", "job_1"])

    def test_native_budget_overflow_stops_following_jobs(self):
        manifest = self.manifest("SCIntegrator")
        calls = []

        def worker(job, attempt):
            calls.append(job["job_id"])
            self.evidence(manifest, job, attempt,
                          count=6 if job["job_id"] == "job_0" else 1)
            return {"state": "FAILED_TERMINAL", "error_code": "CHILD_EXIT_8"}

        result = runner._run_batch(manifest, self.output, worker, offline_only=False,
                                   execution_scope="sc_integrator_production")
        self.assertEqual(calls, ["job_0"])
        self.assertEqual(result["jobs"]["job_1"]["state"], "PENDING")

    def test_unverified_orphan_fails_closed_until_reap_proven(self):
        manifest = self.manifest("SCIntegrator")
        calls = []

        def orphan(job, attempt):
            calls.append(job["job_id"])
            self.evidence(manifest, job, attempt, reaped=False)
            raise runner.UnverifiedOrphanError("unconfirmed group")

        first = runner._run_batch(manifest, self.output, orphan, offline_only=False,
                                  execution_scope="sc_integrator_production")
        self.assertEqual(first["jobs"]["job_0"]["error_code"], "ORPHAN_UNVERIFIED")
        with self.assertRaises(runner.UnverifiedOrphanError):
            runner._run_batch(manifest, self.output, orphan, offline_only=False,
                              execution_scope="sc_integrator_production")
        self.assertEqual(calls, ["job_0"])
        row = runner.status_batch(manifest["batch_id"], self.output)["jobs"]["job_0"]
        attempt = Path(row["attempts"][0]["attempt_directory"])
        atomic_json(attempt / "child_reaped.json", {
            "attempt_id": attempt.name, "exit_confirmed": True, "exit_code": 130})

        def resumed(job, attempt):
            calls.append(job["job_id"])
            self.evidence(manifest, job, attempt)
            return self.success()

        result = runner._run_batch(manifest, self.output, resumed, offline_only=False,
                                   execution_scope="sc_integrator_production")
        self.assertEqual(calls, ["job_0", "job_1", "job_2"])
        self.assertEqual(result["jobs"]["job_0"]["error_code"], "ORPHAN_UNVERIFIED")

    def test_legacy_abandoned_orphan_does_not_require_new_invalid_job_override(self):
        manifest = self.manifest("SampleHold")

        def legacy_orphan(job, _attempt):
            self.assertEqual(job["job_id"], "job_0")
            raise runner.UnverifiedOrphanError("legacy reviewed orphan")

        runner._run_batch(manifest, self.output, legacy_orphan, offline_only=False,
                          execution_scope="sample_hold_production")
        calls = []

        def resumed(job, attempt):
            calls.append(job["job_id"])
            self.evidence(manifest, job, attempt)
            return ({"stop_reason": "invalid"} if job["job_id"] == "job_1"
                    else self.success())

        result = runner._run_batch(manifest, self.output, resumed, offline_only=False,
                                   execution_scope="sample_hold_production",
                                   abandoned_orphan_job_ids={"job_0"})
        self.assertEqual(calls, ["job_1", "job_2"])
        self.assertEqual(result["jobs"]["job_0"]["error_code"], "ORPHAN_UNVERIFIED")
        self.assertEqual(result["jobs"]["job_1"]["error_code"],
                         "INVALID_WORKER_RESULT")
        self.assertTrue(result["jobs"]["job_1"]["isolation_verified"])
        self.assertEqual(result["jobs"]["job_2"]["state"], "COMPLETED")

    def test_ctrl_c_with_persisted_reap_reconciles_without_replaying(self):
        manifest = self.manifest("SCIntegrator")
        calls = []

        def interrupted(job, attempt):
            calls.append(job["job_id"])
            self.evidence(manifest, job, attempt)
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            runner._run_batch(manifest, self.output, interrupted, offline_only=False,
                              execution_scope="sc_integrator_production")
        self.assertEqual(calls, ["job_0"])
        resumed = runner._run_batch(manifest, self.output,
                                    lambda job, _attempt: (calls.append(job["job_id"]),
                                                           self.success())[1],
                                    offline_only=False,
                                    execution_scope="sc_integrator_production")
        self.assertEqual(calls, ["job_0", "job_1", "job_2"])
        self.assertEqual(resumed["jobs"]["job_0"]["state"], "INTERRUPTED_UNCERTAIN")
        self.assertEqual(resumed["jobs"]["job_0"]["error_code"], "LOST_AL_CALL_STACK")

    def test_ctrl_c_without_reap_evidence_stays_fail_closed(self):
        manifest = self.manifest("SampleHold")
        calls = []

        def interrupted(job, attempt):
            calls.append(job["job_id"])
            self.evidence(manifest, job, attempt, reaped=False)
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            runner._run_batch(manifest, self.output, interrupted, offline_only=False,
                              execution_scope="sample_hold_production")
        resumed = runner._run_batch(manifest, self.output, interrupted,
                                    offline_only=False,
                                    execution_scope="sample_hold_production")
        self.assertEqual(resumed["jobs"]["job_0"]["state"], "INTERRUPTED_UNCERTAIN")
        self.assertEqual(resumed["jobs"]["job_1"]["state"], "PENDING")
        self.assertEqual(calls, ["job_0"])
        with self.assertRaises(runner.UnverifiedOrphanError):
            runner._run_batch(manifest, self.output, interrupted, offline_only=False,
                              execution_scope="sample_hold_production")

if __name__ == "__main__":
    unittest.main()
