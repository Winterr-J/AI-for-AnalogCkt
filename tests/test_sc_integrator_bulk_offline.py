"""Dependency-light checks: no PySpice, LLM, D0, Profiling or AL launch."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from ActiveLearning.batch_runner_v2.sc_integrator_production import inspect_manifest


ROOT = Path(__file__).resolve().parents[1]


def _selected_functions(path: Path, names: set[str], namespace: dict) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in names]
    assert {node.name for node in functions} == names
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class OfflineIntegrationTests(unittest.TestCase):
    def test_public_autoconfig_autotarget_d0_and_al_dispatch_with_mock_evaluator(self):
        from ActiveLearning.onboarding import build_active_learning_circuit_config
        from ActiveLearning.onboarding.runtime_provider import resolve_runtime_provider
        from ActiveLearning.auto_target import _real_point_evaluator
        from ActiveLearning.InitializeD0.d0_builder import build_runtime_d0
        from ActiveLearning.real_evaluation.real_evaluator import evaluate_selected_candidate

        runtime = build_active_learning_circuit_config("SCIntegrator", "circuit_0662")
        self.assertEqual(runtime.circuit_type, "SCIntegrator")
        self.assertEqual(runtime.runtime_provider.circuit_type, "SCIntegrator")
        self.assertEqual(runtime.runtime_provider.structural_builder_name,
                         "_build_sc_integrator_bulk_config")
        for legacy in ("SampleHold", "OpAmp", "CurrentMirror"):
            self.assertEqual(resolve_runtime_provider(legacy).circuit_type, legacy)
        params = {item.name: item.nominal for item in runtime.runtime_parameters}
        metrics = {name: 0.1 for name in (
            "integration_step_magnitude_v", "zero_input_drift_v_per_cycle",
            "step_consistency_v", "virtual_ground_error_v", "output_headroom_v")}
        calls = []

        def mock_physical(full_vector):
            calls.append(full_vector)
            return {"metrics": metrics, "parameter_valid": True,
                    "simulation_valid": True, "measurement_valid": True,
                    "functional_valid": True, "failure_reasons": []}

        runtime.testbench_config["_bulk_executor"] = mock_physical
        point = _real_point_evaluator(runtime)({"sample_id": "mock_profile", "params": params})
        self.assertTrue(point["functional_valid"])
        self.assertEqual(point["integration_step_magnitude_v"], 0.1)
        d0 = build_runtime_d0(runtime, prepared_samples=[
            {"sample_id": "mock_d0", "sampling_source": "mock",
             "params": params, "normalized_params": params}])
        self.assertEqual(len(d0), 1)
        self.assertTrue(d0[0]["status"]["functional_valid"])
        al = evaluate_selected_candidate(
            {"candidate_id": "mock_al", "params": params}, None, {}, runtime=runtime)
        self.assertEqual(al["sample_id"], "mock_al")
        self.assertTrue(al["status"]["functional_valid"])
        self.assertEqual(len(calls), 3)

    def test_autoconfig_blocked_retry_preserves_receipts_and_reenters_preparation(self):
        from ActiveLearning.batch_runner_v2 import sc_integrator_production as production
        from ActiveLearning import onboarding
        from ActiveLearning.batch_runner_v2 import sample_hold_bulk_production

        batch_id, circuit_id = "MTV2_SCI_PREP_001", "circuit_0662"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / batch_id / circuit_id
            directory.mkdir(parents=True)
            original = {"status": "BLOCKED", "batch_id": batch_id,
                        "circuit_id": circuit_id,
                        "reason": "AutoConfigError: Unsupported AutoConfig circuit type: SCIntegrator"}
            original_bytes = json.dumps(original, sort_keys=True).encode()
            (directory / "result.json").write_bytes(original_bytes)
            summary = {"batch_id": batch_id, "status": "PREPARATION_COMPLETE",
                       "blocked": 1, "circuits": {circuit_id: original}}
            summary_bytes = json.dumps(summary, sort_keys=True).encode()
            (directory.parent / "summary.json").write_bytes(summary_bytes)
            spec = {"batch_id": batch_id, "circuit_ids": [circuit_id],
                    "preparation_root": str(directory.parent),
                    "candidate_manifest_sha256": "a" * 64}
            with patch.object(production, "_linux"), patch.object(
                    production, "inspect_manifest", return_value=spec), patch.object(
                    production, "OUTPUT_ROOT", root / "batch_output"), patch.object(
                    sample_hold_bulk_production, "_bytecat_settings", return_value=(
                        "mock", "https://example.invalid", "chat")), patch.object(
                    onboarding, "build_active_learning_circuit_config",
                    side_effect=RuntimeError("MOCK_REENTERED_AUTOCONFIG")) as builder:
                first = production.preparation(batch_id, allow_spice=True, allow_llm=True)
                self.assertEqual(first["circuits"][circuit_id], original)
                builder.assert_not_called()
                pre_retry_summary = (directory.parent / "summary.json").read_bytes()
                second = production.preparation(
                    batch_id, allow_spice=True, allow_llm=True,
                    retry_autoconfig_blocked=True)
                self.assertIn("MOCK_REENTERED_AUTOCONFIG",
                              second["circuits"][circuit_id]["reason"], second)
                builder.assert_called_once_with("SCIntegrator", circuit_id)
            self.assertIn("MOCK_REENTERED_AUTOCONFIG",
                          second["circuits"][circuit_id]["reason"])
            self.assertEqual((directory / "result.blocked_autoconfig_original.json").read_bytes(),
                             original_bytes)
            self.assertEqual((directory.parent / "summary.blocked_autoconfig_original.json")
                             .read_bytes(), pre_retry_summary)
            self.assertFalse((directory / "physical_profile").exists())
            self.assertFalse((directory / "physical_d0").exists())

    def test_autoconfig_retry_rejects_other_errors_and_physical_history(self):
        from ActiveLearning.batch_runner_v2 import sc_integrator_production as production
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            circuit_id = "circuit_0662"
            directory = root / circuit_id
            directory.mkdir()
            spec = {"batch_id": "MTV2_SCI_PREP_001", "preparation_root": str(root)}
            receipt = {"status": "BLOCKED", "batch_id": spec["batch_id"],
                       "circuit_id": circuit_id,
                       "reason": "AutoConfigError: Unsupported AutoConfig circuit type: SCIntegrator"}
            (directory / "result.json").write_text(json.dumps(receipt), encoding="utf-8")
            (directory / "physical_profile").mkdir()
            with self.assertRaisesRegex(ValueError, "existing Preparation artifacts"):
                production._preserve_autoconfig_blocked_receipt(spec, circuit_id)
            (directory / "physical_profile").rmdir()
            output = root / "batch_output"
            (output / spec["batch_id"] / "native" / circuit_id).mkdir(parents=True)
            with patch.object(production, "OUTPUT_ROOT", output), self.assertRaisesRegex(
                    ValueError, "physical history"):
                production._preserve_autoconfig_blocked_receipt(spec, circuit_id)
            (output / spec["batch_id"] / "native" / circuit_id).rmdir()
            receipt["reason"] = "another failure"
            (directory / "result.json").write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "limited to the original"):
                production._preserve_autoconfig_blocked_receipt(spec, circuit_id)

    def _core_functions(self):
        core = ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_core.py"
        fixture_file = ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_fixtures.json"
        names = {"_constructor", "_topology", "_validate_converted_topology", "derive_plan",
                 "evaluate_traces", "_nominal_values", "build_runtime"}
        namespace = {"ast": ast, "re": re, "math": math, "json": json,
                     "np": np, "Path": Path, "ROOT": ROOT,
                     "MASTER": ROOT / "ActiveLearning/candidate_manifests/sc_integrator_v2/master_manifest.json",
                     "OVERRIDES": fixture_file,
                     "_DEVICE": re.compile(r"^(\S+)\s+\(([^)]*)\)\s+(\S+)$"),
                     "AutoConfigError": ValueError,
                     "METRICS": ("integration_step_magnitude_v",
                                 "zero_input_drift_v_per_cycle", "step_consistency_v",
                                 "virtual_ground_error_v", "output_headroom_v"),
                     "sha": lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
                     "canonical_sha256": lambda value: hashlib.sha256(json.dumps(
                         value, sort_keys=True, separators=(",", ":"), allow_nan=False
                     ).encode()).hexdigest(),
                     "_candidate_ids": lambda: {"circuit_0643", "circuit_0644",
                                                "circuit_0662", "circuit_2471"},
                     "_model_definitions": lambda archive: {
                         "NMOS_IDEAL": {"type": "NMOS", "parameters": {"VTO": .7}},
                         "PMOS_IDEAL": {"type": "PMOS", "parameters": {"VTO": -.7}}},
                     "_device_rows": lambda archive: archive.get("device_parameters", {})}
        namespace.update({"Any": object,
                          "ActiveLearningCircuitConfig": lambda **kwargs: SimpleNamespace(**kwargs),
                          "RuntimeBinding": lambda *args: args,
                          "RuntimeParameter": lambda *args: SimpleNamespace(
                              name=args[0], nominal=args[2], lower=args[3], upper=args[4]),
                          "SampleHoldCandidateBuilder": lambda template, fixed, scales:
                              SimpleNamespace(template=template, fixed=fixed, scales=scales),
                          "SCIntegratorSemanticMapping": lambda fixture: SimpleNamespace(
                              mapping=fixture)})
        namespace["yaml"] = SimpleNamespace(safe_load=lambda _text: {
            "circuit": {"id": namespace["_active_id"]},
            "operating_parameters": {"VDD": 3.3, "VIN1": 1.2, "IB1": 1e-4}})
        return _selected_functions(core, names, namespace)

    def test_all_archived_batches_have_unique_historical_ids(self):
        batches = [inspect_manifest(f"MTV2_SCI_PREP_{number:03d}")
                   for number in range(1, 83)]
        ids = [circuit for batch in batches for circuit in batch["circuit_ids"]]
        self.assertEqual((len(batches), len(ids), len(set(ids))), (82, 811, 811))
        with self.assertRaises(ValueError):
            inspect_manifest("MTV2_SCI_PREP_083")

    def test_representative_source_and_converted_topology_binding(self):
        import re as regex
        core = ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_core.py"
        namespace = _selected_functions(core, {"_topology", "_validate_converted_topology"},
                                        {"ast": ast, "re": regex,
                                         "_DEVICE": regex.compile(r"^(\S+)\s+\(([^)]*)\)\s+(\S+)$"),
                                         "AutoConfigError": ValueError})
        for suffix in ("0643", "0644", "0662", "2471"):
            base = ROOT / "dataset" / f"circuit_{suffix}" / "topology"
            rows = namespace["_topology"](base / f"circuit_{suffix}.cir")
            namespace["_validate_converted_topology"](base / f"circuit_{suffix}.py", rows)
            self.assertTrue(any(kind == "capacitor" for _, _, kind in rows))
        with self.assertRaises(ValueError):
            namespace["_validate_converted_topology"](
                ROOT / "dataset/circuit_0644/topology/circuit_0644.py",
                namespace["_topology"](ROOT / "dataset/circuit_0662/topology/circuit_0662.cir"))

    def test_two_reviewed_fixtures_have_distinct_phases_and_signs(self):
        data = json.loads((ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_fixtures.json")
                          .read_text(encoding="utf-8"))["circuits"]
        self.assertEqual(data["circuit_0662"]["expected_response_sign"], "negative")
        self.assertEqual(data["circuit_0644"]["expected_response_sign"], "positive")
        self.assertNotEqual(data["circuit_0662"]["integration_phase"],
                            data["circuit_0644"]["integration_phase"])
        self.assertNotIn("circuit_0643", data)

    def test_real_topology_fixture_and_model_binding(self):
        scope = self._core_functions()
        for suffix in ("0662", "0644", "0643"):
            circuit_id = f"circuit_{suffix}"
            scope["_active_id"] = circuit_id
            plan = scope["derive_plan"](circuit_id)
            self.assertEqual(plan["fixture"]["input"], "VIN1")
            self.assertEqual(plan["fixture"]["output"], "VOUT1")
            self.assertEqual(len(plan["fixture"]["clocks"]), 2)
            self.assertIn(plan["fixture"]["virtual_ground"],
                          {node for _, pins, kind in scope["_topology"](
                              ROOT / "dataset" / circuit_id / "topology" / f"{circuit_id}.cir")
                           if kind == "capacitor" for node in pins})
            self.assertEqual(set(plan["models"]), {"NMOS_IDEAL", "PMOS_IDEAL"})
        scope["_active_id"] = "circuit_2471"
        with self.assertRaisesRegex(ValueError, "virtual-ground reference"):
            scope["derive_plan"]("circuit_2471")

    def test_stage_a_b_parameter_binding_to_constructor(self):
        scope = self._core_functions()
        scope["_active_id"] = "circuit_0662"
        plan = scope["derive_plan"]("circuit_0662")
        plan["constructor_names"] = {"W_M11", "L_M11", "NMOS_MODEL", "VDD",
                                     "VIN1", "VCLK1", "VCLK2"}
        plan["archive"] = {"device_parameters": {"M11": {"W_um": 10.0, "L_um": 1.0}},
                           "operating_parameters": {"VDD": 3.3}}
        scope["derive_plan"] = lambda _circuit_id, root=None: plan
        stage_a = {"circuit_id": "circuit_0662", "device_parameters": {
            "M11": {"tunable_parameters": {"W": {"optimizable": True},
                                           "L": {"optimizable": True}}}}}
        stage_b = {"circuit_id": "circuit_0662", "device_design_space": {
            "M11": {"parameters": {"W": {"data_type": "continuous", "minimum": .22,
                                            "maximum": 100., "unit": "um", "scale": "log"},
                                   "L": {"data_type": "continuous", "minimum": .28,
                                         "maximum": 50., "unit": "um", "scale": "log"}}}}}
        scope["yaml"] = SimpleNamespace(safe_load=lambda text: stage_a if
                                        "scope: device_parameters_only" in text else stage_b)
        provider = SimpleNamespace(performance_template_path=lambda _root:
                                   ROOT / "ActiveLearning/circuit_types/SCIntegrator/performance_template.yaml")
        runtime = scope["build_runtime"]("SCIntegrator", "circuit_0662",
                                         repository_root=ROOT, runtime_provider=provider)
        self.assertEqual({item.name for item in runtime.runtime_parameters},
                         {"W_M11", "L_M11"})
        self.assertEqual(runtime.candidate_builder.scales,
                         {"W_M11": 1e-6, "L_M11": 1e-6})
        self.assertEqual(runtime.candidate_builder.fixed["NMOS_MODEL"], "NMOS_IDEAL")

    def test_transient_measurement_requires_real_step_direction_and_clock(self):
        scope = self._core_functions()
        fixture = {"period_s": 1e-6, "integration_phase": "phi2",
                   "delta_v": .005, "supply_v": 3.3,
                   "expected_response_sign": "positive", "clock_low_v": 0,
                   "clocks": ["VCLK1", "VCLK2"]}
        t = np.linspace(0, 15.2e-6, 15201)
        cycle = t / 1e-6
        clocks = {name: np.where((cycle % 1 >= start) & (cycle % 1 <= stop), 3.3, 0)
                  for name, start, stop in (("VCLK1", .12, .40), ("VCLK2", .62, .90))}
        baseline = {"time_s": t, "input_v": np.zeros_like(t),
                    "output_v": np.full_like(t, 1.65),
                    "virtual_ground_v": np.zeros_like(t), "clocks_v": clocks}
        perturbed = {**baseline,
                     "input_v": np.where(cycle >= 8.905, .005, 0),
                     "output_v": 1.65 + .001 * np.maximum(0, np.floor(cycle - 8.9) + 1)}
        good = scope["evaluate_traces"](fixture, baseline, perturbed)
        self.assertTrue(good["functional_valid"])
        self.assertGreater(good["metrics"]["integration_step_magnitude_v"], 0)
        bad_sign = scope["evaluate_traces"]({**fixture, "expected_response_sign": "negative"},
                                            baseline, perturbed)
        self.assertFalse(bad_sign["functional_valid"])
        bad_clock = scope["evaluate_traces"](fixture, baseline,
                                             {**perturbed, "clocks_v": {
                                                 "VCLK1": np.zeros_like(t),
                                                 "VCLK2": np.zeros_like(t)}})
        self.assertFalse(bad_clock["measurement_valid"])

    def test_duplicate_native_attempt_fails_closed(self):
        native_path = ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_native.py"
        tree = ast.parse(native_path.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "SCIntegratorPhysicalExecutor")
        verify = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == "verify_existing")
        scope = {"json": json, "AutoConfigError": ValueError,
                 "_events": lambda path: [json.loads(line) for line in path.read_text(
                     encoding="utf-8").splitlines()] if path.exists() else []}
        exec(compile(ast.Module(body=[verify], type_ignores=[]), str(native_path), "exec"), scope)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = {"schema": "sc_integrator_bulk_native_v1"}
            (root / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
            ledger = root / "physical_attempt_ledger.jsonl"
            ledger.write_text(json.dumps({"event": "reserved", "attempt": 1,
                                          "vector_sha256": "same",
                                          "identity_sha256": "identity"}) + "\n" +
                              json.dumps({"event": "reserved", "attempt": 2,
                                          "vector_sha256": "same",
                                          "identity_sha256": "identity"}) + "\n", encoding="utf-8")
            obj = SimpleNamespace(root=root, identity=identity, ledger=ledger,
                                  limit=5, identity_sha256="identity")
            with self.assertRaisesRegex(ValueError, "duplicate ledger"):
                scope["verify_existing"](obj)
            ledger.write_text("\n".join(json.dumps({"event": "reserved", "attempt": index,
                                                    "vector_sha256": f"vector-{index}",
                                                    "identity_sha256": "identity"})
                                        for index in range(1, 7)) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "budget/duplicate ledger"):
                scope["verify_existing"](obj)

    def test_v2_failure_isolation_resume_and_usage_fields(self):
        from ActiveLearning.batch_runner_v2 import runner
        from ActiveLearning.batch_runner_v2.storage import atomic_json
        jobs = [{"job_id": circuit, "circuit_id": circuit,
                 "circuit_type": "SCIntegrator", "configuration_id": circuit,
                 "config_sha256": "a" * 64, "d0_sha256": "b" * 64,
                 "source_sha256": "c" * 64, "max_iterations": 5, "seed": 42}
                for circuit in ("circuit_0662", "circuit_0644")]
        manifest = {"batch_id": "MTV2_SCI_PREP_001", "manifest_sha256": "d" * 64,
                    "provider": "openai", "model": "bytecat-mock",
                    "credential_scope": "bytecat_ubuntu",
                    "retry_policy": {"automatic_native_retries": 0},
                    "resource_limits": {"worker_timeout_seconds": 2400},
                    "jobs": jobs}
        calls = []

        def worker(job, _attempt):
            calls.append(job["job_id"])
            if job["job_id"] == "circuit_0662":
                atomic_json(_attempt / "child_spawn_intent.json", {
                    "manifest_sha256": manifest["manifest_sha256"],
                    "job_id": job["job_id"], "attempt_id": _attempt.name})
                atomic_json(_attempt / "child_reaped.json", {
                    "attempt_id": _attempt.name, "exit_confirmed": True,
                    "exit_code": 7})
                return {"state": "FAILED_TERMINAL", "error_code": "MOCK_FAILURE"}
            return {"state": "COMPLETED", "stop_reason": "max_iterations",
                    "iterations_completed": 5, "meets_spec": False,
                    "parameter_valid": True, "simulation_valid": True,
                    "functional_valid": True, "final_metrics": {},
                    "llm_calls": 6, "total_tokens": {"total_tokens": 1200},
                    "total_timing": {"total_seconds": 12.0},
                    "physical_ledger_path": "/mock/physical_attempt_ledger.jsonl"}

        with tempfile.TemporaryDirectory() as temporary, patch.object(
                runner, "verify_job", return_value=None):
            runner._run_batch(manifest, temporary, worker, offline_only=False,
                              execution_scope="sc_integrator_production")
            state = runner.status_batch(manifest["batch_id"], temporary)
            self.assertEqual(state["jobs"]["circuit_0662"]["state"], "FAILED_TERMINAL")
            self.assertEqual(state["jobs"]["circuit_0644"]["state"], "COMPLETED")
            runner._run_batch(manifest, temporary, worker, offline_only=False,
                              execution_scope="sc_integrator_production")
            self.assertEqual(calls, ["circuit_0662", "circuit_0644"])
            attempt = state["jobs"]["circuit_0644"]["attempts"][0]["attempt_id"]
            result = json.loads((Path(temporary) / manifest["batch_id"] / "jobs" /
                                 "circuit_0644" / "attempts" / attempt / "result.json").read_text(
                                     encoding="utf-8"))
            self.assertEqual((result["llm_calls"], result["total_tokens"]["total_tokens"]),
                             (6, 1200))

    def test_sample_hold_public_executor_not_changed(self):
        source = (ROOT / "ActiveLearning/onboarding/sample_hold_bulk_native.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "BulkPhysicalExecutor")
        initializer = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                           and node.name == "__init__")
        self.assertNotIn("kind", [arg.arg for arg in initializer.args.kwonlyargs])
        registry = (ROOT / "ActiveLearning/onboarding/runtime_provider.py").read_text(
            encoding="utf-8")
        for circuit_type in ("SampleHold", "OpAmp", "CurrentMirror", "SCIntegrator"):
            self.assertIn(f'"{circuit_type}": CircuitTypeRuntimeProvider(', registry)


if __name__ == "__main__":
    unittest.main()
