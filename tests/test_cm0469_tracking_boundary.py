"""Offline-only reference-tracking bounds and bounded-worker regressions."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ActiveLearning import cm0469_bounded_d0 as bounded
from ActiveLearning.InitializeD0 import d0_builder
from ActiveLearning.InitializeD0.validation import current_mirror_functional_validator as tracking
from ActiveLearning.onboarding.errors import BindingValidationError


class FakeAdapter:
    reference_excitation_quantity = "current"
    mirror_ratio = 1.0
    vout_nominal = 1.65
    temperature = 25.0

    def __init__(self, *, unrelated_error=False):
        self.unrelated_error = unrelated_error
        self.runtime_parameters = (
            SimpleNamespace(name="IB1", lower=-0.01, upper=-1e-6),
            SimpleNamespace(name="R0", lower=100.0, upper=1e6),
            SimpleNamespace(name="R1", lower=100.0, upper=1e6),
        )
        self.op_calls = 0

    def scale_reference(self, params, scale):
        return {**params, "IB1": params["IB1"] * scale}

    def prepare_reference_stimulus(self, params, scale):
        if self.unrelated_error and scale == 1.2:
            raise BindingValidationError("unrelated topology binding failure")
        scaled = self.scale_reference(params, scale)
        if not -0.01 <= scaled["IB1"] <= -1e-6:
            raise BindingValidationError("outside admissible range")
        magnitude = abs(scaled["IB1"])
        return {"params": scaled, "requested_excitation": magnitude,
                "requested_reference": magnitude, "nominal_excitation": abs(params["IB1"]),
                "excitation_element": "IIB1", "stimulus_overrides": None}

    def build_circuit(self, params, _nominal_vout, *, stimulus_overrides=None):
        return params

    def read_applied_stimulus(self, circuit, _prepared):
        return abs(circuit["IB1"])


def fake_op(circuit, adapter, _temperature):
    adapter.op_calls += 1
    value = -abs(circuit["IB1"])
    return value, value


def functional_result(params, adapter):
    nominal = {"reference_current_valid": True, "output_current_valid": True,
               "reference_current": abs(params["IB1"]),
               "output_current": abs(params["IB1"]), "observed_ratio_to_expected": 1.0}
    compliance = {"passed": True, "quality_smoke_passed": True,
                  "relative_variation": 0.0, "output_branch_alive": True, "alive_fraction": 1.0}
    with patch.object(tracking, "configure_ngspice"), \
         patch.object(tracking, "_run_basic_nominal_test", return_value=nominal), \
         patch.object(tracking, "_run_compliance_test", return_value=compliance), \
         patch.object(tracking, "_measure_op_currents", side_effect=fake_op):
        return tracking.check_current_mirror_functionality(
            params, adapter, compliance_vout_start=1.55, compliance_vout_stop=1.75,
        )


class TrackingBoundaryTests(unittest.TestCase):
    def test_d0_record_keeps_original_candidate_valid_but_unassessed(self):
        runtime = SimpleNamespace(
            param_template={},
            create_evaluator=lambda: lambda _params: (0.01, 30e6, True),
            create_functional_validator=lambda: lambda _params, **_kwargs: {
                "functional_feasible": False, "functional_status": "not_evaluable",
                "failure_reasons": ["reference_tracking_not_evaluable_due_to_bounds"],
                "compliance": {"relative_variation": 0.0},
                "reference_tracking": {"max_tracking_error": None,
                                       "measurements": [{"scale": 0.8}, {"scale": 1.0}],
                                       "bounds_exclusion": {"scale": 1.2,
                                                            "outside": [{"parameter": "IB1"}]}},
            },
        )
        sample = {"sample_id": "d0_v3_013", "sampling_source": "global",
                  "params": {"IB1": -0.00870091486815062, "R0": 10000.0, "R1": 10000.0},
                  "normalized_params": {}}
        with patch.object(d0_builder, "_runtime_metric_names",
                          return_value=["current_error", "output_resistance"]), \
             patch.object(d0_builder, "validate_parameters",
                          return_value={"parameter_valid": True, "violations": []}), \
             patch.object(d0_builder, "check_meets_spec", return_value={"meets_spec": True}):
            row = d0_builder.build_runtime_d0(runtime, target_specs=bounded.TARGETS,
                                              prepared_samples=[sample])[0]
        self.assertTrue(row["status"]["parameter_valid"])
        self.assertTrue(row["status"]["simulation_valid"])
        self.assertFalse(row["status"]["functional_feasible"])
        self.assertIsNone(row["status"]["meets_spec"])
        self.assertEqual(row["status"]["functional_status"], "invalid")
        self.assertEqual(row["functional_evaluation_status"], "not_evaluable")
        self.assertIn("reference_tracking_not_evaluable_due_to_bounds", row["failure_reasons"])
        self.assertEqual(row["reference_tracking"]["bounds_exclusion"]["scale"], 1.2)

    def test_upper_magnitude_boundary_is_explicitly_not_evaluable(self):
        params = {"IB1": -0.00870091486815062, "R0": 10000.0, "R1": 10000.0}
        adapter = FakeAdapter()
        result = functional_result(params, adapter)
        self.assertEqual(adapter.op_calls, 2)
        self.assertFalse(result["functional_feasible"])
        self.assertEqual(result["functional_status"], "not_evaluable")
        self.assertIn("reference_tracking_not_evaluable_due_to_bounds", result["failure_reasons"])
        self.assertNotIn("reference_response_data_invalid", result["failure_reasons"])
        observed = result["reference_tracking"]
        self.assertEqual(len(observed["measurements"]), 2)
        self.assertEqual(observed["bounds_exclusion"]["scale"], 1.2)
        self.assertEqual(observed["bounds_exclusion"]["outside"][0]["parameter"], "IB1")
        self.assertFalse(observed["passed"])
        self.assertIsNone(observed["max_tracking_error"])

    def test_small_magnitude_boundary_skips_before_any_tracking_op(self):
        params = {"IB1": -1.1e-6, "R0": 10000.0, "R1": 10000.0}
        adapter = FakeAdapter()
        result = functional_result(params, adapter)
        self.assertEqual(adapter.op_calls, 0)
        self.assertEqual(result["functional_status"], "not_evaluable")
        self.assertEqual(result["reference_tracking"]["bounds_exclusion"]["scale"], 0.8)
        self.assertEqual(result["reference_tracking"]["measurements"], [])
        self.assertFalse(result["reference_tracking"]["passed"])
        self.assertFalse(result["functional_feasible"])

    def test_normal_three_scale_tracking_is_unchanged(self):
        params = {"IB1": -1e-4, "R0": 10000.0, "R1": 10000.0}
        adapter = FakeAdapter()
        result = functional_result(params, adapter)
        self.assertEqual(adapter.op_calls, 3)
        self.assertEqual(result["functional_status"], "valid")
        self.assertTrue(result["functional_feasible"])
        self.assertEqual(result["failure_reasons"], [])
        self.assertNotIn("bounds_exclusion", result["reference_tracking"])

    def test_in_range_tracking_can_fail_without_becoming_bounds_limited(self):
        params = {"IB1": -1e-4, "R0": 10000.0, "R1": 10000.0}
        adapter = FakeAdapter()
        nominal = {"reference_current_valid": True, "output_current_valid": True,
                   "reference_current": -1e-4, "output_current": -1e-4,
                   "observed_ratio_to_expected": 1.0}
        compliance = {"passed": True, "quality_smoke_passed": True,
                      "relative_variation": 0.0, "output_branch_alive": True,
                      "alive_fraction": 1.0}
        with patch.object(tracking, "configure_ngspice"), \
             patch.object(tracking, "_run_basic_nominal_test", return_value=nominal), \
             patch.object(tracking, "_run_compliance_test", return_value=compliance), \
             patch.object(tracking, "_measure_op_currents",
                          side_effect=lambda circuit, *_: (-abs(circuit["IB1"]), -1e-4)):
            result = tracking.check_current_mirror_functionality(
                params, adapter, compliance_vout_start=1.55, compliance_vout_stop=1.75,
            )
        self.assertEqual(len(result["reference_tracking"]["measurements"]), 3)
        self.assertEqual(result["functional_status"], "invalid")
        self.assertFalse(result["functional_feasible"])
        self.assertNotIn("reference_tracking_not_evaluable_due_to_bounds", result["failure_reasons"])

    def test_unrelated_binding_failure_is_not_a_bounds_case(self):
        params = {"IB1": -1e-4, "R0": 10000.0, "R1": 10000.0}
        adapter = FakeAdapter(unrelated_error=True)
        result = functional_result(params, adapter)
        self.assertEqual(adapter.op_calls, 2)
        self.assertIn("reference_response_data_invalid", result["failure_reasons"])
        self.assertNotIn("reference_tracking_not_evaluable_due_to_bounds", result["failure_reasons"])
        self.assertFalse(result["functional_feasible"])

    def test_al_initial_target_refresh_preserves_bounds_non_evaluability(self):
        from ActiveLearning.active_learning import _recompute_initial_meets_spec

        record = {
            "sample_id": "d0_v3_013", "metrics": {
                "current_error": 0.01, "output_resistance": 30e6,
            },
            "status": {"parameter_valid": True, "simulation_valid": True,
                       "functional_status": "invalid", "functional_feasible": False,
                       "meets_spec": None},
            "functional_evaluation_status": "not_evaluable",
            "failure_reasons": ["reference_tracking_not_evaluable_due_to_bounds"],
        }
        self.assertIsNone(_recompute_initial_meets_spec([record], bounded.TARGETS))
        self.assertIsNone(record["status"]["meets_spec"])
        ordinary = {**record, "status": dict(record["status"]), "failure_reasons": []}
        self.assertIs(_recompute_initial_meets_spec([ordinary], bounded.TARGETS), ordinary)
        self.assertIs(ordinary["status"]["meets_spec"], True)


class BoundedWorkerBoundaryTests(unittest.TestCase):
    def _worker(self, *, completed_tracking_ops, actual_op_attempts, reasons,
                max_attempts=8, original_params=None, status_overrides=None,
                exclusion_overrides=None):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            request_path = base / "request.json"
            ledger_slot = {}
            bounds = "reference_tracking_not_evaluable_due_to_bounds" in reasons
            params = dict(original_params or {**bounded.ANCHOR["params"], "IB1": (
                -1.1e-6 if bounds and completed_tracking_ops == 0 else
                -0.00870091486815062 if bounds else -1e-4
            )})
            runtime = SimpleNamespace(adapter=FakeAdapter())
            fake_onboarding = types.ModuleType("ActiveLearning.onboarding")
            fake_onboarding.build_active_learning_circuit_config = lambda *_a, **_k: runtime
            fake_init = types.ModuleType("ActiveLearning.InitializeD0")
            fake_init.__path__ = []
            fake_builder = types.ModuleType("ActiveLearning.InitializeD0.d0_builder")
            fake_initialization = types.ModuleType("ActiveLearning.d0_initialization")
            fake_initialization._functional_config = lambda *_a: {}

            def build(_runtime, **_kwargs):
                ledger = ledger_slot["ledger"]
                for _ in range(actual_op_attempts):
                    ledger.call("op", 1, lambda: None)
                for _ in range(2):
                    ledger.call("dc", 11, lambda: None)
                status = {"parameter_valid": True, "simulation_valid": True,
                          "functional_valid": False, "functional_status": "invalid",
                          "functional_feasible": False,
                          "meets_spec": None if bounds else False}
                status.update(status_overrides or {})
                scale = 0.8 if completed_tracking_ops == 0 else 1.2
                exclusion = tracking._reference_scale_outside_admissible(
                    params, runtime.adapter, scale,
                )
                if exclusion is None and bounds:
                    valid = {**params, "IB1": -0.00870091486815062}
                    exclusion = tracking._reference_scale_outside_admissible(
                        valid, runtime.adapter, 1.2,
                    )
                if exclusion is not None:
                    exclusion = {**exclusion, **(exclusion_overrides or {})}
                return [{
                    "params": params, "status": status,
                    "functional_evaluation_status": "not_evaluable" if bounds else "invalid",
                    "failure_reasons": reasons,
                    "reference_tracking": {
                        "measurements": [{"scale": value} for value in
                                         (0.8, 1.0, 1.2)[:completed_tracking_ops]],
                        "perturbation_policy": {"primary_scales": [0.8, 1.0, 1.2]},
                        "bounds_exclusion": exclusion,
                    } if bounds else {},
                    "metrics": {"current_error": 0.1, "output_resistance": 1000.0},
                }]

            fake_builder.build_runtime_d0 = build
            bounded._json_atomic(request_path, {
                "mode": "d0", "sample": {"sample_id": "fake", "params": params},
                "asset_sha256": {}, "max_attempts": max_attempts,
                "worker_nonce": "offline-nonce", "event_path": str(base / "analyses.jsonl"),
                "result_path": str(base / "result.json"),
            })
            with patch.object(bounded, "_verify_assets"), patch.object(bounded, "_check_design"), \
                 patch.object(bounded, "_environment"), \
                 patch.object(bounded, "install_spice_accounting",
                              side_effect=lambda ledger: ledger_slot.update(ledger=ledger)), \
                 patch.dict(sys.modules, {
                     "ActiveLearning.onboarding": fake_onboarding,
                     "ActiveLearning.InitializeD0": fake_init,
                     "ActiveLearning.InitializeD0.d0_builder": fake_builder,
                     "ActiveLearning.d0_initialization": fake_initialization,
                 }), patch.dict(bounded.os.environ, {"CM0469_BOUNDED_WORKER_NONCE": "offline-nonce"}):
                exit_code = bounded._worker(request_path)
            events = bounded._read_events(base / "analyses.jsonl")
            counts = bounded._counts(events)
            result = json.loads((base / "result.json").read_text(encoding="utf-8")) if (base / "result.json").is_file() else None
            return exit_code, counts, result

    def test_bounds_cases_keep_exact_journal_and_never_claim_spec(self):
        for completed, op in ((2, 5), (0, 3)):
            with self.subTest(completed=completed):
                code, counts, result = self._worker(
                    completed_tracking_ops=completed, actual_op_attempts=op,
                    reasons=["reference_tracking_not_evaluable_due_to_bounds"],
                )
                self.assertEqual(code, 0)
                self.assertEqual((counts["op_attempts"], counts["dc_attempts"]), (op, 2))
                self.assertEqual(counts["attempts"], op + 2)
                self.assertEqual(counts["dc_sweep_points_requested"], 22)
                self.assertIsNone(result["record"]["status"]["meets_spec"])
                self.assertFalse(result["record"]["status"]["functional_feasible"])

    def test_invalid_original_or_simulation_cannot_claim_bounds_limited(self):
        reason = ["reference_tracking_not_evaluable_due_to_bounds"]
        for status_overrides, original_params in (
            ({"parameter_valid": False}, None),
            ({"simulation_valid": False}, None),
            ({}, {**bounded.ANCHOR["params"], "IB1": -0.011}),
        ):
            with self.subTest(status=status_overrides, params=original_params):
                code, counts, result = self._worker(
                    completed_tracking_ops=2, actual_op_attempts=5,
                    reasons=reason, status_overrides=status_overrides,
                    original_params=original_params,
                )
                self.assertEqual(code, 1)
                self.assertEqual(counts["attempts"], 7)
                self.assertIsNone(result)

    def test_malformed_bounds_evidence_still_stops(self):
        code, counts, result = self._worker(
            completed_tracking_ops=2, actual_op_attempts=5,
            reasons=["reference_tracking_not_evaluable_due_to_bounds"],
            exclusion_overrides={"outside": [{"parameter": "IB1", "requested_value": -0.009,
                                               "admissible_range": [-0.01, -1e-6]}]},
        )
        self.assertEqual(code, 1)
        self.assertEqual(counts["attempts"], 7)
        self.assertIsNone(result)

    def test_infrastructure_failure_and_mismatched_count_still_stop(self):
        code, counts, result = self._worker(
            completed_tracking_ops=2, actual_op_attempts=6,
            reasons=["reference_response_data_invalid"],
        )
        self.assertEqual(code, 1)
        self.assertEqual(counts["attempts"], 8)
        self.assertIsNone(result)
        code, counts, result = self._worker(
            completed_tracking_ops=2, actual_op_attempts=6,
            reasons=["reference_tracking_not_evaluable_due_to_bounds"],
        )
        self.assertEqual(code, 1)
        self.assertEqual(counts["attempts"], 8)
        self.assertIsNone(result)

    def test_budget_exhaustion_does_not_start_next_analysis(self):
        code, counts, result = self._worker(
            completed_tracking_ops=2, actual_op_attempts=5,
            reasons=["reference_tracking_not_evaluable_due_to_bounds"], max_attempts=6,
        )
        self.assertEqual(code, 1)
        self.assertEqual(counts["attempts"], 6)
        self.assertIsNone(result)

    def test_bounds_limited_candidate_does_not_block_next_fake_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            samples = [{"sample_id": "bounds_limited", "params": bounded.ANCHOR["params"]},
                       {"sample_id": "next_candidate", "params": bounded.ANCHOR["params"]}]

            def new_config(output):
                path = output / "active_learning_config.yaml"
                path.write_text("offline only\n", encoding="utf-8")
                return path

            def child(request_path, log_path, _timeout):
                request = json.loads(request_path.read_text(encoding="utf-8"))
                first = request["sample"]["sample_id"] == "bounds_limited"
                event_path = Path(request["event_path"])
                event_path.touch()
                op_count = 5 if first else 6
                for number in range(1, op_count + 3):
                    kind = "op" if number <= op_count else "dc"
                    bounded._event(event_path, {"event": "analysis_attempt", "number": number,
                                                "kind": kind, "points": 1 if kind == "op" else 11})
                    bounded._event(event_path, {"event": "analysis_result", "number": number,
                                                "outcome": "ok"})
                record = {"sample_id": request["sample"]["sample_id"],
                          "status": {"parameter_valid": True, "simulation_valid": True,
                                     "functional_feasible": not first,
                                     "meets_spec": None if first else False},
                          "metrics": {"current_error": 0.1, "output_resistance": 1000.0},
                          "failure_reasons": (["reference_tracking_not_evaluable_due_to_bounds"]
                                              if first else [])}
                bounded._json_atomic(Path(request["result_path"]), {
                    "record": record, "counts": bounded._counts(bounded._read_events(event_path)),
                })
                log_path.write_text("fake child\n", encoding="utf-8")
                return 0, False

            def save_rows(records, output, name):
                output.mkdir()
                jsonl = output / f"{name}.jsonl"
                csv = output / f"{name}.csv"
                jsonl.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
                csv.write_text("sample_id\n" + "\n".join(row["sample_id"] for row in records), encoding="utf-8")
                return {"jsonl": jsonl, "csv": csv}

            fake_init = types.ModuleType("ActiveLearning.InitializeD0")
            fake_init.__path__ = []
            fake_builder = types.ModuleType("ActiveLearning.InitializeD0.d0_builder")
            fake_builder.save_d0_dataset = save_rows
            with patch.object(bounded, "_check_design"), \
                 patch.object(bounded, "_environment", return_value={"git_commit": bounded.FIXED_COMMIT}), \
                 patch.object(bounded, "_asset_hashes", return_value={"asset": "offline"}), \
                 patch.object(bounded, "_verify_frozen_inputs"), \
                 patch.object(bounded, "_samples", return_value=samples), \
                 patch.object(bounded, "_new_config", side_effect=new_config), \
                 patch.object(bounded, "_run_child", side_effect=child), \
                 patch.dict(sys.modules, {"ActiveLearning.InitializeD0": fake_init,
                                          "ActiveLearning.InitializeD0.d0_builder": fake_builder}):
                manifest = bounded.execute("d0", base / "fresh_output")
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["analysis_counts"]["attempts"], 15)
            self.assertEqual([item["sample_id"] for item in manifest["candidates"]],
                             ["bounds_limited", "next_candidate"])
            self.assertIsNone(manifest["candidates"][0]["status"]["meets_spec"])


if __name__ == "__main__":
    unittest.main()
