from __future__ import annotations

import inspect
import json
import math
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from ActiveLearning.performance_prediction import performance_predictor
from ActiveLearning.performance_prediction import predict_candidate_performance


_CIRCUIT_INFO = {
    "circuit_type": "CurrentMirror",
    "circuit_id": "test_circuit",
}


def _performance_template():
    return {
        "schema_version": "performance_template_v1",
        "performance_template": {
            "circuit_type": "current_mirror",
            "active_metrics": ["current_error", "output_resistance"],
            "metrics": {
                "current_error": {
                    "name": "current_error",
                    "unit": "ratio",
                    "direction": "minimize",
                    "description": "Relative current mismatch.",
                    "calculation": {"expression": "abs(IOUT-IREF)/abs(IREF)"},
                },
                "output_resistance": {
                    "name": "output_resistance",
                    "unit": "ohm",
                    "direction": "maximize",
                    "description": "Local output resistance.",
                    "calculation": {"expression": "abs(dVOUT/dIOUT)"},
                },
                "disabled_metric": {
                    "name": "disabled_metric",
                    "unit": "W",
                    "direction": "minimize",
                    "description": "Not active.",
                },
            },
        },
    }


def _candidate(index: int, *, normalized=None):
    normalized = normalized or {
        "B": 0.2 + index * 0.01,
        "A": 0.1 + index * 0.01,
    }
    return {
        "candidate_id": f"candidate_{index:04d}",
        "sampling_source": "local",
        "params": {"A": 1.0 + index, "B": 2.0 + index},
        "normalized_params": normalized,
        "seed_relevance": 0.9,
    }


def _record(
    index: int,
    *,
    parameter_valid=True,
    simulation_valid=True,
    functional_feasible=False,
    metrics=None,
    normalized=None,
):
    return {
        "sample_id": f"sample_{index}",
        "sampling_source": "global",
        "params": {"A": 1.0 + index, "B": 2.0 + index},
        "normalized_params": normalized
        or {"A": 0.05 + index * 0.1, "B": 0.10 + index * 0.1},
        "metrics": metrics
        or {
            "current_error": 0.01 + index * 0.001,
            "output_resistance": 1.0e6 + index * 1.0e5,
        },
        "status": {
            "parameter_valid": parameter_valid,
            "simulation_valid": simulation_valid,
            "functional_feasible": functional_feasible,
            "meets_spec": False,
        },
    }


def _round_response(candidates, round_index: int) -> str:
    return json.dumps(
        {
            "predictions": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "predicted_metrics": {
                        "current_error": 0.020
                        + 0.001 * round_index
                        + 0.0001 * index,
                        "output_resistance": 2.0e7
                        + 1.0e6 * round_index
                        + 1.0e5 * index,
                    },
                }
                for index, candidate in enumerate(candidates)
            ]
        }
    )


class PerformancePredictorTests(unittest.TestCase):
    def test_log_aware_retrieval_prefers_50k_for_60k_candidate(self) -> None:
        parameter_template = {
            "optimization_space": {"variables": ["R"]},
            "parameters": [
                {
                    "name": "R",
                    "variable_type": "continuous",
                    "admissible_range": {"lower": 100.0, "upper": 1.0e6},
                }
            ],
        }
        candidate = {
            "candidate_id": "candidate_log",
            "params": {"R": 60_000.0},
            "normalized_params": {"R": (60_000.0 - 100.0) / 999_900.0},
        }
        records = []
        for index, resistance in enumerate((12_000.0, 50_000.0, 500_000.0)):
            record = _record(index)
            record["params"] = {"R": resistance}
            record["normalized_params"] = {
                "R": (resistance - 100.0) / 999_900.0
            }
            records.append(record)

        cases, diagnostics = performance_predictor._build_prediction_cases(
            [candidate],
            records,
            ["current_error", "output_resistance"],
            ["R"],
            3,
            parameter_template=parameter_template,
            return_diagnostics=True,
        )

        self.assertEqual(
            cases[0]["historical_context"][0]["params"]["R"],
            50_000.0,
        )
        self.assertEqual(
            diagnostics["candidate_log"][0]["similarity_space"],
            "log_aware",
        )
    def test_ten_candidates_use_top_three_and_five_committee_calls(self) -> None:
        candidates = [_candidate(index) for index in range(10)]
        records = [_record(index) for index in range(6)]
        client = MagicMock(
            side_effect=[_round_response(candidates, index) for index in range(5)]
        )

        with patch.object(
            performance_predictor,
            "retrieve_top_k",
            wraps=performance_predictor.retrieve_top_k,
        ) as retrieval:
            result = predict_candidate_performance(
                candidates,
                records,
                _CIRCUIT_INFO,
                _performance_template(),
                llm_client=client,
            )

        self.assertEqual(retrieval.call_count, 10)
        self.assertTrue(all(call.kwargs == {"k": 3} for call in retrieval.call_args_list))
        self.assertEqual(client.call_count, 5)
        self.assertTrue(
            all(call.args[0] == client.call_args_list[0].args[0] for call in client.call_args_list)
        )
        self.assertEqual(len(result), 10)
        self.assertEqual(
            [item["candidate_id"] for item in result],
            [item["candidate_id"] for item in candidates],
        )
        expected_errors = [0.020 + 0.001 * index for index in range(5)]
        self.assertAlmostEqual(
            result[0]["prediction"]["current_error"]["mean"],
            float(np.mean(expected_errors)),
        )
        self.assertAlmostEqual(
            result[0]["prediction"]["current_error"]["std"],
            float(np.std(expected_errors, ddof=1)),
        )
        self.assertEqual(len(result[0]["retrieval_context"]), 3)
        self.assertEqual(len(result[0]["raw_predictions"]), 5)
        self.assertEqual(
            [item["round"] for item in result[0]["raw_predictions"]],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(
            [
                item["metrics"]["current_error"]
                for item in result[0]["raw_predictions"]
            ],
            expected_errors,
        )
        self.assertEqual(
            result[0]["committee_unique_count"],
            {"current_error": 5, "output_resistance": 5},
        )
        self.assertEqual(result[0]["committee_attempt_counts"], [1, 1, 1, 1, 1])

    def test_short_round_is_retried_with_same_prompt(self) -> None:
        candidates = [_candidate(index) for index in range(10)]
        short = json.loads(_round_response(candidates, 0))
        short["predictions"].pop()
        responses = [
            json.dumps(short),
            _round_response(candidates, 0),
            *[_round_response(candidates, index) for index in range(1, 5)],
        ]
        client = MagicMock(side_effect=responses)

        result = predict_candidate_performance(
            candidates,
            [_record(index) for index in range(3)],
            _CIRCUIT_INFO,
            _performance_template(),
            llm_client=client,
        )

        self.assertEqual(client.call_count, 6)
        self.assertEqual(result[0]["committee_attempt_counts"], [2, 1, 1, 1, 1])
        self.assertEqual(client.call_args_list[0].args[0], client.call_args_list[1].args[0])

    def test_extra_prediction_round_is_retried(self) -> None:
        candidates = [_candidate(index) for index in range(10)]
        extra = json.loads(_round_response(candidates, 0))
        extra["predictions"].append(dict(extra["predictions"][0]))
        client = MagicMock(
            side_effect=[
                json.dumps(extra),
                *[_round_response(candidates, index) for index in range(5)],
            ]
        )

        result = predict_candidate_performance(
            candidates,
            [_record(index) for index in range(3)],
            _CIRCUIT_INFO,
            _performance_template(),
            llm_client=client,
        )

        self.assertEqual(client.call_count, 6)
        self.assertEqual(result[0]["committee_attempt_counts"], [2, 1, 1, 1, 1])

    def test_missing_or_duplicate_candidate_id_round_is_retried(self) -> None:
        candidates = [_candidate(index) for index in range(10)]
        malformed_responses = {}
        missing_id = json.loads(_round_response(candidates, 0))
        del missing_id["predictions"][0]["candidate_id"]
        malformed_responses["missing"] = missing_id
        duplicate = json.loads(_round_response(candidates, 0))
        duplicate["predictions"][-1]["candidate_id"] = "candidate_0000"
        malformed_responses["duplicate"] = duplicate

        for case_name, malformed in malformed_responses.items():
            with self.subTest(case=case_name):
                client = MagicMock(
                    side_effect=[
                        json.dumps(malformed),
                        *[
                            _round_response(candidates, index)
                            for index in range(5)
                        ],
                    ]
                )

                result = predict_candidate_performance(
                    candidates,
                    [_record(index) for index in range(3)],
                    _CIRCUIT_INFO,
                    _performance_template(),
                    llm_client=client,
                )

                self.assertEqual(client.call_count, 6)
                self.assertEqual(
                    result[0]["committee_attempt_counts"],
                    [2, 1, 1, 1, 1],
                )

    def test_three_malformed_attempts_raise_round_context(self) -> None:
        candidates = [_candidate(index) for index in range(10)]
        short = json.loads(_round_response(candidates, 0))
        short["predictions"].pop()
        client = MagicMock(return_value=json.dumps(short))

        with self.assertRaisesRegex(
            ValueError,
            r"round 1 failed after 3 attempts \(expected 10 predictions\).*prediction count mismatch",
        ):
            predict_candidate_performance(
                candidates,
                [_record(index) for index in range(3)],
                _CIRCUIT_INFO,
                _performance_template(),
                llm_client=client,
            )

        self.assertEqual(client.call_count, 3)

    def test_retrieval_vectors_use_stable_sorted_normalized_parameter_order(self) -> None:
        candidate = _candidate(0, normalized={"B": 0.8, "A": 0.2})
        records = [
            _record(0, normalized={"B": 0.7, "A": 0.1}),
            _record(1, normalized={"A": 0.3, "B": 0.9}),
        ]
        with patch.object(
            performance_predictor,
            "retrieve_top_k",
            return_value=[0],
        ) as retrieval:
            cases = performance_predictor._build_prediction_cases(
                [candidate],
                records,
                ["current_error", "output_resistance"],
                ["A", "B"],
                3,
            )

        retrieval.assert_called_once_with(
            [0.2, 0.8],
            [[0.1, 0.7], [0.3, 0.9]],
            k=3,
        )
        self.assertEqual(len(cases[0]["historical_context"]), 1)

    def test_retrieval_diagnostics_match_prompt_indices_order_and_distance(self):
        candidate = _candidate(0, normalized={"B": 0.8, "A": 0.2})
        records = [
            _record(0, normalized={"A": 0.1, "B": 0.7}),
            _record(1, normalized={"A": 0.4, "B": 0.9}),
        ]
        with patch.object(
            performance_predictor,
            "retrieve_top_k",
            return_value=[1, 0],
        ):
            cases, diagnostics = performance_predictor._build_prediction_cases(
                [candidate],
                records,
                ["current_error", "output_resistance"],
                ["A", "B"],
                3,
                return_diagnostics=True,
            )

        context = cases[0]["historical_context"]
        logged = diagnostics["candidate_0000"]
        self.assertEqual([item["rank"] for item in logged], [1, 2])
        self.assertEqual(
            [item["sample_id"] for item in logged],
            ["sample_1", "sample_0"],
        )
        self.assertEqual(
            [item["params"] for item in logged],
            [item["params"] for item in context],
        )
        self.assertEqual(
            [item["metrics"] for item in logged],
            [item["metrics"] for item in context],
        )
        self.assertAlmostEqual(logged[0]["distance"], math.sqrt(0.05))
        self.assertAlmostEqual(logged[1]["distance"], math.sqrt(0.02))

    def test_prediction_cases_send_only_physical_candidate_and_history_fields(self) -> None:
        cases = performance_predictor._build_prediction_cases(
            [_candidate(0)],
            [_record(0, functional_feasible=False)],
            ["current_error", "output_resistance"],
            ["A", "B"],
            3,
        )

        self.assertEqual(
            set(cases[0]),
            {"candidate_id", "params", "historical_context"},
        )
        self.assertEqual(
            set(cases[0]["historical_context"][0]),
            {"params", "metrics", "comparison_to_candidate"},
        )

    def test_neighbor_comparison_has_delta_positive_ratio_and_negative_magnitude_ratio(self):
        comparison = performance_predictor._build_neighbor_comparison(
            {
                "IB1": -0.0001010135,
                "R0": 12339.793,
                "mode": "fast",
            },
            {
                "IB1": -0.0000937139,
                "R0": 12337.600,
                "mode": "slow",
            },
        )

        self.assertAlmostEqual(
            comparison["IB1"]["absolute_delta"],
            -0.0001010135 - -0.0000937139,
        )
        self.assertAlmostEqual(
            comparison["IB1"]["magnitude_ratio"],
            0.0001010135 / 0.0000937139,
        )
        self.assertNotIn("relative_ratio", comparison["IB1"])
        self.assertAlmostEqual(
            comparison["R0"]["absolute_delta"],
            12339.793 - 12337.600,
        )
        self.assertAlmostEqual(
            comparison["R0"]["relative_ratio"],
            12339.793 / 12337.600,
        )
        self.assertEqual(
            comparison["mode"],
            {
                "candidate_value": "fast",
                "historical_value": "slow",
                "same": False,
            },
        )

    def test_historical_filter_uses_status_and_required_finite_metrics_only(self) -> None:
        valid_functional_false = _record(0, functional_feasible=False)
        missing_metric = _record(3)
        del missing_metric["metrics"]["current_error"]
        records = [
            valid_functional_false,
            _record(1, parameter_valid=False),
            _record(2, simulation_valid=False),
            missing_metric,
            _record(
                4,
                metrics={"current_error": math.nan, "output_resistance": 1.0e6},
            ),
            _record(
                5,
                metrics={"current_error": 0.01, "output_resistance": math.inf},
            ),
            _record(
                6,
                metrics={"current_error": "0.01", "output_resistance": 1.0e6},
            ),
        ]

        usable = performance_predictor._filter_usable_records(
            records,
            ["current_error", "output_resistance"],
        )

        self.assertEqual(usable, [valid_functional_false])

    def test_no_usable_history_raises_before_llm(self) -> None:
        client = MagicMock()
        with self.assertRaisesRegex(
            ValueError,
            "No usable historical samples for performance prediction",
        ):
            predict_candidate_performance(
                [_candidate(0)],
                [_record(0, simulation_valid=False)],
                _CIRCUIT_INFO,
                _performance_template(),
                llm_client=client,
            )
        client.assert_not_called()

    def test_fewer_than_retrieval_k_uses_all_usable_records(self) -> None:
        cases = performance_predictor._build_prediction_cases(
            [_candidate(0)],
            [_record(0), _record(1)],
            ["current_error", "output_resistance"],
            ["A", "B"],
            3,
        )

        self.assertEqual(len(cases[0]["historical_context"]), 2)

    def test_performance_definitions_come_from_active_template_metrics(self) -> None:
        definitions = performance_predictor._extract_performance_definitions(
            _performance_template()
        )

        self.assertEqual(
            list(definitions),
            ["current_error", "output_resistance"],
        )
        self.assertEqual(definitions["current_error"]["unit"], "ratio")
        self.assertEqual(definitions["output_resistance"]["direction"], "maximize")
        self.assertIn("calculation", definitions["current_error"])
        self.assertNotIn("disabled_metric", definitions)

    def test_prompt_excludes_internal_fields_and_target_specs(self) -> None:
        definitions = performance_predictor._extract_performance_definitions(
            _performance_template()
        )
        cases = performance_predictor._build_prediction_cases(
            [_candidate(0)],
            [_record(0)],
            list(definitions),
            ["A", "B"],
            3,
        )
        prompt = performance_predictor._build_prompt(
            _CIRCUIT_INFO,
            definitions,
            cases,
        )

        for forbidden in (
            "normalized_params",
            "seed_relevance",
            "sampling_source",
            "functional_feasible",
            "parameter_valid",
            "simulation_valid",
            "meets_spec",
            "target_specs",
        ):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("PERFORMANCE DEFINITIONS", prompt)
        self.assertIn("CURRENT MIRROR PERFORMANCE PREDICTION GUIDANCE", prompt)
        self.assertIn("comparison_to_candidate", prompt)
        self.assertIn("ANTI-COPY RULE", prompt)
        self.assertIn("Do not select one neighbor", prompt)

    def test_unsupported_circuit_type_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported circuit type"):
            performance_predictor._build_prompt(
                {"circuit_type": "UnknownCircuit"},
                {"metric": {}},
                [],
            )

    def test_plain_and_fenced_json_are_accepted(self) -> None:
        response = _round_response([_candidate(0)], 0)

        plain = performance_predictor._parse_predictions(
            response,
            ["candidate_0000"],
            ["current_error", "output_resistance"],
        )
        fenced = performance_predictor._parse_predictions(
            f"```json\n{response}\n```",
            ["candidate_0000"],
            ["current_error", "output_resistance"],
        )

        self.assertEqual(plain, fenced)

    def test_missing_duplicate_and_unknown_candidate_ids_raise(self) -> None:
        cases = (
            (
                {"predictions": []},
                ["candidate_0000"],
                "count",
            ),
            (
                {
                    "predictions": [
                        {
                            "candidate_id": "candidate_0000",
                            "predicted_metrics": {
                                "current_error": 0.1,
                                "output_resistance": 1.0,
                            },
                        },
                        {
                            "candidate_id": "candidate_0000",
                            "predicted_metrics": {
                                "current_error": 0.2,
                                "output_resistance": 2.0,
                            },
                        },
                    ]
                },
                ["candidate_0000", "candidate_0001"],
                "duplicate",
            ),
            (
                {
                    "predictions": [
                        {
                            "candidate_id": "unknown",
                            "predicted_metrics": {
                                "current_error": 0.1,
                                "output_resistance": 1.0,
                            },
                        }
                    ]
                },
                ["candidate_0000"],
                "unknown",
            ),
        )
        for payload, candidate_ids, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    performance_predictor._parse_predictions(
                        json.dumps(payload),
                        candidate_ids,
                        ["current_error", "output_resistance"],
                    )

    def test_missing_or_invalid_metric_values_raise(self) -> None:
        invalid_metrics = (
            {"current_error": 0.1},
            {"current_error": "0.1", "output_resistance": 1.0},
            {"current_error": True, "output_resistance": 1.0},
            {"current_error": math.nan, "output_resistance": 1.0},
            {"current_error": 0.1, "output_resistance": math.inf},
        )
        for metrics in invalid_metrics:
            response = json.dumps(
                {
                    "predictions": [
                        {
                            "candidate_id": "candidate_0000",
                            "predicted_metrics": metrics,
                        }
                    ]
                }
            )
            with self.subTest(metrics=metrics):
                with self.assertRaises(ValueError):
                    performance_predictor._parse_predictions(
                        response,
                        ["candidate_0000"],
                        ["current_error", "output_resistance"],
                    )

    def test_malformed_json_and_missing_predictions_raise(self) -> None:
        for response in ("not json", "[]", "{}", '{"predictions": {}}'):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    performance_predictor._parse_predictions(
                        response,
                        ["candidate_0000"],
                        ["current_error"],
                    )

    def test_single_committee_member_has_zero_std(self) -> None:
        candidate = _candidate(0)
        result = predict_candidate_performance(
            [candidate],
            [_record(0)],
            _CIRCUIT_INFO,
            _performance_template(),
            committee_size=1,
            llm_client=MagicMock(return_value=_round_response([candidate], 0)),
        )

        self.assertEqual(result[0]["prediction"]["current_error"]["std"], 0.0)
        self.assertEqual(result[0]["prediction"]["output_resistance"]["std"], 0.0)
        self.assertEqual(len(result[0]["raw_predictions"]), 1)
        self.assertEqual(
            result[0]["committee_unique_count"],
            {"current_error": 1, "output_resistance": 1},
        )

    def test_identical_five_rounds_log_collapse_without_changing_aggregation(self):
        candidate = _candidate(0)
        response = _round_response([candidate], 0)
        client = MagicMock(return_value=response)

        result = predict_candidate_performance(
            [candidate],
            [_record(0)],
            _CIRCUIT_INFO,
            _performance_template(),
            committee_size=5,
            llm_client=client,
        )[0]

        self.assertEqual(client.call_count, 5)
        self.assertEqual(len(result["raw_predictions"]), 5)
        self.assertEqual(
            result["committee_unique_count"],
            {"current_error": 1, "output_resistance": 1},
        )
        self.assertEqual(result["prediction"]["current_error"]["std"], 0.0)
        self.assertEqual(result["prediction"]["output_resistance"]["std"], 0.0)
        self.assertTrue(
            all(
                item["metrics"] == result["raw_predictions"][0]["metrics"]
                for item in result["raw_predictions"]
            )
        )

    def test_final_output_schema_and_candidate_order(self) -> None:
        candidates = [_candidate(2), _candidate(0), _candidate(1)]
        result = predict_candidate_performance(
            candidates,
            [_record(0), _record(1), _record(2)],
            _CIRCUIT_INFO,
            _performance_template(),
            committee_size=1,
            llm_client=MagicMock(return_value=_round_response(candidates, 0)),
        )

        self.assertEqual(
            [item["candidate_id"] for item in result],
            [item["candidate_id"] for item in candidates],
        )
        for item in result:
            self.assertEqual(
                set(item),
                {
                    "candidate_id",
                    "params",
                    "prediction",
                    "retrieval_context",
                    "raw_predictions",
                    "committee_attempt_counts",
                    "committee_unique_count",
                },
            )
            self.assertEqual(
                set(item["prediction"]),
                {"current_error", "output_resistance"},
            )
            self.assertNotIn("meets_spec", item)
            self.assertNotIn("acquisition_score", item)

    def test_empty_candidates_return_without_retrieval_or_llm(self) -> None:
        client = MagicMock()
        self.assertEqual(
            predict_candidate_performance(
                [],
                [],
                _CIRCUIT_INFO,
                _performance_template(),
                llm_client=client,
            ),
            [],
        )
        client.assert_not_called()

    def test_module_has_no_parameter_validation_spice_or_metric_hardcoding(self) -> None:
        source = inspect.getsource(performance_predictor)
        for forbidden in (
            "validate_parameters",
            "PySpice",
            "ngspice",
            "evaluate_current_mirror",
            '"current_error"',
            '"output_resistance"',
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
