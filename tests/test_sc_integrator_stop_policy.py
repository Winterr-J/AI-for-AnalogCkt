"""Synthetic-only SCIntegrator early-stop contract; no SPICE or LLM calls."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ActiveLearning import active_learning
from ActiveLearning.experiment_logging import write_completed_run


METRICS = (
    "integration_step_magnitude_v", "zero_input_drift_v_per_cycle",
    "step_consistency_v", "virtual_ground_error_v", "output_headroom_v",
)
TARGETS = {name: {"operator": ">=", "value": 1.0} for name in METRICS}
PARAMETERS = {"optimization_space": {"variables": ["X"]},
              "parameters": [{"name": "X", "variable_type": "continuous",
                              "admissible_range": {"lower": 0.0, "upper": 4.0}}]}
PERFORMANCE = {"performance_template": {
    "active_metrics": list(METRICS),
    "metrics": {name: {"direction": "maximize"} for name in METRICS}}}
CIRCUIT = {"circuit_type": "SCIntegrator", "circuit_id": "synthetic"}


def record(sample_id, *, meets=False, parameter=True, simulation=True,
           measurement=True, functional=True, value=None):
    level = (1.2 if meets else 0.8) if value is None else value
    return {"sample_id": sample_id, "params": {"X": 1.0},
            "metrics": {name: level for name in METRICS},
            "status": {"parameter_valid": parameter, "simulation_valid": simulation,
                       "measurement_valid": measurement, "functional_valid": functional,
                       "meets_spec": meets}}


def run_mock(records, real_records, *, stop=True, output_dir=None):
    calls = []
    pending = list(real_records)
    candidate = {"candidate_id": "candidate_0001", "params": {"X": 1.0},
                 "normalized_params": {"X": 0.5}}
    prediction = {**candidate, "prediction": {
        name: {"mean": 1.2, "std": 0.1} for name in METRICS}}
    selected = {**candidate, "violation_score": 0.0, "uncertainty_score": 0.1,
                "acquisition_score": 0.0, "metric_scores": {}}

    def evaluation(evaluation_candidate, *_args, **_kwargs):
        calls.append("real_evaluation")
        row = pending.pop(0)
        row["sample_id"] = evaluation_candidate["candidate_id"]
        return row

    with (patch.object(active_learning, "propose_inverse_design_seed", return_value={
            "x_seed": {"X": 1.0}, "inverse_context": [], "parameter_valid": True})
          as inverse,
          patch.object(active_learning, "expand_candidates", return_value=([candidate], {})),
          patch.object(active_learning, "predict_candidate_performance",
                       return_value=[prediction]),
          patch.object(active_learning, "select_next_candidate",
                       return_value=(selected, [selected])),
          patch.object(active_learning, "evaluate_selected_candidate",
                       side_effect=evaluation)):
        result = active_learning.run_active_learning(
            records, CIRCUIT, PARAMETERS, PERFORMANCE, TARGETS, adapter="mock",
            max_iterations=5, stop_on_meets_spec=stop, output_dir=output_dir,
            run_id="mock_run", llm_client=lambda _prompt: "mock")
    return result, calls, inverse.call_count


class SCIntegratorStopPolicyTests(unittest.TestCase):
    def test_new_preparation_writes_true_and_keeps_five_iterations(self):
        source = (Path(__file__).resolve().parents[1] /
                  "ActiveLearning/batch_runner_v2/sc_integrator_production.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        preparation = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                           and node.name == "preparation")
        policy = [node for node in ast.walk(preparation) if isinstance(node, ast.Dict)
                  and any(isinstance(key, ast.Constant) and key.value == "stop_on_meets_spec"
                          for key in node.keys)]
        self.assertEqual(len(policy), 1)
        configured = {key.value: value.value for key, value in
                      zip(policy[0].keys, policy[0].values)
                      if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)}
        self.assertEqual(configured["max_iterations"], 5)
        self.assertIs(configured["stop_on_meets_spec"], True)

    def test_valid_d0_stops_without_inverse_or_physical_evaluation(self):
        invalid = record("invalid_d0", meets=True, functional=False)
        valid = record("valid_d0", meets=True)
        with tempfile.TemporaryDirectory() as directory:
            result, calls, inverse_calls = run_mock(
                [invalid, valid], [], output_dir=directory)
            saved = [json.loads(line) for line in
                     (Path(directory) / "active_learning_records.jsonl").read_text().splitlines()]
            write_completed_run(
                directory, {"run_id": "mock_run", "timestamp": "mock",
                            "run_directory": Path(directory)}, result,
                circuit_type="SCIntegrator", circuit_id="synthetic",
                acquisition_mode="mock")
            summary = json.loads((Path(directory) / "summary.json").read_text())
        self.assertEqual(result["stop_reason"], "initial_d0_meets_spec")
        self.assertEqual(result["iterations_completed"], 0)
        self.assertEqual(result["d0_meets_spec_count"], 1)
        self.assertIs(result["best_or_final_record"], valid)
        self.assertFalse(invalid["status"]["meets_spec"])
        self.assertEqual([row["sample_id"] for row in saved], ["invalid_d0", "valid_d0"])
        self.assertTrue(summary["meets_spec"])
        self.assertEqual(summary["final_metrics"], valid["metrics"])
        self.assertEqual(summary["stop_reason"], "initial_d0_meets_spec")
        self.assertEqual(calls, [])
        self.assertEqual(inverse_calls, 0)

    def test_third_real_joint_success_stops_and_summary_preserves_winner(self):
        observations = [record(f"real_{i}", meets=i == 3) for i in range(1, 6)]
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            result, calls, _ = run_mock([record("d0")], observations,
                                        output_dir=run_dir)
            write_completed_run(
                directory, {"run_id": "mock_run", "timestamp": "mock",
                            "run_directory": run_dir}, result,
                circuit_type="SCIntegrator", circuit_id="synthetic",
                acquisition_mode="mock")
            summary = json.loads((run_dir / "summary.json").read_text())
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["iterations_completed"], 3)
        self.assertEqual(result["stop_reason"], "meets_spec")
        self.assertIs(result["best_or_final_record"], observations[2])
        self.assertEqual(result["best_or_final_record"]["params"], {"X": 1.0})
        self.assertEqual(set(result["best_or_final_record"]["metrics"]), set(METRICS))
        self.assertTrue(summary["meets_spec"])
        self.assertEqual(summary["final_metrics"], observations[2]["metrics"])
        self.assertEqual(summary["stop_reason"], "meets_spec")

    def test_prediction_claim_does_not_stop_without_real_success(self):
        result, calls, _ = run_mock(
            [record("d0")], [record("first"), record("second", meets=True)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["stop_reason"], "meets_spec")
        self.assertFalse(result["iteration_log"][0]["meets_spec"])

    def test_invalid_real_sample_cannot_trigger_early_stop(self):
        observations = [record("sim_invalid", meets=True, simulation=False),
                        record("function_invalid", meets=True, functional=False),
                        record("measurement_invalid", meets=True, measurement=False),
                        record("valid", meets=True)]
        result, calls, _ = run_mock([record("d0")], observations)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["stop_reason"], "meets_spec")
        self.assertEqual(result["iterations_completed"], 4)

    def test_five_real_failures_stop_at_max_iterations(self):
        result, calls, _ = run_mock(
            [record("d0")], [record(f"real_{i}") for i in range(5)])
        self.assertEqual(len(calls), 5)
        self.assertEqual(result["iterations_completed"], 5)
        self.assertEqual(result["stop_reason"], "max_iterations")
        self.assertFalse(result["best_or_final_record"]["status"]["meets_spec"])

    def test_legacy_false_policy_keeps_all_five_and_retains_earlier_success(self):
        initial = record("d0_success", meets=True)
        observations = [record(f"real_{i}", meets=i == 3) for i in range(1, 6)]
        result, calls, _ = run_mock([initial], observations, stop=False)
        self.assertEqual(len(calls), 5)
        self.assertEqual(result["stop_reason"], "max_iterations")
        self.assertFalse(result["best_or_final_record"]["status"]["meets_spec"])
        self.assertTrue(result["records"][3]["status"]["meets_spec"])
        self.assertTrue(initial["status"]["meets_spec"])


if __name__ == "__main__":
    unittest.main()
