from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ActiveLearning import active_learning
from ActiveLearning.active_learning import run_active_learning


_CIRCUIT_INFO = {"circuit_type": "CurrentMirror", "circuit_id": "test"}
_PARAMETER_TEMPLATE = {
    "optimization_space": {"variables": ["X"]},
    "parameters": [
        {
            "name": "X",
            "variable_type": "continuous",
            "admissible_range": {"lower": 0.0, "upper": 4.0},
        }
    ],
}
_PERFORMANCE_TEMPLATE = {
    "performance_template": {
        "active_metrics": ["metric"],
        "metrics": {"metric": {"direction": "minimize"}},
    }
}
_TARGETS = {"metric": {"operator": "<=", "value": 1.0}}
_CLIENT = object()
_EXPANSION_DIAGNOSTICS = {
    "filtered_historical_duplicates": 0,
    "filtered_within_pool_duplicates": 0,
}


def _candidate():
    return {
        "candidate_id": "candidate_0000",
        "params": {"X": 1.0},
        "normalized_params": {"X": 0.5},
    }


def _prediction():
    return {
        "candidate_id": "candidate_0000",
        "params": {"X": 1.0},
        "prediction": {"metric": {"mean": 0.8, "std": 0.1}},
    }


def _selected():
    return {
        "candidate_id": "candidate_0000",
        "params": {"X": 1.0},
        "violation_score": 0.2,
        "uncertainty_score": 0.1,
        "acquisition_score": 0.17,
        "metric_scores": {},
    }


def _record(iteration, *, simulation_valid=True, meets_spec=False):
    return {
        "sample_id": f"candidate_{iteration}",
        "source": "active_learning_query",
        "params": {"X": 1.0},
        "normalized_params": {"X": 0.5},
        "metrics": {"metric": 0.8 if simulation_valid else None},
        "functional_metrics": {},
        "status": {
            "parameter_valid": True,
            "simulation_valid": simulation_valid,
            "functional_feasible": None,
            "meets_spec": meets_spec if simulation_valid else None,
        },
    }


def _run_with_mocks(real_results, *, max_iterations, output_dir=None, run_id=None,
                    data_expansion_config=None):
    events = []
    inverse_lengths = []

    def inverse(records, *_args, **_kwargs):
        events.append("inverse")
        inverse_lengths.append(len(records))
        return {
            "x_seed": {"X": 1.0},
            "inverse_context": [],
            "parameter_valid": True,
        }

    def expansion(*_args, **_kwargs):
        events.append("expansion")
        return [_candidate()], dict(_EXPANSION_DIAGNOSTICS)

    def prediction(*_args, **_kwargs):
        events.append("prediction")
        return [_prediction()]

    def acquisition(*_args, **_kwargs):
        events.append("acquisition")
        selected = _selected()
        return selected, [selected]

    def evaluation(*_args, **_kwargs):
        events.append("evaluation")
        return real_results.pop(0)

    records = [{"sample_id": "d0_000"}]
    with (
        patch.object(active_learning, "propose_inverse_design_seed", side_effect=inverse) as inverse_mock,
        patch.object(active_learning, "expand_candidates", side_effect=expansion) as expansion_mock,
        patch.object(active_learning, "predict_candidate_performance", side_effect=prediction) as prediction_mock,
        patch.object(active_learning, "select_next_candidate", side_effect=acquisition) as acquisition_mock,
        patch.object(active_learning, "evaluate_selected_candidate", side_effect=evaluation) as evaluation_mock,
    ):
        result = run_active_learning(
            records,
            _CIRCUIT_INFO,
            _PARAMETER_TEMPLATE,
            _PERFORMANCE_TEMPLATE,
            _TARGETS,
            adapter="adapter",
            max_iterations=max_iterations,
            beta=0.6,
            context_k=4,
            retrieval_k=2,
            committee_size=3,
            output_dir=output_dir,
            run_id=run_id,
            data_expansion_config=data_expansion_config,
            llm_client=_CLIENT,
        )
    mocks = (
        inverse_mock,
        expansion_mock,
        prediction_mock,
        acquisition_mock,
        evaluation_mock,
    )
    return result, records, events, inverse_lengths, mocks


class ActiveLearningTests(unittest.TestCase):
    def test_v2_mode_and_candidate_geometry_metadata_reach_iteration_log(self):
        candidate = {
            **_candidate(),
            "sampling_source": "near_local",
            "source": "near_local",
            "requested_radius": 0.005,
            "actual_seed_distance": 0.002,
            "sampling_mode": "v2",
        }
        config = {"mode": "v2", "radius": {"near": 0.005, "broad": 0.02}}
        with patch(__name__ + "._candidate", return_value=candidate):
            result, _records, _events, _lengths, mocks = _run_with_mocks(
                [_record(1)], max_iterations=1, data_expansion_config=config
            )
        expansion_kwargs = mocks[1].call_args.kwargs
        self.assertEqual(expansion_kwargs["mode"], "v2")
        self.assertEqual(expansion_kwargs["v2_config"], config)
        entry = result["iteration_log"][0]
        self.assertEqual(entry["data_expansion_mode"], "v2")
        diagnostic = entry["candidate_diagnostics"][0]
        self.assertEqual(diagnostic["source"], "near_local")
        self.assertEqual(diagnostic["requested_radius"], 0.005)
        self.assertEqual(diagnostic["actual_seed_distance"], 0.002)
        self.assertEqual(diagnostic["sampling_mode"], "v2")

    def test_initial_d0_feasible_stops_before_all_pipeline_stages(self):
        first = _record(0)
        first["metrics"]["metric"] = 1.2
        feasible = _record(1, meets_spec=True)
        records = [first, feasible]

        with (
            patch.object(active_learning, "propose_inverse_design_seed") as inverse,
            patch.object(active_learning, "expand_candidates") as expansion,
            patch.object(active_learning, "predict_candidate_performance") as prediction,
            patch.object(active_learning, "select_next_candidate") as acquisition,
            patch.object(active_learning, "evaluate_selected_candidate") as evaluation,
        ):
            result = run_active_learning(
                records,
                _CIRCUIT_INFO,
                _PARAMETER_TEMPLATE,
                _PERFORMANCE_TEMPLATE,
                _TARGETS,
                adapter="adapter",
                llm_client=_CLIENT,
            )

        self.assertIs(result["records"], records)
        self.assertEqual(len(records), 2)
        self.assertEqual(result["iterations_completed"], 0)
        self.assertEqual(result["stop_reason"], "initial_d0_meets_spec")
        self.assertIs(result["best_or_final_record"], feasible)
        self.assertEqual(result["iteration_log"], [])
        self.assertEqual(result["llm_calls"], 0)
        self.assertEqual(
            result["evaluation_counts"],
            {
                "initial_d0_records_loaded": 2,
                "new_real_spice_evaluations": 0,
                "real_spice_evaluations": 0,
            },
        )
        self.assertEqual(
            result["total_tokens"],
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        for stage in active_learning._STAGE_NAMES:
            self.assertEqual(result["total_timing"][f"{stage}_seconds"], 0.0)
        self.assertGreaterEqual(result["total_timing"]["total_seconds"], 0.0)
        for stage_mock in (inverse, expansion, prediction, acquisition, evaluation):
            stage_mock.assert_not_called()

    def test_initial_d0_selects_first_of_multiple_feasible_records(self):
        first = _record(1, meets_spec=True)
        second = _record(2, meets_spec=True)

        result = run_active_learning(
            [first, second],
            _CIRCUIT_INFO,
            _PARAMETER_TEMPLATE,
            _PERFORMANCE_TEMPLATE,
            _TARGETS,
            adapter="adapter",
            llm_client=_CLIENT,
        )

        self.assertIs(result["best_or_final_record"], first)

    def test_only_literal_true_triggers_initial_stop(self):
        records = [
            {"sample_id": "false", "status": {"meets_spec": False}},
            {"sample_id": "none", "status": {"meets_spec": None}},
            {"sample_id": "missing", "status": {}},
        ]
        measured = _record(1)
        with (
            patch.object(
                active_learning,
                "propose_inverse_design_seed",
                return_value={
                    "x_seed": {"X": 1.0},
                    "inverse_context": [],
                    "parameter_valid": True,
                },
            ) as inverse,
            patch.object(
                active_learning,
                "expand_candidates",
                return_value=([_candidate()], dict(_EXPANSION_DIAGNOSTICS)),
            ),
            patch.object(
                active_learning,
                "predict_candidate_performance",
                return_value=[_prediction()],
            ),
            patch.object(
                active_learning,
                "select_next_candidate",
                return_value=(_selected(), [_selected()]),
            ),
            patch.object(
                active_learning,
                "evaluate_selected_candidate",
                return_value=measured,
            ),
        ):
            result = run_active_learning(
                records,
                _CIRCUIT_INFO,
                _PARAMETER_TEMPLATE,
                _PERFORMANCE_TEMPLATE,
                _TARGETS,
                adapter="adapter",
                max_iterations=1,
                llm_client=_CLIENT,
            )

        inverse.assert_called_once()
        self.assertEqual(result["iterations_completed"], 1)
        self.assertEqual(result["stop_reason"], "max_iterations")

    def test_stale_top_level_true_without_valid_measurement_is_not_trusted(self):
        stale = {"sample_id": "top-level", "meets_spec": True}

        result = run_active_learning(
            [stale],
            _CIRCUIT_INFO,
            _PARAMETER_TEMPLATE,
            _PERFORMANCE_TEMPLATE,
            _TARGETS,
            adapter="adapter",
            max_iterations=0,
            llm_client=_CLIENT,
        )

        self.assertIsNone(result["best_or_final_record"])
        self.assertEqual(result["stop_reason"], "max_iterations")
        self.assertIsNone(stale["meets_spec"])

    def test_stale_true_is_recomputed_false_for_current_target(self):
        stale = _record(0, meets_spec=True)
        stale["metrics"]["metric"] = 1.2
        measured = _record(1, meets_spec=False)

        with (
            patch.object(
                active_learning,
                "propose_inverse_design_seed",
                return_value={
                    "x_seed": {"X": 1.0},
                    "inverse_context": [],
                    "parameter_valid": True,
                },
            ) as inverse,
            patch.object(
                active_learning,
                "expand_candidates",
                return_value=([_candidate()], dict(_EXPANSION_DIAGNOSTICS)),
            ),
            patch.object(
                active_learning,
                "predict_candidate_performance",
                return_value=[_prediction()],
            ),
            patch.object(
                active_learning,
                "select_next_candidate",
                return_value=(_selected(), [_selected()]),
            ),
            patch.object(
                active_learning,
                "evaluate_selected_candidate",
                return_value=measured,
            ) as evaluation,
        ):
            result = run_active_learning(
                [stale],
                _CIRCUIT_INFO,
                _PARAMETER_TEMPLATE,
                _PERFORMANCE_TEMPLATE,
                _TARGETS,
                adapter="adapter",
                max_iterations=1,
                llm_client=_CLIENT,
            )

        self.assertIs(stale["status"]["meets_spec"], False)
        self.assertEqual(result["stop_reason"], "max_iterations")
        self.assertEqual(result["iterations_completed"], 1)
        self.assertEqual(result["llm_calls"], 0)
        inverse.assert_called_once()
        evaluation.assert_called_once()

    def test_stale_false_is_recomputed_true_without_llm_or_spice(self):
        stale = _record(0, meets_spec=False)

        with (
            patch.object(active_learning, "propose_inverse_design_seed") as inverse,
            patch.object(active_learning, "expand_candidates") as expansion,
            patch.object(active_learning, "predict_candidate_performance") as prediction,
            patch.object(active_learning, "select_next_candidate") as acquisition,
            patch.object(active_learning, "evaluate_selected_candidate") as evaluation,
        ):
            result = run_active_learning(
                [stale],
                _CIRCUIT_INFO,
                _PARAMETER_TEMPLATE,
                _PERFORMANCE_TEMPLATE,
                _TARGETS,
                adapter="adapter",
                llm_client=_CLIENT,
            )

        self.assertIs(stale["status"]["meets_spec"], True)
        self.assertEqual(result["stop_reason"], "initial_d0_meets_spec")
        self.assertEqual(result["llm_calls"], 0)
        for stage_mock in (inverse, expansion, prediction, acquisition, evaluation):
            stage_mock.assert_not_called()

    def test_matching_stored_value_remains_true_after_recomputation(self):
        record = _record(0, meets_spec=True)

        result = run_active_learning(
            [record],
            _CIRCUIT_INFO,
            _PARAMETER_TEMPLATE,
            _PERFORMANCE_TEMPLATE,
            _TARGETS,
            adapter="adapter",
            llm_client=_CLIENT,
        )

        self.assertIs(record["status"]["meets_spec"], True)
        self.assertEqual(result["stop_reason"], "initial_d0_meets_spec")

    def test_invalid_initial_simulation_recomputes_meets_spec_to_none(self):
        invalid = _record(0, simulation_valid=False)
        invalid["status"]["meets_spec"] = True

        result = run_active_learning(
            [invalid],
            _CIRCUIT_INFO,
            _PARAMETER_TEMPLATE,
            _PERFORMANCE_TEMPLATE,
            _TARGETS,
            adapter="adapter",
            max_iterations=0,
            llm_client=_CLIENT,
        )

        self.assertIsNone(invalid["status"]["meets_spec"])
        self.assertEqual(result["stop_reason"], "max_iterations")

    def test_three_iterations_follow_order_update_dt_and_log(self):
        result, records, events, lengths, mocks = _run_with_mocks(
            [_record(1), _record(2), _record(3)],
            max_iterations=3,
        )
        inverse_mock, expansion, prediction, acquisition, evaluation = mocks

        self.assertEqual(
            events,
            [
                "inverse", "expansion", "prediction", "acquisition", "evaluation",
                "inverse", "expansion", "prediction", "acquisition", "evaluation",
                "inverse", "expansion", "prediction", "acquisition", "evaluation",
            ],
        )
        self.assertEqual(lengths, [1, 2, 3])
        self.assertIs(result["records"], records)
        self.assertEqual(len(records), 4)
        self.assertEqual(result["iterations_completed"], 3)
        self.assertEqual(result["stop_reason"], "max_iterations")
        self.assertEqual(len(result["iteration_log"]), 3)
        self.assertEqual([record["iteration"] for record in records[1:]], [1, 2, 3])
        self.assertEqual(
            [entry["history_size_before_iteration"] for entry in result["iteration_log"]],
            [1, 2, 3],
        )
        self.assertEqual(
            [entry["data_expansion_seed"] for entry in result["iteration_log"]],
            [42, 43, 44],
        )
        timing_keys = {
            "inverse_design_seconds",
            "data_expansion_seconds",
            "performance_prediction_seconds",
            "acquisition_seconds",
            "real_spice_seconds",
            "iteration_total_seconds",
        }
        for entry in result["iteration_log"]:
            self.assertEqual(
                entry["selected_prediction"],
                {"metric": {"mean": 0.8, "std": 0.1}},
            )
            self.assertEqual(entry["violation_score"], 0.2)
            self.assertEqual(entry["uncertainty_score"], 0.1)
            self.assertEqual(entry["acquisition_score"], 0.17)
            self.assertEqual(entry["metric_scores"], {})
            self.assertEqual(set(entry["timing"]), timing_keys)
            self.assertTrue(all(value >= 0 for value in entry["timing"].values()))
            self.assertEqual(
                entry["tokens"]["iteration_total"],
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        self.assertEqual(result["llm_calls"], 0)
        self.assertEqual(
            result["total_tokens"],
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        self.assertGreaterEqual(result["total_timing"]["total_seconds"], 0)

        inverse_mock.assert_called()
        self.assertEqual(inverse_mock.call_args.kwargs["context_k"], 4)
        expansion.assert_called_with(
            {"X": 1.0},
            _PARAMETER_TEMPLATE,
            seed=44,
            historical_records=records,
            return_diagnostics=True,
        )
        self.assertNotIn("target_specs", prediction.call_args.kwargs)
        self.assertEqual(prediction.call_args.kwargs["retrieval_k"], 2)
        self.assertEqual(prediction.call_args.kwargs["committee_size"], 3)
        self.assertIs(prediction.call_args.kwargs["llm_client"], _CLIENT)
        self.assertEqual(acquisition.call_args.kwargs["beta"], 0.6)
        self.assertEqual(
            acquisition.call_args.kwargs["acquisition_mode"],
            "conservative_feasibility",
        )
        self.assertIs(acquisition.call_args.kwargs["return_scored"], True)
        self.assertIn(
            "candidate_0000",
            acquisition.call_args.kwargs["validity_by_candidate_id"],
        )
        self.assertEqual(evaluation.call_args.kwargs, {"run_functional": False})
        evaluation_candidate = evaluation.call_args.args[0]
        self.assertEqual(
            evaluation_candidate["candidate_id"],
            "iter_003_candidate_0000",
        )
        self.assertEqual(evaluation_candidate["normalized_params"], {"X": 0.5})

    def test_logs_prediction_for_selected_candidate_without_overwriting_metrics(self):
        candidates = [
            {
                "candidate_id": "candidate_0000",
                "sampling_source": "local",
                "params": {"X": 1.0},
                "normalized_params": {"X": 0.25},
            },
            {
                "candidate_id": "candidate_0001",
                "sampling_source": "global",
                "params": {"X": 2.0},
                "normalized_params": {"X": 0.75},
            },
        ]
        predictions = [
            {
                "candidate_id": "candidate_0000",
                "params": {"X": 1.0},
                "prediction": {"metric": {"mean": 0.9, "std": 0.01}},
                "retrieval_context": [{"rank": 1, "sample_id": "d0_000"}],
                "raw_predictions": [
                    {"round": 1, "metrics": {"metric": 0.9}}
                ],
                "committee_attempt_counts": [1],
                "committee_unique_count": {"metric": 1},
            },
            {
                "candidate_id": "candidate_0001",
                "params": {"X": 2.0},
                "prediction": {"metric": {"mean": 0.4, "std": 0.2}},
                "retrieval_context": [{"rank": 1, "sample_id": "d0_001"}],
                "raw_predictions": [
                    {"round": 1, "metrics": {"metric": 0.4}}
                ],
                "committee_attempt_counts": [2],
                "committee_unique_count": {"metric": 1},
            },
        ]
        selected = {
            "candidate_id": "candidate_0001",
            "params": {"X": 2.0},
            "violation_score": 0.6,
            "mean_violation_score": 0.6,
            "uncertainty_score": 0.05,
            "risk_violation_score": 0.12,
            "acquisition_mode": "conservative_feasibility",
            "kappa": 1.0,
            "acquisition_score": 0.435,
            "metric_scores": {"metric": 0.6},
        }
        scored_candidates = [
            {
                "candidate_id": "candidate_0000",
                "params": {"X": 1.0},
                "violation_score": 0.9,
                "uncertainty_score": 0.01,
                "acquisition_score": -0.627,
                "metric_scores": {"metric": 0.9},
            },
            selected,
        ]
        inverse_context = [
            {
                "sample_id": "d0_000",
                "params": {"X": 0.5},
                "metrics": {"metric": 0.7},
                "relevance": 0.8,
            }
        ]
        measured = _record(1)
        measured["params"] = {"X": 2.0}
        measured["normalized_params"] = {"X": 0.75}
        measured["metrics"] = {"metric": 0.55}
        records = [_record(0)]
        records[0]["metrics"]["metric"] = 1.2

        with (
            patch.object(
                active_learning,
                "propose_inverse_design_seed",
                return_value={
                    "x_seed": {"X": 1.0},
                    "inverse_context": inverse_context,
                    "best_known_anchor": {
                        "sample_id": "d0_000",
                        "params": {"X": 0.5},
                        "measured_metrics": {"metric": 0.7},
                        "joint_violation": 0.3,
                    },
                    "x_seed_vs_anchor": {
                        "X": {
                            "seed_value": 1.0,
                            "anchor_value": 0.5,
                            "magnitude_ratio_or_relative_ratio": 2.0,
                        }
                    },
                    "x_seed_anchor_distance": 0.5,
                    "parameter_valid": True,
                },
            ) as inverse,
            patch.object(
                active_learning,
                "expand_candidates",
                return_value=(candidates, dict(_EXPANSION_DIAGNOSTICS)),
            ) as expansion,
            patch.object(
                active_learning, "predict_candidate_performance", return_value=predictions
            ) as prediction,
            patch.object(
                active_learning,
                "select_next_candidate",
                return_value=(selected, scored_candidates),
            ) as acquisition,
            patch.object(
                active_learning, "evaluate_selected_candidate", return_value=measured
            ) as evaluation,
        ):
            result = run_active_learning(
                records,
                _CIRCUIT_INFO,
                _PARAMETER_TEMPLATE,
                _PERFORMANCE_TEMPLATE,
                _TARGETS,
                adapter="adapter",
                max_iterations=1,
                llm_client=_CLIENT,
            )

        entry = result["iteration_log"][0]
        self.assertIs(entry["inverse_context"], inverse_context)
        self.assertEqual(
            entry["inverse_anchor"],
            {
                "sample_id": "d0_000",
                "params": {"X": 0.5},
                "metrics": {"metric": 0.7},
                "joint_violation": 0.3,
            },
        )
        self.assertEqual(entry["x_seed_anchor_distance"], 0.5)
        self.assertEqual(
            entry["x_seed_vs_anchor"]["X"][
                "magnitude_ratio_or_relative_ratio"
            ],
            2.0,
        )
        self.assertEqual(entry["candidate_count"], 2)
        self.assertEqual(len(entry["candidate_diagnostics"]), 2)
        self.assertEqual(
            [item["candidate_id"] for item in entry["candidate_diagnostics"]],
            ["candidate_0000", "candidate_0001"],
        )
        for diagnostic, candidate, predicted, scored in zip(
            entry["candidate_diagnostics"],
            candidates,
            predictions,
            scored_candidates,
        ):
            self.assertEqual(diagnostic["candidate_id"], candidate["candidate_id"])
            self.assertEqual(diagnostic["source"], candidate["sampling_source"])
            self.assertEqual(diagnostic["params"], candidate["params"])
            self.assertIs(diagnostic["prediction"], predicted["prediction"])
            self.assertIs(
                diagnostic["retrieval_context"], predicted["retrieval_context"]
            )
            self.assertIs(
                diagnostic["raw_predictions"], predicted["raw_predictions"]
            )
            self.assertIs(
                diagnostic["committee_attempt_counts"],
                predicted["committee_attempt_counts"],
            )
            self.assertIs(
                diagnostic["committee_unique_count"],
                predicted["committee_unique_count"],
            )
            for key in (
                "violation_score",
                "uncertainty_score",
                "acquisition_score",
                "metric_scores",
            ):
                self.assertEqual(diagnostic[key], scored[key])
        self.assertEqual(entry["selected_candidate_id"], "candidate_0001")
        self.assertEqual(entry["mean_violation_score"], 0.6)
        self.assertEqual(entry["risk_violation_score"], 0.12)
        self.assertEqual(entry["acquisition_mode"], "conservative_feasibility")
        self.assertEqual(entry["kappa"], 1.0)
        self.assertEqual(
            entry["selected_candidate_id"],
            max(
                entry["candidate_diagnostics"],
                key=lambda item: item["acquisition_score"],
            )["candidate_id"],
        )
        self.assertIs(entry["selected_prediction"], predictions[1]["prediction"])
        self.assertIs(
            entry["selected_prediction"],
            entry["candidate_diagnostics"][1]["prediction"],
        )
        self.assertEqual(
            entry["selected_prediction"],
            {"metric": {"mean": 0.4, "std": 0.2}},
        )
        self.assertEqual(entry["violation_score"], 0.6)
        self.assertEqual(entry["uncertainty_score"], 0.05)
        self.assertEqual(entry["acquisition_score"], 0.435)
        self.assertEqual(entry["metric_scores"], {"metric": 0.6})
        self.assertEqual(entry["metrics"], {"metric": 0.55})
        self.assertIn("timing", entry)
        self.assertIn("tokens", entry)
        inverse.assert_called_once()
        expansion.assert_called_once()
        prediction.assert_called_once()
        acquisition.assert_called_once()
        evaluation.assert_called_once()

    def test_meets_spec_on_second_iteration_stops(self):
        result, records, _, _, _ = _run_with_mocks(
            [_record(1), _record(2, meets_spec=True), _record(3)],
            max_iterations=3,
        )

        self.assertEqual(result["iterations_completed"], 2)
        self.assertEqual(result["stop_reason"], "meets_spec")
        self.assertEqual(len(records), 3)
        self.assertIs(result["best_or_final_record"], records[-1])

    def test_failed_simulation_is_appended_and_next_iteration_runs(self):
        result, records, _, lengths, _ = _run_with_mocks(
            [_record(1, simulation_valid=False), _record(2)],
            max_iterations=2,
        )

        self.assertIs(records[1]["status"]["simulation_valid"], False)
        self.assertEqual(lengths, [1, 2])
        self.assertEqual(result["iterations_completed"], 2)

    def test_failed_real_evaluation_updates_next_iteration_validity_memory(self):
        d0 = _record(0)
        d0["metrics"]["metric"] = 1.2  # usable, but not target-feasible
        d0["status"]["functional_feasible"] = True
        failed = _record(1, simulation_valid=False)
        failed["sample_id"] = "failed_real_query"
        second = _record(2, simulation_valid=False)
        with (
            patch.object(
                active_learning, "propose_inverse_design_seed",
                return_value={
                    "x_seed": {"X": 1.0},
                    "inverse_context": [],
                    "parameter_valid": True,
                },
            ),
            patch.object(
                active_learning, "expand_candidates",
                return_value=([_candidate()], dict(_EXPANSION_DIAGNOSTICS)),
            ),
            patch.object(
                active_learning, "predict_candidate_performance",
                return_value=[_prediction()],
            ),
            patch.object(
                active_learning, "evaluate_selected_candidate",
                side_effect=[failed, second],
            ),
        ):
            result = run_active_learning(
                [d0], _CIRCUIT_INFO, _PARAMETER_TEMPLATE,
                _PERFORMANCE_TEMPLATE, _TARGETS, adapter="adapter",
                max_iterations=2, llm_client=_CLIENT,
            )
        first, next_iteration = result["iteration_log"]
        self.assertEqual(first["validity_memory_size_before_iteration"], 1)
        self.assertEqual(first["validity_memory_size_after_iteration"], 2)
        self.assertIs(first["new_record_binary_validity"], False)
        self.assertEqual(first["selected_validity_risk"], 0.0)
        self.assertEqual(next_iteration["validity_memory_size_before_iteration"], 2)
        self.assertGreater(next_iteration["selected_validity_risk"], 0.0)
        self.assertIs(next_iteration["new_record_binary_validity"], False)
        from ActiveLearning.performance_prediction.performance_predictor import (
            _filter_usable_records,
        )
        self.assertEqual(_filter_usable_records([d0, failed], ["metric"]), [d0])

    def test_each_iteration_saves_current_jsonl_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            result, records, _, _, _ = _run_with_mocks(
                [_record(1), _record(2)],
                max_iterations=2,
                output_dir=directory,
                run_id="test_run_001",
            )
            path = Path(directory) / "active_learning_records.jsonl"
            saved = [json.loads(line) for line in path.read_text().splitlines()]
            iteration_path = Path(directory) / "iteration_log.jsonl"
            usage_path = Path(directory) / "llm_usage.jsonl"
            saved_iterations = [
                json.loads(line) for line in iteration_path.read_text().splitlines()
            ]
            saved_usage = usage_path.read_text()

        self.assertEqual(saved, records)
        self.assertEqual(result["iterations_completed"], 2)
        self.assertEqual(result["evaluation_counts"]["initial_d0_records_loaded"], 1)
        self.assertEqual(result["evaluation_counts"]["new_real_spice_evaluations"], 2)
        self.assertEqual(len(saved_iterations), 2)
        self.assertEqual(
            [item["real_spice_evaluation_index"] for item in saved_iterations],
            [1, 2],
        )
        self.assertTrue(
            all(item["run_id"] == "test_run_001" for item in saved_iterations)
        )
        self.assertIn("timing", saved_iterations[0])
        self.assertIn("tokens", saved_iterations[0])
        self.assertEqual(saved_usage, "")
        combined = path.name + iteration_path.name + usage_path.name + saved_usage
        self.assertNotIn("OPENAI_API_KEY", combined)
        self.assertNotIn("prompt", combined.casefold())

    def test_explicit_mock_usage_aggregates_by_stage_iteration_and_total(self):
        def usage_client(_prompt, *, usage_log, usage_context):
            if usage_context["stage"] == "inverse_design":
                usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
            else:
                usage = {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                }
            usage_log.append({**usage_context, **usage})
            return "{}"

        def inverse(*_args, llm_client, **_kwargs):
            llm_client("inverse prompt")
            return {
                "x_seed": {"X": 1.0},
                "inverse_context": [],
                "parameter_valid": True,
            }

        def prediction(*_args, llm_client, **_kwargs):
            for _ in range(5):
                llm_client("prediction prompt")
            return [_prediction()]

        records = [{"sample_id": "d0_000"}]
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(active_learning, "propose_inverse_design_seed", side_effect=inverse),
                patch.object(
                    active_learning,
                    "expand_candidates",
                    return_value=([_candidate()], dict(_EXPANSION_DIAGNOSTICS)),
                ),
                patch.object(
                    active_learning,
                    "predict_candidate_performance",
                    side_effect=prediction,
                ),
                patch.object(
                    active_learning,
                    "select_next_candidate",
                    return_value=(_selected(), [_selected()]),
                ),
                patch.object(
                    active_learning,
                    "evaluate_selected_candidate",
                    side_effect=[_record(1), _record(2)],
                ),
            ):
                result = run_active_learning(
                    records,
                    _CIRCUIT_INFO,
                    _PARAMETER_TEMPLATE,
                    _PERFORMANCE_TEMPLATE,
                    _TARGETS,
                    adapter="adapter",
                    max_iterations=2,
                    output_dir=directory,
                    llm_client=usage_client,
                )
            usage_entries = [
                json.loads(line)
                for line in (Path(directory) / "llm_usage.jsonl")
                .read_text()
                .splitlines()
            ]

        expected_inverse = {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        }
        expected_prediction = {
            "input_tokens": 500,
            "output_tokens": 100,
            "total_tokens": 600,
        }
        expected_iteration = {
            "input_tokens": 510,
            "output_tokens": 102,
            "total_tokens": 612,
        }
        for entry in result["iteration_log"]:
            self.assertEqual(entry["tokens"]["inverse_design"], expected_inverse)
            self.assertEqual(
                entry["tokens"]["performance_prediction"], expected_prediction
            )
            self.assertEqual(entry["tokens"]["iteration_total"], expected_iteration)
        self.assertEqual(
            result["total_tokens"],
            {"input_tokens": 1020, "output_tokens": 204, "total_tokens": 1224},
        )
        self.assertEqual(result["llm_calls"], 12)
        self.assertEqual(
            set(usage_entries[0]),
            {
                "iteration",
                "stage",
                "round",
                "input_tokens",
                "output_tokens",
                "total_tokens",
            },
        )
        for iteration in (1, 2):
            entries = [entry for entry in usage_entries if entry["iteration"] == iteration]
            inverse_entries = [
                entry for entry in entries if entry["stage"] == "inverse_design"
            ]
            prediction_entries = [
                entry
                for entry in entries
                if entry["stage"] == "performance_prediction"
            ]
            self.assertEqual([entry["round"] for entry in inverse_entries], [1])
            self.assertEqual(
                [entry["round"] for entry in prediction_entries],
                [1, 2, 3, 4, 5],
            )

    def test_invalid_inverse_seed_stops_before_expansion(self):
        with (
            patch.object(
                active_learning,
                "propose_inverse_design_seed",
                return_value={"x_seed": {"X": -1.0}, "parameter_valid": False},
            ),
            patch.object(active_learning, "expand_candidates") as expansion,
        ):
            with self.assertRaisesRegex(ValueError, "parameter validation"):
                run_active_learning(
                    [],
                    _CIRCUIT_INFO,
                    _PARAMETER_TEMPLATE,
                    _PERFORMANCE_TEMPLATE,
                    _TARGETS,
                    adapter="adapter",
                    max_iterations=1,
                    llm_client=_CLIENT,
                )
        expansion.assert_not_called()

    def test_real_spice_counter_counts_one_mocked_success_at_call_boundary(self):
        result, records, events, _, mocks = _run_with_mocks(
            [_record(1, meets_spec=True)], max_iterations=3
        )

        self.assertEqual(events.count("evaluation"), 1)
        mocks[-1].assert_called_once()
        self.assertEqual(result["iterations_completed"], 1)
        self.assertEqual(result["stop_reason"], "meets_spec")
        self.assertEqual(
            result["evaluation_counts"],
            {
                "initial_d0_records_loaded": 1,
                "new_real_spice_evaluations": 1,
                "real_spice_evaluations": 1,
            },
        )
        entry = result["iteration_log"][0]
        self.assertEqual(entry["real_spice_evaluation_index"], 1)
        self.assertTrue(entry["simulation_valid"])
        self.assertTrue(entry["meets_spec"])
        self.assertIn("real_spice_seconds", entry["timing"])
        for stage in active_learning._STAGE_NAMES:
            self.assertIn(f"{stage}_seconds", result["total_timing"])
        self.assertEqual(
            result["total_tokens"],
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        self.assertEqual(result["llm_calls"], 0)
        self.assertEqual(len(records), 2)

    def test_invalid_real_spice_result_still_counts_once(self):
        result, _, events, _, mocks = _run_with_mocks(
            [_record(1, simulation_valid=False)], max_iterations=1
        )

        self.assertEqual(events.count("evaluation"), 1)
        mocks[-1].assert_called_once()
        self.assertEqual(result["evaluation_counts"]["initial_d0_records_loaded"], 1)
        self.assertEqual(result["evaluation_counts"]["new_real_spice_evaluations"], 1)
        self.assertEqual(result["evaluation_counts"]["real_spice_evaluations"], 1)
        self.assertEqual(result["iteration_log"][0]["real_spice_evaluation_index"], 1)
        self.assertFalse(result["iteration_log"][0]["simulation_valid"])
        self.assertIsNone(result["iteration_log"][0]["meets_spec"])

    def test_main_contains_no_algorithm_or_direct_spice_dependency(self):
        source = Path(active_learning.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "validate_parameters",
            "PySpice",
            "ngspice",
            "current_error =",
            "output_resistance =",
            "OPENAI_API_KEY",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
