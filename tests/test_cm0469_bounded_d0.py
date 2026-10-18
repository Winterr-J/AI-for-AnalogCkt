"""Offline-only tests: no PySpice, ngspice, D0, AL, or network calls."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from ActiveLearning import cm0469_bounded_d0 as bounded


class BoundedAccountingTests(unittest.TestCase):
    def test_existing_d0_builder_evaluates_only_prepared_sample(self):
        fake_package = types.ModuleType("ActiveLearning.InitializeD0")
        fake_package.__path__ = []
        fake_yaml = types.ModuleType("yaml")
        fake_yaml.safe_load = lambda _stream: {"performance_template": {
            "active_metrics": ["current_error", "output_resistance"]}}
        fake_evaluator = types.ModuleType("ActiveLearning.InitializeD0.evaluator")
        fake_evaluator.evaluate_current_mirror = lambda *_args, **_kwargs: None
        fake_sampling = types.ModuleType("ActiveLearning.InitializeD0.sampling")

        def unexpected_sampler(*_args, **_kwargs):
            self.fail("V3 sampler must not be called again inside a candidate worker")

        for name in ("generate_v1_global_samples", "generate_v2_local_global_samples",
                     "generate_v3_physical_local_global_samples",
                     "generate_v4_norm_bounded_local_global_samples"):
            setattr(fake_sampling, name, unexpected_sampler)
        fake_validation = types.ModuleType("ActiveLearning.InitializeD0.validation")
        fake_validation.validate_parameters = lambda *_args: {"parameter_valid": True}
        fake_validation.check_meets_spec = lambda *_args: {"meets_spec": False}
        fake_validation.check_current_mirror_functionality = lambda *_args, **_kwargs: None
        runtime = unittest.mock.Mock()
        runtime.param_template = {"optimization_space": {"variables": ["IB1", "R0", "R1"]}}
        runtime.performance_template_path = Path(__file__)
        runtime.create_evaluator.return_value = lambda _params: (0.1, 1000.0, True)
        runtime.create_functional_validator.return_value = lambda _params, **_kwargs: {
            "functional_feasible": False, "functional_status": "invalid",
            "failure_reasons": ["output_branch_collapsed"],
            "compliance": {"relative_variation": 0.5},
            "reference_tracking": {"max_tracking_error": 0.1},
        }
        with patch.dict(sys.modules, {
            "yaml": fake_yaml,
            "ActiveLearning.InitializeD0": fake_package,
            "ActiveLearning.InitializeD0.evaluator": fake_evaluator,
            "ActiveLearning.InitializeD0.sampling": fake_sampling,
            "ActiveLearning.InitializeD0.validation": fake_validation,
        }):
            spec = importlib.util.spec_from_file_location(
                "ActiveLearning.InitializeD0.d0_builder",
                bounded.ROOT / "ActiveLearning/InitializeD0/d0_builder.py",
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            rows = module.build_runtime_d0(runtime, target_specs=bounded.TARGETS,
                                           prepared_samples=[{
                                               "sample_id": "only_one", "sampling_source": "global",
                                               "params": bounded.ANCHOR["params"],
                                               "normalized_params": {},
                                           }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_id"], "only_one")
        self.assertFalse(rows[0]["status"]["meets_spec"])
        self.assertEqual(rows[0]["failure_reasons"], ["output_branch_collapsed"])

    def test_attempts_count_before_success_and_failure_and_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyses.jsonl"
            path.touch()
            ledger = bounded.AnalysisLedger(path, 2, "candidate")
            self.assertEqual(ledger.call("op", 1, lambda: 3), 3)

            def failing():
                raise RuntimeError("fake ngspice failure")

            with self.assertRaisesRegex(RuntimeError, "fake ngspice"):
                ledger.call("dc", 11, failing)
            with self.assertRaisesRegex(bounded.BoundedD0Error, "exhausted"):
                ledger.call("op", 1, lambda: self.fail("must not execute"))
            counts = bounded._counts(bounded._read_events(path))
            self.assertEqual((counts["op_attempts"], counts["dc_attempts"]), (1, 1))
            self.assertEqual(counts["failures"], 1)
            self.assertEqual(counts["dc_sweep_points_requested"], 11)

    def test_unfinished_attempt_is_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyses.jsonl"
            path.touch()
            bounded._event(path, {"event": "analysis_attempt", "number": 1,
                                  "kind": "op", "points": 1})
            self.assertEqual(bounded._counts(bounded._read_events(path))["unknown_outcomes"], 1)

    def test_bad_journal_and_sweep_are_rejected(self):
        with self.assertRaises(bounded.BoundedD0Error):
            bounded._counts([{"event": "analysis_attempt", "number": 2,
                              "kind": "op", "points": 1}])
        self.assertEqual(bounded._dc_points({"VOUT": slice(1.55, 1.75, 0.02)}), 11)
        with self.assertRaises(bounded.BoundedD0Error):
            bounded._dc_points({"VOUT": slice(1.55, 1.75, 0.01)})

    def test_patch_rejects_uncounted_backend_and_unsupported_analysis(self):
        class Base:
            def _run(self, method, *args, **kwargs):
                return method

        class Shared(Base):
            def _run(self, method, *args, **kwargs):
                return super()._run(method, *args, **kwargs)

        class Low:
            def run(self, *args, **kwargs):
                return None

        fake_simulation = types.ModuleType("PySpice.Spice.Simulation")
        fake_simulation.CircuitSimulator = Base
        fake_ngsimulation = types.ModuleType("PySpice.Spice.NgSpice.Simulation")
        fake_ngsimulation.NgSpiceSharedCircuitSimulator = Shared
        fake_shared = types.ModuleType("PySpice.Spice.NgSpice.Shared")
        fake_shared.NgSpiceShared = Low
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {
            "PySpice.Spice.Simulation": fake_simulation,
            "PySpice.Spice.NgSpice.Simulation": fake_ngsimulation,
            "PySpice.Spice.NgSpice.Shared": fake_shared,
        }):
            path = Path(directory) / "analyses.jsonl"
            path.touch()
            bounded.install_spice_accounting(bounded.AnalysisLedger(path, 2, "fake"))
            self.assertEqual(Shared()._run("operating_point"), "operating_point")
            with self.assertRaisesRegex(bounded.BoundedD0Error, "uncounted"):
                Base()._run("operating_point")
            with self.assertRaisesRegex(bounded.BoundedD0Error, "unsupported"):
                Shared()._run("ac")
            self.assertEqual(bounded._counts(bounded._read_events(path))["attempts"], 1)


class BoundedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def _fake_child(self, request_path, log_path, timeout_seconds):
        self.assertLessEqual(timeout_seconds, 180)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        journal = Path(request["event_path"])
        journal.touch()
        for number, kind, points in ((1, "op", 1), (2, "dc", 11)):
            bounded._event(journal, {"event": "analysis_attempt", "number": number,
                                     "kind": kind, "points": points})
            bounded._event(journal, {"event": "analysis_result", "number": number,
                                     "outcome": "ok"})
        result = {
            "record": {"sample_id": request["sample"]["sample_id"],
                       "params": bounded.ANCHOR["params"],
                       "metrics": {"current_error": 0.1, "output_resistance": 1000},
                       "simulation_valid": True},
            "counts": bounded._counts(bounded._read_events(journal)),
            "elapsed_seconds": 0.01,
        }
        bounded._json_atomic(Path(request["result_path"]), result)
        log_path.write_text("fake child output\n", encoding="utf-8")
        return 0, False

    def _patches(self, child=None):
        return (patch.object(bounded, "_check_design"),
                patch.object(bounded, "_environment", return_value={"git_commit": bounded.FIXED_COMMIT}),
                patch.object(bounded, "_asset_hashes", return_value={"asset": "abc"}),
                patch.object(bounded, "_verify_frozen_inputs"),
                patch.object(bounded, "_samples", return_value=[{
                    "sample_id": "nominal", "params": bounded.ANCHOR["params"],
                }]),
                patch.object(bounded, "_run_child", side_effect=child or self._fake_child))

    def test_preflight_evidence_is_complete_without_target_pass(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for context in self._patches():
                stack.enter_context(context)
            output = self.base / "preflight"
            result = bounded.execute("preflight", output)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["analysis_counts"]["attempts"], 2)
        self.assertEqual(result["preflight"]["metrics"]["current_error"], 0.1)
        self.assertTrue((output / "run_events.jsonl").is_file())
        self.assertTrue((output / "nominal/worker.log").is_file())

    def test_failure_preserves_incomplete_manifest(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for context in self._patches(child=lambda *_: (_ for _ in ()).throw(
                    bounded.BoundedD0Error("fake fatal"))):
                stack.enter_context(context)
            output = self.base / "failed"
            with self.assertRaisesRegex(bounded.BoundedD0Error, "fake fatal"):
                bounded.execute("preflight", output)
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "incomplete")
        self.assertIn("fake fatal", manifest["stop_reason"])

    def test_existing_output_refuses_overwrite(self):
        output = self.base / "occupied"
        output.mkdir()
        with self.assertRaises(FileExistsError):
            bounded.execute("preflight", output)

    def test_asset_change_and_environment_mismatch_fail_closed(self):
        with patch.object(bounded, "_asset_hashes", return_value={"a": "changed"}):
            with self.assertRaisesRegex(bounded.BoundedD0Error, "hash changed"):
                bounded._verify_assets({"a": "original"})
        with patch.object(bounded.sys, "platform", "win32"):
            with self.assertRaisesRegex(bounded.BoundedD0Error, "Ubuntu"):
                bounded._environment()

    def test_pre_run_asset_change_stops_without_child(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for context in self._patches():
                stack.enter_context(context)
            stack.enter_context(patch.object(bounded, "_asset_hashes", side_effect=[
                {"asset": "before"}, {"asset": "changed"}, {"asset": "changed"},
            ]))
            child = stack.enter_context(patch.object(bounded, "_run_child"))
            output = self.base / "changed"
            with self.assertRaisesRegex(bounded.BoundedD0Error, "hash changed"):
                bounded.execute("preflight", output)
            child.assert_not_called()
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "incomplete")

    def test_child_timeout_kills_and_reaps(self):
        process = unittest.mock.Mock()
        process.pid = 123
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 1), ("stopped", None)]
        process.returncode = -9
        with patch.object(bounded.subprocess, "Popen", return_value=process), \
             patch.object(bounded.os, "name", "posix"), \
             patch.object(bounded.os, "killpg", create=True), \
             patch.object(bounded.signal, "SIGKILL", 9, create=True):
            code, timed_out = bounded._run_child(
                self.base / "request", self.base / "child.log", 0.01,
                command=[sys.executable, "-c", "pass"],
            )
        self.assertTrue(timed_out)
        self.assertEqual(code, -9)
        self.assertIn("stopped", (self.base / "child.log").read_text(encoding="utf-8"))

    def test_real_fake_child_process_is_hard_stopped(self):
        began = bounded.time.monotonic()
        code, timed_out = bounded._run_child(
            self.base / "unused_request", self.base / "sleep.log", 0.1,
            command=[sys.executable, "-B", "-c", "import time; time.sleep(5)"],
        )
        self.assertTrue(timed_out)
        self.assertNotEqual(code, 0)
        self.assertLess(bounded.time.monotonic() - began, 3)
        self.assertTrue((self.base / "sleep.log").is_file())

    def test_global_termination_kills_active_candidate_group(self):
        with patch.object(bounded, "_ACTIVE_CHILD_PID", 123), \
             patch.object(bounded.os, "killpg", create=True) as kill_group, \
             patch.object(bounded.signal, "SIGKILL", 9, create=True):
            with self.assertRaisesRegex(bounded.BoundedD0Error, "overall deadline"):
                bounded._terminate_for_global_deadline(15, None)
        kill_group.assert_called_once_with(123, 9)

    def test_overall_deadline_stops_before_candidate(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for context in self._patches():
                stack.enter_context(context)
            stack.enter_context(patch.object(bounded.time, "monotonic", side_effect=[0, 181, 182]))
            output = self.base / "expired"
            with self.assertRaisesRegex(bounded.BoundedD0Error, "time or analysis"):
                bounded.execute("preflight", output)
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(manifest["analysis_counts"]["attempts"], 0)

    def test_d0_unmet_targets_continue_and_manifest_matches(self):
        from contextlib import ExitStack

        def new_config(directory):
            path = directory / "active_learning_config.yaml"
            path.write_text("fake frozen config\n", encoding="utf-8")
            return path

        def save_rows(records, directory, name):
            directory.mkdir()
            jsonl = directory / f"{name}.jsonl"
            csv = directory / f"{name}.csv"
            jsonl.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            csv.write_text("id\n" + "\n".join(row["sample_id"] for row in records), encoding="utf-8")
            return {"jsonl": jsonl, "csv": csv}

        samples = [{"sample_id": f"fake_{index:03d}", "params": bounded.ANCHOR["params"]}
                   for index in range(2)]
        fake_package = types.ModuleType("ActiveLearning.InitializeD0")
        fake_package.__path__ = []
        fake_builder = types.ModuleType("ActiveLearning.InitializeD0.d0_builder")
        fake_builder.save_d0_dataset = save_rows
        with ExitStack() as stack:
            for context in self._patches():
                stack.enter_context(context)
            stack.enter_context(patch.object(bounded, "_samples", return_value=samples))
            stack.enter_context(patch.object(bounded, "_new_config", side_effect=new_config))
            stack.enter_context(patch.dict(sys.modules, {
                "ActiveLearning.InitializeD0": fake_package,
                "ActiveLearning.InitializeD0.d0_builder": fake_builder,
            }))
            output = self.base / "d0"
            result = bounded.execute("d0", output)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["analysis_counts"]["attempts"], 4)
        self.assertEqual(result["candidates"][1]["metrics"]["current_error"], 0.1)
        independent = json.loads((output / "d0/d0_v1_seed42_initialization.json").read_text(encoding="utf-8"))
        self.assertEqual(independent["d0_mode"], "independent")
        self.assertEqual(independent["analysis_counts"], result["analysis_counts"])
        self.assertTrue((output / "d0/d0_v1_seed42.csv").is_file())

    def test_worker_normal_functional_rejection_does_not_stop(self):
        runtime = unittest.mock.Mock()
        fake_onboarding = types.ModuleType("ActiveLearning.onboarding")
        fake_onboarding.build_active_learning_circuit_config = lambda *_args, **_kwargs: runtime
        fake_init = types.ModuleType("ActiveLearning.InitializeD0")
        fake_init.__path__ = []
        fake_builder = types.ModuleType("ActiveLearning.InitializeD0.d0_builder")
        fake_initialization = types.ModuleType("ActiveLearning.d0_initialization")
        fake_initialization._functional_config = lambda *_args: {}
        ledger_slot = {}

        def install(ledger):
            ledger_slot["ledger"] = ledger

        def build(_runtime, **_kwargs):
            ledger = ledger_slot["ledger"]
            for _ in range(6):
                ledger.call("op", 1, lambda: None)
            for _ in range(2):
                ledger.call("dc", 11, lambda: None)
            return [{"status": {"parameter_valid": True, "simulation_valid": True,
                                 "functional_status": "invalid", "functional_feasible": False,
                                 "meets_spec": False},
                     "metrics": {"current_error": 0.1, "output_resistance": 1000},
                     "failure_reasons": ["output_branch_collapsed"]}]

        fake_builder.build_runtime_d0 = build
        with patch.object(bounded, "_verify_assets"), patch.object(bounded, "_check_design"), \
             patch.object(bounded, "_environment"), \
             patch.object(bounded, "install_spice_accounting", side_effect=install), \
             patch.dict(sys.modules, {
                 "ActiveLearning.onboarding": fake_onboarding,
                 "ActiveLearning.InitializeD0": fake_init,
                 "ActiveLearning.InitializeD0.d0_builder": fake_builder,
                 "ActiveLearning.d0_initialization": fake_initialization,
             }):
            candidate = self.base / "candidate"
            candidate.mkdir()
            request = candidate / "request.json"
            bounded._json_atomic(request, {
                "mode": "d0", "sample": {"sample_id": "fake_000", "params": bounded.ANCHOR["params"]},
                "asset_sha256": {}, "max_attempts": 8, "worker_nonce": "test-only-nonce",
                "event_path": str(candidate / "analyses.jsonl"),
                "result_path": str(candidate / "result.json"),
            })
            with patch.dict(bounded.os.environ, {"CM0469_BOUNDED_WORKER_NONCE": "test-only-nonce"}):
                self.assertEqual(bounded._worker(request), 0)
        result = json.loads((candidate / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["counts"]["attempts"], 8)
        self.assertEqual(result["counts"]["dc_sweep_points_requested"], 22)
        self.assertFalse(result["record"]["status"]["meets_spec"])

    def test_worker_preflight_only_calls_formal_evaluator(self):
        runtime = unittest.mock.Mock()
        runtime.param_template = {}
        fake_onboarding = types.ModuleType("ActiveLearning.onboarding")
        fake_onboarding.build_active_learning_circuit_config = lambda *_args, **_kwargs: runtime
        fake_init = types.ModuleType("ActiveLearning.InitializeD0")
        fake_init.__path__ = []
        fake_validation = types.ModuleType("ActiveLearning.InitializeD0.validation")
        fake_validation.__path__ = []
        fake_parameter = types.ModuleType("ActiveLearning.InitializeD0.validation.parameter_validator")
        fake_parameter.validate_parameters = lambda *_args: {"parameter_valid": True}
        ledger_slot = {}

        def evaluator(_params):
            ledger = ledger_slot["ledger"]
            ledger.call("op", 1, lambda: None)
            ledger.call("dc", 11, lambda: None)
            return 0.1, 1000.0, True

        runtime.create_evaluator.return_value = evaluator
        with patch.object(bounded, "_verify_assets"), patch.object(bounded, "_check_design"), \
             patch.object(bounded, "_environment"), \
             patch.object(bounded, "install_spice_accounting",
                          side_effect=lambda ledger: ledger_slot.update(ledger=ledger)), \
             patch.dict(sys.modules, {
                 "ActiveLearning.onboarding": fake_onboarding,
                 "ActiveLearning.InitializeD0": fake_init,
                 "ActiveLearning.InitializeD0.validation": fake_validation,
                 "ActiveLearning.InitializeD0.validation.parameter_validator": fake_parameter,
             }):
            candidate = self.base / "preflight_worker"
            candidate.mkdir()
            request = candidate / "request.json"
            bounded._json_atomic(request, {
                "mode": "preflight", "sample": {"sample_id": "nominal", "params": bounded.ANCHOR["params"]},
                "asset_sha256": {}, "max_attempts": 2, "worker_nonce": "test-only-nonce",
                "event_path": str(candidate / "analyses.jsonl"),
                "result_path": str(candidate / "result.json"),
            })
            with patch.dict(bounded.os.environ, {"CM0469_BOUNDED_WORKER_NONCE": "test-only-nonce"}):
                self.assertEqual(bounded._worker(request), 0)
        runtime.create_functional_validator.assert_not_called()
        result = json.loads((candidate / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((result["counts"]["op_attempts"], result["counts"]["dc_attempts"]), (1, 1))

    def test_direct_worker_without_parent_nonce_is_rejected(self):
        request = self.base / "untrusted_request.json"
        bounded._json_atomic(request, {"mode": "preflight", "max_attempts": 2,
                                       "worker_nonce": "not-in-environment"})
        with patch.dict(bounded.os.environ, {"CM0469_BOUNDED_WORKER_NONCE": ""}):
            with self.assertRaisesRegex(bounded.BoundedD0Error, "parent process"):
                bounded._worker(request)


if __name__ == "__main__":
    unittest.main()
