"""Mock-only Current Mirror AL functional-evaluation and reporting regressions."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ActiveLearning.experiment_logging import write_completed_run
from ActiveLearning.real_evaluation import real_evaluator


CANDIDATE = {"candidate_id": "al_001", "params": {"IB1": -0.001}}
TARGETS = {
    "current_error": {"operator": "<=", "value": 0.02},
    "output_resistance": {"operator": ">=", "value": 26e6},
}


def functional_result(valid, status, reasons):
    return {
        "functional_feasible": valid,
        "functional_status": status,
        "failure_reasons": reasons,
        "basic_behavior": {"mirror_response_valid": valid},
        "compliance": {"relative_variation": 0.02},
        "reference_tracking": {"max_tracking_error": None if not valid else 0.03},
    }


class CurrentMirrorALFunctionalTests(unittest.TestCase):
    def test_existing_validator_result_propagates_and_numeric_spec_stays_separate(self):
        for valid, status, reasons in (
            (True, "valid", []),
            (False, "invalid", ["output_branch_collapsed"]),
            (False, "not_evaluable",
             ["reference_tracking_not_evaluable_due_to_bounds"]),
        ):
            with (
                self.subTest(status=status),
                patch.object(real_evaluator, "evaluate_current_mirror",
                             return_value=(0.01, 30e6, True)) as performance,
                patch.object(real_evaluator, "check_current_mirror_functionality",
                             return_value=functional_result(valid, status, reasons))
                as functional,
            ):
                record = real_evaluator.evaluate_selected_candidate(
                    CANDIDATE, adapter="fake", target_specs=TARGETS,
                    run_functional=True,
                )
            performance.assert_called_once()
            functional.assert_called_once_with(CANDIDATE["params"], adapter="fake")
            self.assertIs(record["status"]["meets_spec"], True)
            self.assertIs(record["status"]["functional_valid"], valid)
            self.assertIs(record["status"]["functional_feasible"], valid)
            self.assertEqual(record["status"]["functional_status"], status)
            self.assertEqual(record["failure_reasons"], reasons)
            self.assertEqual(record["functional_checks"]["mirror_response_valid"],
                             valid)

    def test_unperformed_functional_validation_remains_unknown(self):
        with (
            patch.object(real_evaluator, "evaluate_current_mirror",
                         return_value=(0.01, 30e6, True)),
            patch.object(real_evaluator, "check_current_mirror_functionality")
            as functional,
        ):
            record = real_evaluator.evaluate_selected_candidate(
                CANDIDATE, adapter="fake", target_specs=TARGETS,
                run_functional=False,
            )
        functional.assert_not_called()
        self.assertIsNone(record["status"]["functional_valid"])
        self.assertIsNone(record["status"]["functional_feasible"])
        self.assertIs(record["status"]["meets_spec"], True)

    def test_invalid_base_simulation_does_not_run_functional_validator(self):
        with (
            patch.object(real_evaluator, "evaluate_current_mirror",
                         return_value=(None, None, False)),
            patch.object(real_evaluator, "check_current_mirror_functionality")
            as functional,
        ):
            record = real_evaluator.evaluate_selected_candidate(
                CANDIDATE, adapter="fake", target_specs=TARGETS,
                run_functional=True,
            )
        functional.assert_not_called()
        self.assertIs(record["status"]["simulation_valid"], False)
        self.assertIsNone(record["status"]["functional_valid"])
        self.assertIsNone(record["status"]["meets_spec"])

    def test_invalid_parameter_is_classified_before_simulation(self):
        runtime = SimpleNamespace(
            param_template={
                "optimization_space": {"variables": ["IB1"]},
                "parameters": [{
                    "name": "IB1",
                    "variable_type": "continuous",
                    "admissible_range": {"lower": -0.01, "upper": -1e-6},
                }],
            },
            create_evaluator=MagicMock(
                side_effect=AssertionError("invalid parameter reached evaluator")
            ),
        )
        invalid = {
            "candidate_id": "bad",
            "params": {"IB1": 1.0},
        }
        with patch.object(
                real_evaluator, "evaluate_current_mirror",
                side_effect=AssertionError("invalid parameter reached SPICE")):
            record = real_evaluator.evaluate_selected_candidate(
                invalid, adapter="fake", target_specs=TARGETS,
                runtime=runtime, run_functional=True,
            )
        self.assertIs(record["status"]["parameter_valid"], False)
        self.assertIsNone(record["status"]["simulation_valid"])
        self.assertIsNone(record["status"]["functional_valid"])
        self.assertIsNone(record["status"]["meets_spec"])
        self.assertIn("outside admissible range", record["failure_reasons"][0])
        runtime.create_evaluator.assert_not_called()

    def test_summary_reports_numeric_and_joint_success_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            result = {
                "best_or_final_record": {
                    "status": {
                        "parameter_valid": True,
                        "simulation_valid": True,
                        "functional_feasible": True,
                        "meets_spec": True,
                        "joint_success": False,
                    },
                    "metrics": {"current_error": 0.01,
                                "output_resistance": 30e6},
                },
                "iterations_completed": 1,
                "stop_reason": "max_iterations",
            }
            summary = write_completed_run(
                root, {"run_id": "mock", "timestamp": "2026-01-01",
                       "run_directory": run_dir}, result,
                circuit_type="CurrentMirror", circuit_id="circuit_test",
                acquisition_mode="conservative_feasibility",
            )
            stored = json.loads((run_dir / "summary.json").read_text(
                encoding="utf-8"
            ))
            self.assertIs(summary["meets_spec"], True)
            self.assertIsNone(summary["functional_valid"])
            self.assertIsNone(summary["joint_success"])
            self.assertIsNone(summary["functional_joint_success"])
            self.assertIs(summary["functional_unverified"], True)
            self.assertIsNone(stored["joint_success"])
            other_dir = root / "other"
            other_dir.mkdir()
            other_summary = write_completed_run(
                root, {"run_id": "other", "timestamp": "2026-01-01",
                       "run_directory": other_dir}, result,
                circuit_type="OpAmp", circuit_id="circuit_other",
                acquisition_mode="conservative_feasibility",
            )
            self.assertIs(other_summary["functional_valid"], True)
            self.assertNotIn("joint_success", other_summary)

    def test_existing_run_index_keeps_legacy_header(self):
        legacy_fields = (
            "run_id", "timestamp", "circuit_type", "circuit_id",
            "benchmark_version", "data_expansion_mode", "acquisition_mode",
            "iterations_completed", "stop_reason", "meets_spec",
            "current_error", "output_resistance", "total_seconds",
            "total_input_tokens", "total_output_tokens", "total_tokens",
            "llm_calls",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "run_index.csv"
            with index.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=legacy_fields)
                writer.writeheader()
                writer.writerow({"run_id": "historical", "meets_spec": True})
            run_dir = root / "run"
            run_dir.mkdir()
            result = {
                "best_or_final_record": {
                    "status": {
                        "parameter_valid": True,
                        "simulation_valid": True,
                        "functional_valid": True,
                        "meets_spec": True,
                        "joint_success": True,
                    },
                    "metrics": {
                        "current_error": 0.01,
                        "output_resistance": 30e6,
                    },
                },
                "iterations_completed": 1,
                "stop_reason": "meets_spec",
            }
            summary = write_completed_run(
                root, {"run_id": "new", "timestamp": "2026-01-01",
                       "run_directory": run_dir}, result,
                circuit_type="CurrentMirror", circuit_id="circuit_test",
                acquisition_mode="conservative_feasibility",
            )
            with index.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                header = tuple(reader.fieldnames or ())
            self.assertEqual(header, legacy_fields)
            self.assertEqual([row["run_id"] for row in rows],
                             ["historical", "new"])
            self.assertIs(summary["joint_success"], True)


if __name__ == "__main__":
    unittest.main()
