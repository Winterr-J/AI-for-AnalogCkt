"""B2A authorization and real-worker integration tests; no external calls."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ActiveLearning.batch_runner_v2.authorization import (
    AuthorizationError, ProductionExecutionDisabledError,
    authorization_identity, claim_authorization, current_source_revision,
    validate_authorization,
)
from ActiveLearning.batch_runner_v2.real_al_child import (
    _production_run, execute_invocation,
)
from ActiveLearning.batch_runner_v2.runner import (
    plan_batch, run_authorized_batch, status_batch,
)
from ActiveLearning.batch_runner_v2.schema import (
    _REQUIRED_REAL_AL_CLOSURE, canonical_sha256, load_manifest,
)
from ActiveLearning.batch_runner_v2.storage import atomic_json, sha256_file


class B2AIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code_root = Path(__file__).resolve().parents[1]
        cls.revision = current_source_revision(cls.code_root)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "outputs"
        (self.root / "cfg" / "d0").mkdir(parents=True)
        (self.root / "src").mkdir()
        self.jobs = [self._job(number) for number in range(3)]
        self.manifest_path = self.root / "batch.json"
        self._write_manifest()
        self.authorization_path = self.root / "authorization.json"
        self.authorization_sha = self._write_authorization("offline_integration")

    def _job(self, number, *, circuit_type="OpAmp", circuit_id=None):
        circuit_id = circuit_id or f"circuit_{number:04d}"
        config = self.root / "cfg" / f"{circuit_id}.json"
        d0 = self.root / "cfg" / "d0" / f"{circuit_id}.jsonl"
        source = self.root / "src" / f"{circuit_id}.py"
        target_specs = {"gain": {"operator": ">=", "value": 1.0}}
        config.write_text(json.dumps({
            "configuration_id": f"frozen_{number}",
            "active_learning": {"max_iterations": 2, "data_expansion_seed": 42},
            "data": {"d0_path_relative_to": "config",
                     "d0_path": f"d0/{circuit_id}.jsonl",
                     "initialize_if_missing": False},
            "target_specs": target_specs,
        }), encoding="utf-8")
        d0.write_text(json.dumps({
            "sample_id": "d0_0", "data_source": "independent_d0_new_evaluation",
            "params": {"x": 1}, "metrics": {"gain": 0.5},
        }) + "\n", encoding="utf-8")
        source.write_text("# frozen synthetic topology\n", encoding="utf-8")
        return {
            "job_id": f"job_{number}", "circuit_type": circuit_type,
            "circuit_id": circuit_id, "configuration_id": f"frozen_{number}",
            "config_path": f"cfg/{circuit_id}.json",
            "config_sha256": sha256_file(config),
            "d0_path": f"cfg/d0/{circuit_id}.jsonl", "d0_sha256": sha256_file(d0),
            "source_path": f"src/{circuit_id}.py", "source_sha256": sha256_file(source),
            "target_spec_sha256": canonical_sha256(target_specs),
            "max_iterations": 2, "seed": 42, "execution_mode": "al_only",
        }

    def _source_closure(self):
        return {name: sha256_file(self.code_root / name)
                for name in sorted(_REQUIRED_REAL_AL_CLOSURE)}

    def _write_manifest(self, *, cap=10):
        atomic_json(self.manifest_path, {
            "schema_version": "batch_runner_v2b2a_candidate_v1",
            "batch_id": "b2a_offline", "provider": "gemini",
            "model": "gemini-test", "credential_scope": "offline-only",
            "resource_limits": {"max_physical_attempts": cap,
                                "worker_timeout_seconds": 10},
            "retry_policy": {"request_timeout_seconds": 120, "max_attempts": 8,
                             "max_elapsed_seconds": 1800},
            "source_revision": self.revision,
            "source_closure": self._source_closure(), "jobs": self.jobs,
        })

    def _write_authorization(self, scope, *, overrides=None):
        manifest = load_manifest(self.manifest_path)
        approval = {
            "schema_version": "batch_runner_v2b2a_authorization_v1",
            "authorization_id": f"approval_{scope}",
            "execution_scope": scope, "approved": True,
            "approved_by": "offline-test", "approved_at": "2026-10-17T00:00:00Z",
            **authorization_identity(manifest, self.output),
        }
        approval.update(overrides or {})
        atomic_json(self.authorization_path, approval)
        return sha256_file(self.authorization_path)

    def test_authorization_binds_manifest_revision_assets_and_limits(self):
        manifest = load_manifest(self.manifest_path)
        approval = validate_authorization(
            manifest, self.authorization_path, self.authorization_sha, self.output)
        self.assertEqual(approval["manifest_sha256"], manifest["manifest_sha256"])
        self.assertEqual(approval["source_revision"], self.revision)
        self.assertEqual(len(approval["jobs"]), 3)
        self.assertEqual(plan_batch(self.manifest_path)["status"],
                         "READY_FOR_AUTHORIZATION")

    def test_job_deadline_r8_schema_is_explicit_and_r5_remains_valid(self):
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        payload["resource_limits"].update(
            max_physical_attempts=120, per_job_physical_attempt_cap=60,
            worker_timeout_seconds=4800)
        payload["retry_policy"].update(
            rate_limit_mode="job_deadline", request_timeout_seconds=120,
            max_elapsed_seconds=1800)
        payload["source_closure"]["ActiveLearning/batch_runner_v2/time_recovery.py"] = (
            sha256_file(self.code_root / "ActiveLearning/batch_runner_v2/time_recovery.py"))
        for attempts in (5, 8):
            with self.subTest(max_attempts=attempts):
                payload["retry_policy"]["max_attempts"] = attempts
                atomic_json(self.manifest_path, payload)
                loaded = load_manifest(self.manifest_path)
                self.assertEqual(loaded["retry_policy"]["max_attempts"], attempts)
        payload["retry_policy"]["max_attempts"] = 13
        atomic_json(self.manifest_path, payload)
        with self.assertRaisesRegex(ValueError, "approved first-version time bounds"):
            load_manifest(self.manifest_path)

    def test_missing_or_semantically_mismatched_approval_fails_before_output(self):
        with self.assertRaises(AuthorizationError):
            run_authorized_batch(
                self.manifest_path, self.output, self.authorization_path, "0" * 64,
                offline_scenarios={job["job_id"]: "success" for job in self.jobs})
        self.assertFalse(self.output.exists())
        bad_sha = self._write_authorization(
            "offline_integration", overrides={"model": "other-model"})
        with self.assertRaisesRegex(AuthorizationError, "model"):
            run_authorized_batch(
                self.manifest_path, self.output, self.authorization_path, bad_sha,
                offline_scenarios={job["job_id"]: "success" for job in self.jobs})
        self.assertFalse(self.output.exists())

    def test_production_scope_remains_disabled_without_explicit_enable(self):
        production_sha = self._write_authorization("production")
        with self.assertRaises(ProductionExecutionDisabledError):
            run_authorized_batch(
                self.manifest_path, self.output, self.authorization_path, production_sha)
        self.assertFalse(self.output.exists())

    def test_authorization_cannot_move_to_another_output_root(self):
        other_output = self.root / "other_outputs"
        with self.assertRaisesRegex(AuthorizationError, "output_root"):
            run_authorized_batch(
                self.manifest_path, other_output, self.authorization_path,
                self.authorization_sha,
                offline_scenarios={job["job_id"]: "success" for job in self.jobs})
        self.assertFalse(other_output.exists())

    def test_child_cannot_run_outside_registered_scheduler_attempt(self):
        manifest = load_manifest(self.manifest_path)
        approval = validate_authorization(
            manifest, self.authorization_path, self.authorization_sha, self.output)
        claim_authorization(approval)
        attempt = (self.output / manifest["batch_id"] / "jobs" / "job_0" /
                   "attempts" / ("b" * 32))
        attempt.mkdir(parents=True)
        invocation = attempt / "real_al_invocation.json"
        atomic_json(invocation, {
            "schema_version": "batch_runner_v2b2a_invocation_v1",
            "batch_id": manifest["batch_id"], "job_id": "job_0",
            "attempt_id": attempt.name, "manifest_path": manifest["manifest_path"],
            "manifest_sha256": manifest["manifest_sha256"],
            "authorization_id": approval["authorization_id"],
            "authorization_sha256": approval["authorization_sha256"],
            "attempt_directory": str(attempt), "offline_scenario": "success",
        })
        with self.assertRaisesRegex(PermissionError, "scheduler state"):
            execute_invocation(
                invocation, self.authorization_path, self.authorization_sha)

    def test_three_job_worker_429_terminal_continue_and_restart(self):
        scenarios = {"job_0": "success", "job_1": "429_success",
                     "job_2": "auth_terminal"}
        report = run_authorized_batch(
            self.manifest_path, self.output, self.authorization_path,
            self.authorization_sha, offline_scenarios=scenarios)
        self.assertEqual((report["completed"], report["failed_terminal"]), (2, 1))
        self.assertEqual(report["api_usage"]["physical_attempts"], 4)
        self.assertTrue(report["api_usage"]["usage_unknown"])
        self.assertEqual(report["jobs"]["job_1"]["final_metrics"], {"gain": 1.0})
        self.assertEqual(report["jobs"]["job_1"]["d0_record_count"], 1)
        self.assertEqual(status_batch("b2a_offline", self.output)["jobs"]["job_2"]["state"],
                         "FAILED_TERMINAL")
        with patch("ActiveLearning.batch_runner_v2.al_process.run_real_al_child",
                   side_effect=AssertionError("completed or terminal job replayed")):
            restarted = run_authorized_batch(
                self.manifest_path, self.output, self.authorization_path,
                self.authorization_sha, offline_scenarios=scenarios)
        self.assertEqual((restarted["completed"], restarted["failed_terminal"]), (2, 1))
        self.assertEqual(restarted["api_usage"]["physical_attempts"], 4)

    def test_confirmed_terminal_child_does_not_block_later_job(self):
        report = run_authorized_batch(
            self.manifest_path, self.output, self.authorization_path,
            self.authorization_sha,
            offline_scenarios={"job_0": "success", "job_1": "auth_terminal",
                               "job_2": "success"})
        self.assertEqual((report["completed"], report["failed_terminal"]), (2, 1))
        states = status_batch("b2a_offline", self.output)["jobs"]
        self.assertEqual(states["job_2"]["state"], "COMPLETED")

    def test_uncertain_child_stops_batch_and_is_not_replayed(self):
        self.jobs = self.jobs[:2]
        self._write_manifest()
        self.authorization_sha = self._write_authorization("offline_integration")
        scenarios = {"job_0": "uncertain", "job_1": "success"}
        report = run_authorized_batch(
            self.manifest_path, self.output, self.authorization_path,
            self.authorization_sha, offline_scenarios=scenarios)
        self.assertEqual((report["interrupted_uncertain"], report["completed"]), (1, 0))
        self.assertEqual(report["jobs"]["job_1"]["state"], "PENDING")
        self.assertTrue(report["api_usage"]["usage_unknown"])
        with patch("ActiveLearning.batch_runner_v2.al_process.run_real_al_child",
                   side_effect=AssertionError("uncertain attempt replayed")):
            restarted = run_authorized_batch(
                self.manifest_path, self.output, self.authorization_path,
                self.authorization_sha, offline_scenarios=scenarios)
        self.assertEqual((restarted["interrupted_uncertain"], restarted["completed"]), (1, 0))

    def test_missing_or_changed_asset_fails_before_worker_or_output(self):
        (self.root / self.jobs[0]["d0_path"]).write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "asset hash mismatch"):
            run_authorized_batch(
                self.manifest_path, self.output, self.authorization_path,
                self.authorization_sha,
                offline_scenarios={job["job_id"]: "success" for job in self.jobs})
        self.assertFalse(self.output.exists())

    def test_physical_attempt_cap_cannot_be_bypassed_by_retry(self):
        self.jobs = self.jobs[:1]
        self._write_manifest(cap=1)
        self.authorization_sha = self._write_authorization("offline_integration")
        report = run_authorized_batch(
            self.manifest_path, self.output, self.authorization_path,
            self.authorization_sha, offline_scenarios={"job_0": "429_success"})
        self.assertEqual(report["api_usage"]["physical_attempts"], 1)
        self.assertEqual(report["blocked"], 1)
        self.assertTrue(report["api_usage"]["usage_unknown"])

    def test_source_closure_and_target_hash_fail_closed(self):
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        payload["source_closure"]["ActiveLearning/main.py"] = "0" * 64
        atomic_json(self.manifest_path, payload)
        with self.assertRaisesRegex(ValueError, "asset hash mismatch"):
            load_manifest(self.manifest_path)
        self._write_manifest()
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        payload["jobs"][0]["target_spec_sha256"] = "0" * 64
        atomic_json(self.manifest_path, payload)
        with self.assertRaisesRegex(ValueError, "performance specification"):
            load_manifest(self.manifest_path)
        self.assertFalse(self.output.exists())

    def test_ag626_one_time_guard_is_not_bypassed(self):
        self.jobs = [self._job(9, circuit_type="VoltageReference",
                               circuit_id="circuit_0490")]
        self._write_manifest()
        with self.assertRaisesRegex(ValueError, "one-time AL authorization"):
            load_manifest(self.manifest_path)

    def test_production_adapter_wires_frozen_inputs_and_dedicated_output(self):
        manifest = load_manifest(self.manifest_path)
        production_sha = self._write_authorization("production")
        approval = validate_authorization(
            manifest, self.authorization_path, production_sha, self.output)
        job = manifest["jobs"][0]
        attempt = (self.root / "direct_batch" / "jobs" / job["job_id"] /
                   "attempts" / ("a" * 32))
        attempt.mkdir(parents=True)
        invocation = {"batch_id": manifest["batch_id"], "job_id": job["job_id"],
                      "attempt_id": attempt.name}
        captured = {}

        def fake_run(circuit_type, circuit_id, **kwargs):
            captured.update({"circuit_type": circuit_type, "circuit_id": circuit_id,
                             **kwargs})
            run_dir = Path(kwargs["runs_root"]) / circuit_type / circuit_id / "run_1"
            run_dir.mkdir(parents=True)
            kwargs["run_context_callback"]({"run_id": "run_1", "run_directory": run_dir})
            atomic_json(run_dir / "summary.json", {"run_id": "run_1"})
            return {"iterations_completed": 1, "stop_reason": "meets_spec",
                    "records": [{"sample_id": "d0"}],
                    "best_or_final_record": {"status": {"simulation_valid": True,
                                                          "meets_spec": True},
                                             "metrics": {"gain": 1.5}}}

        config_path = (self.root / job["config_path"]).resolve()
        source_path = (self.root / job["source_path"]).resolve()
        with patch.dict(os.environ, {
                "BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256": production_sha}), \
             patch("ActiveLearning.main._resolve_paths",
                   return_value={"topology": source_path}), \
             patch("ActiveLearning.main.run_configured_active_learning",
                   side_effect=fake_run), \
             patch("ActiveLearning.batch_runner_v2.real_al_child.build_gemini_chat_transport",
                   return_value=lambda *_: None):
            result = _production_run(invocation, manifest, job, approval, attempt)
        self.assertEqual((result["simulation_valid"], result["meets_spec"]), (True, True))
        self.assertEqual(result["final_metrics"], {"gain": 1.5})
        self.assertEqual(Path(captured["config_path"]).resolve(), config_path)
        self.assertTrue(captured["require_existing_d0"])
        self.assertEqual(captured["expected_config_sha256"], job["config_sha256"])
        self.assertEqual(captured["expected_d0_sha256"], job["d0_sha256"])
        self.assertTrue(Path(captured["runs_root"]).resolve().is_relative_to(attempt))

    def test_current_mirror_nonempty_functional_config_full_mock_chain(self):
        from ActiveLearning.onboarding import build_active_learning_circuit_config

        code_root = self.code_root
        runtime = build_active_learning_circuit_config(
            "CurrentMirror", "circuit_0472", repository_root=code_root,
        )
        variables = set(runtime.param_template["optimization_space"]["variables"])
        params = {
            item["name"]: item["nominal_value"]
            for item in runtime.param_template["parameters"]
            if item["name"] in variables
        }
        functional_config = {
            "compliance_vout_start": 0.4,
            "compliance_vout_stop": 2.2,
            "compliance_points": 7,
            "reference_scales": [0.75, 1.0, 1.25],
        }
        with tempfile.TemporaryDirectory(
                prefix=".cm_batch_mock_", dir=code_root) as directory:
            fixture = Path(directory)
            config_path = fixture / "active_learning_config.json"
            d0_path = fixture / "d0.jsonl"
            targets = {
                "current_error": {"operator": "<=", "value": 0.02},
                "output_resistance": {"operator": ">=", "value": 26e6},
            }
            config_path.write_text(json.dumps({
                "configuration_id": "cm0472_joint_mock_v1",
                "circuit_info": {
                    "circuit_type": "CurrentMirror",
                    "circuit_id": "circuit_0472",
                },
                "target_specs": targets,
                "active_learning": {
                    "max_iterations": 1,
                    "data_expansion_seed": 42,
                },
                "validity": {"enabled": False},
                "data": {
                    "d0_mode": "reuse",
                    "d0_path_relative_to": "config",
                    "d0_path": d0_path.name,
                    "initialize_if_missing": False,
                    "functional_config": functional_config,
                },
            }), encoding="utf-8")
            d0_path.write_text(json.dumps({
                "sample_id": "d0_0",
                "params": params,
                "normalized_params": {name: 0.5 for name in params},
                "metrics": {
                    "current_error": 0.5,
                    "output_resistance": 1e3,
                },
                "status": {
                    "parameter_valid": True,
                    "simulation_valid": True,
                    "functional_valid": False,
                    "meets_spec": False,
                },
            }) + "\n", encoding="utf-8")
            source_path = (
                code_root / "dataset/circuit_0472/topology/circuit_0472.py"
            )
            job = {
                "job_id": "cm_job",
                "circuit_type": "CurrentMirror",
                "circuit_id": "circuit_0472",
                "configuration_id": "cm0472_joint_mock_v1",
                "config_path": config_path.relative_to(code_root).as_posix(),
                "config_sha256": sha256_file(config_path),
                "d0_path": d0_path.relative_to(code_root).as_posix(),
                "d0_sha256": sha256_file(d0_path),
                "source_path": source_path.relative_to(code_root).as_posix(),
                "source_sha256": sha256_file(source_path),
                "target_spec_sha256": canonical_sha256(targets),
                "max_iterations": 1,
                "seed": 42,
                "execution_mode": "al_only",
            }
            manifest = {
                "batch_id": "cm_batch_mock",
                "manifest_sha256": "a" * 64,
                "source_revision": self.revision,
                "repository_root": str(code_root),
                "code_root": str(code_root),
                "provider": "gemini",
                "model": "gemini-test",
                "credential_scope": "offline-only",
                "resource_limits": {
                    "max_physical_attempts": 10,
                    "worker_timeout_seconds": 10,
                },
                "retry_policy": {
                    "request_timeout_seconds": 5,
                    "max_attempts": 1,
                    "max_elapsed_seconds": 5,
                },
            }
            approval = {
                "authorization_id": "cm_mock_approval",
                "authorization_sha256": "b" * 64,
            }
            attempt = (
                self.root / "cm_direct_batch" / "jobs" / job["job_id"]
                / "attempts" / ("c" * 32)
            )
            attempt.mkdir(parents=True)
            invocation = {
                "batch_id": manifest["batch_id"],
                "job_id": job["job_id"],
                "attempt_id": attempt.name,
            }
            candidate = {
                "candidate_id": "candidate_0000",
                "params": params,
                "normalized_params": {name: 0.5 for name in params},
            }
            selected = {
                "candidate_id": candidate["candidate_id"],
                "params": params,
                "violation_score": 0.0,
                "uncertainty_score": 0.0,
                "acquisition_score": 0.0,
                "metric_scores": {},
            }
            prediction = {
                "candidate_id": candidate["candidate_id"],
                "params": params,
                "prediction": {
                    "current_error": {"mean": 0.01, "std": 0.0},
                    "output_resistance": {"mean": 30e6, "std": 0.0},
                },
            }
            observed_functional_config = []
            functional_outcomes = [True, False]

            def functional_validator(_params, *, adapter, **kwargs):
                self.assertIsInstance(adapter, type(runtime.adapter))
                self.assertEqual(adapter.name, runtime.adapter.name)
                observed_functional_config.append(kwargs)
                valid = functional_outcomes.pop(0)
                return {
                    "functional_feasible": valid,
                    "functional_status": "valid" if valid else "invalid",
                    "failure_reasons": [] if valid else ["mock compliance failure"],
                    "basic_behavior": {"mirror_response_valid": valid},
                    "compliance": {"relative_variation": 0.02},
                    "reference_tracking": {"max_tracking_error": 0.03},
                }

            with patch.dict(os.environ, {
                    "BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256":
                    approval["authorization_sha256"]}), \
                 patch(
                    "ActiveLearning.batch_runner_v2.real_al_child."
                    "build_gemini_chat_transport",
                    return_value=lambda *_: self.fail("provider transport called"),
                 ), \
                 patch(
                    "ActiveLearning.active_learning.propose_inverse_design_seed",
                    return_value={
                        "x_seed": params, "inverse_context": [],
                        "parameter_valid": True,
                    },
                 ), \
                 patch(
                    "ActiveLearning.active_learning.expand_candidates",
                    return_value=([candidate], {
                        "filtered_historical_duplicates": 0,
                        "filtered_within_pool_duplicates": 0,
                    }),
                 ), \
                 patch(
                    "ActiveLearning.active_learning.predict_candidate_performance",
                    return_value=[prediction],
                 ), \
                 patch(
                    "ActiveLearning.active_learning.select_next_candidate",
                    return_value=(selected, [selected]),
                 ), \
                 patch(
                    "ActiveLearning.real_evaluation.real_evaluator."
                    "evaluate_current_mirror",
                    return_value=(0.01, 30e6, True),
                 ), \
                 patch(
                    "ActiveLearning.real_evaluation.real_evaluator."
                    "check_current_mirror_functionality",
                    side_effect=functional_validator,
                 ), \
                 patch(
                    "socket.socket.connect",
                    side_effect=AssertionError("network call"),
                 ), \
                 patch(
                    "subprocess.Popen",
                    side_effect=AssertionError("external process"),
                 ):
                result = _production_run(
                    invocation, manifest, job, approval, attempt,
                )
                numeric_attempt = (
                    self.root / "cm_direct_batch" / "jobs" / job["job_id"]
                    / "attempts" / ("d" * 32)
                )
                numeric_attempt.mkdir(parents=True)
                numeric_invocation = {**invocation, "attempt_id": numeric_attempt.name}
                numeric_only = _production_run(
                    numeric_invocation, manifest, job, approval, numeric_attempt,
                )
            self.assertEqual(observed_functional_config,
                             [functional_config, functional_config])
            self.assertIs(result["parameter_valid"], True)
            self.assertIs(result["simulation_valid"], True)
            self.assertIs(result["functional_valid"], True)
            self.assertIs(result["meets_spec"], True)
            self.assertIs(result["joint_success"], True)
            self.assertIs(result["numerical_spec_success"], True)
            self.assertIs(result["functional_joint_success"], True)
            self.assertIs(result["functional_unverified"], False)
            summary = json.loads(
                Path(result["result_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["success_schema_version"],
                             "al_success_status_v2")
            self.assertIs(summary["joint_success"], True)
            self.assertIs(numeric_only["parameter_valid"], True)
            self.assertIs(numeric_only["simulation_valid"], True)
            self.assertIs(numeric_only["functional_valid"], False)
            self.assertIs(numeric_only["meets_spec"], True)
            self.assertIs(numeric_only["joint_success"], False)
            self.assertIs(numeric_only["numerical_spec_success"], True)
            self.assertIs(numeric_only["functional_joint_success"], False)
            self.assertIs(numeric_only["functional_unverified"], False)
            self.assertEqual(numeric_only["stop_reason"], "max_iterations")
            numeric_summary = json.loads(
                Path(numeric_only["result_path"]).read_text(encoding="utf-8")
            )
            self.assertIs(numeric_summary["functional_valid"], False)
            self.assertIs(numeric_summary["joint_success"], False)


    def test_optin_offline_child_proves_safe_429_deadline(self):
        self.jobs = self.jobs[:1]
        self._write_manifest()
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        payload["resource_limits"]["per_job_physical_attempt_cap"] = 3
        payload["retry_policy"]["rate_limit_mode"] = "job_deadline"
        payload["retry_policy"]["max_attempts"] = 5
        name = "ActiveLearning/batch_runner_v2/time_recovery.py"
        payload["source_closure"][name] = sha256_file(self.code_root / name)
        atomic_json(self.manifest_path, payload)
        self.authorization_sha = self._write_authorization("offline_integration")
        report = run_authorized_batch(
            self.manifest_path, self.output, self.authorization_path,
            self.authorization_sha, offline_scenarios={"job_0": "429_deadline"})
        self.assertEqual(report["skipped_job_timeout"], 1)
        self.assertEqual(report["completed"], 0)
        self.assertEqual(report["api_usage"]["physical_attempts"], 1)
        self.assertIsNone(report["jobs"]["job_0"]["meets_spec"])
        state = status_batch("b2a_offline", self.output)["jobs"]["job_0"]
        attempt = Path(state["attempts"][0]["attempt_directory"])
        self.assertTrue((attempt / "child_reaped.json").is_file())
        self.assertEqual(json.loads((attempt / "worker_result.json").read_text())["safe_point"],
                         "rate_limit_wait")


    def test_optin_offline_child_proves_per_job_cap_stop(self):
        self.jobs = self.jobs[:1]
        self._write_manifest()
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        payload["resource_limits"]["per_job_physical_attempt_cap"] = 1
        payload["retry_policy"]["rate_limit_mode"] = "job_deadline"
        payload["retry_policy"]["max_attempts"] = 5
        name = "ActiveLearning/batch_runner_v2/time_recovery.py"
        payload["source_closure"][name] = sha256_file(self.code_root / name)
        atomic_json(self.manifest_path, payload)
        self.authorization_sha = self._write_authorization("offline_integration")
        report = run_authorized_batch(
            self.manifest_path, self.output, self.authorization_path,
            self.authorization_sha, offline_scenarios={"job_0": "job_budget"})
        self.assertEqual(report["skipped_job_budget"], 1)
        self.assertEqual(report["api_usage"]["physical_attempts"], 1)

    def test_optin_schema_requires_closure_and_bounded_time(self):
        self.jobs = self.jobs[:1]
        self._write_manifest()
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        payload["resource_limits"]["per_job_physical_attempt_cap"] = 3
        atomic_json(self.manifest_path, payload)
        with self.assertRaisesRegex(ValueError, "per-job physical cap requires"):
            load_manifest(self.manifest_path)
        payload["retry_policy"]["rate_limit_mode"] = "job_deadline"
        payload["retry_policy"]["max_attempts"] = 5
        atomic_json(self.manifest_path, payload)
        with self.assertRaisesRegex(ValueError, "time-driven source closure"):
            load_manifest(self.manifest_path)
        name = "ActiveLearning/batch_runner_v2/time_recovery.py"
        payload["source_closure"][name] = sha256_file(self.code_root / name)
        atomic_json(self.manifest_path, payload)
        self.assertEqual(load_manifest(self.manifest_path)["retry_policy"]["max_attempts"], 5)
        payload["resource_limits"]["worker_timeout_seconds"] = 4801
        atomic_json(self.manifest_path, payload)
        with self.assertRaisesRegex(ValueError, "first-version time bounds"):
            load_manifest(self.manifest_path)


if __name__ == "__main__":
    unittest.main()
