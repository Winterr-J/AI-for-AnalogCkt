"""Offline-only equivalence and cost checks for SCIntegrator prediction prompts."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from ActiveLearning.batch_runner_v2 import sc_integrator_production as production
from ActiveLearning.performance_prediction import performance_predictor as predictor
from ActiveLearning.performance_prediction import sc_integrator_compact_v2 as compact
from ActiveLearning.performance_prediction.sample_hold_compact import build_components


METRICS = (
    "integration_step_magnitude_v", "zero_input_drift_v_per_cycle",
    "step_consistency_v", "virtual_ground_error_v", "output_headroom_v",
)
CIRCUIT = {"circuit_id": "circuit_2480", "circuit_type": "SCIntegrator",
           "simulation_environment": "ideal"}
PARAMS = ("C1", "C2", "R1", "R2", "W1", "W2")


def _template():
    definitions = (
        ("V", "maximize", "Absolute baseline-corrected mean output step per cycle."),
        ("V/cycle", "minimize", "Mean absolute baseline output step with zero input."),
        ("V", "minimize", "Deviation of six baseline-corrected integration steps."),
        ("V", "minimize", "RMS virtual-ground voltage over six transfer samples."),
        ("V", "maximize", "Minimum measured distance from either supply rail."),
    )
    return {"performance_template": {
        "active_metrics": list(METRICS),
        "metrics": {name: {"name": name, "unit": unit, "direction": direction,
                           "description": description}
                    for name, (unit, direction, description)
                    in zip(METRICS, definitions, strict=True)}}}


def _params(index):
    return {name: (index + 1) * (position + 1) * 1e-9
            for position, name in enumerate(PARAMS)}


def _history():
    rows = []
    for index in range(20):
        params = _params(index)
        rows.append({"sample_id": f"d0_{index:02d}", "params": params,
                     "normalized_params": params,
                     "metrics": {name: (index + 1) * (m + 1) * 1e-3
                                 for m, name in enumerate(METRICS)},
                     "status": {"parameter_valid": True, "simulation_valid": True,
                                "functional_valid": index != 2}})
    return rows


def _candidates():
    return [{"candidate_id": f"candidate_{index:02d}",
             "params": _params(index), "normalized_params": _params(index)}
            for index in range(10)]


def _cases():
    records = predictor._filter_usable_records(_history(), list(METRICS))
    return predictor._build_prediction_cases(
        _candidates(), records, list(METRICS), sorted(PARAMS), 3,
        return_diagnostics=True,
    )


def _restore_comparisons(cases):
    return [
        {"candidate_id": case["candidate_id"], "params": case["params"],
         "historical_context": [
             {"params": neighbor["params"], "metrics": neighbor["metrics"],
              "comparison_to_candidate": predictor._build_neighbor_comparison(
                  case["params"], neighbor["params"])}
             for neighbor in case["historical_context"]]}
        for case in cases
    ]


def _restore_pool(pool, cases):
    by_id = {item["id"]: item for item in pool}
    return [
        {"candidate_id": case["candidate_id"], "params": case["params"],
         "historical_context": [
             {"params": by_id[link["id"]]["params"],
              "metrics": by_id[link["id"]]["metrics"],
              "comparison_to_candidate": predictor._build_neighbor_comparison(
                  case["params"], by_id[link["id"]]["params"])}
             for link in case["neighbors"]]}
        for case in cases
    ]


class SCIntegratorCompactV2Tests(unittest.TestCase):
    def test_v1_p0_v2_restore_all_values_and_physical_guidance(self):
        cases, diagnostics = _cases()
        self.assertTrue(any(item["sample_id"] == "d0_02"
                            for items in diagnostics.values() for item in items))
        p0_cases = compact.build_p0_components(cases)
        pool, v2_cases = build_components(cases, diagnostics)
        self.assertEqual(_restore_comparisons(p0_cases), cases)
        self.assertEqual(_restore_pool(pool, v2_cases), cases)
        self.assertEqual([case["candidate_id"] for case in v2_cases],
                         [case["candidate_id"] for case in cases])
        self.assertEqual([len(case["neighbors"]) for case in v2_cases], [3] * 10)
        for original, compact_case in zip(cases, v2_cases, strict=True):
            for neighbor, link in zip(original["historical_context"],
                                      compact_case["neighbors"], strict=True):
                self.assertEqual(link["comparison_to_candidate"],
                                 compact._without_repeated_values(
                                     neighbor["comparison_to_candidate"]))
        self.assertEqual(len(pool), 11)
        definitions = predictor._extract_performance_definitions(_template())
        v1 = predictor._build_prompt(CIRCUIT, definitions, cases)
        p0 = compact.build_p0_prompt(CIRCUIT, definitions, cases)
        v2 = compact.build_prompt(CIRCUIT, definitions, cases, diagnostics)
        self.assertIn(json.dumps(cases, indent=2, ensure_ascii=False,
                                 sort_keys=True), v1)
        self.assertIn(compact._compact_json(p0_cases), p0)
        for label, value in (
            ("Circuit information", CIRCUIT),
            ("Metric names, units, directions and physical definitions", definitions),
            ("Measured historical pool", pool),
            ("Ordered prediction cases", v2_cases),
        ):
            line = next(line for line in v2.splitlines() if line.startswith(label + ": "))
            self.assertEqual(json.loads(line[len(label) + 2:]), value)
        self.assertEqual(tuple(definitions), METRICS)
        for name in METRICS:
            self.assertIn(name, v1)
            self.assertIn(name, p0)
            self.assertIn(name, v2)
        for phrase in ("functionally valid", "absolute magnitude", "polarity",
                       "virtual-ground", "clock phase", "output headroom"):
            self.assertIn(phrase, v2)
        self.assertLess(len(p0), len(v1))
        self.assertLess(len(v2), len(p0))
        links = sum(len(case["historical_context"]) for case in cases)
        report = {
            "fixture": "synthetic_sc_integrator_10_candidates_20_history",
            "candidates": len(cases), "neighbor_links": links,
            "unique_neighbors": len(pool), "repeated_expansions": links - len(pool),
            "versions": {
                "v1": {"characters": len(v1), "utf8_bytes": len(v1.encode()),
                       "json_bytes": sum(len(json.dumps(value, indent=2,
                           ensure_ascii=False, sort_keys=True).encode())
                           for value in (CIRCUIT, definitions, cases))},
                "p0": {"characters": len(p0), "utf8_bytes": len(p0.encode()),
                       "json_bytes": sum(len(compact._compact_json(value).encode())
                           for value in (CIRCUIT, definitions, p0_cases))},
                "v2": {"characters": len(v2), "utf8_bytes": len(v2.encode()),
                       "json_bytes": sum(len(compact._compact_json(value).encode())
                           for value in (CIRCUIT, definitions, pool, v2_cases))},
            },
        }
        baseline = report["versions"]["v1"]["utf8_bytes"]
        for version in ("p0", "v2"):
            report["versions"][version]["byte_reduction_ratio"] = round(
                1 - report["versions"][version]["utf8_bytes"] / baseline, 6)
        print("OFFLINE_PROMPT_COMPARISON " + json.dumps(report, sort_keys=True))

    def test_v1_default_and_v2_mock_committee_same_schema(self):
        candidates, records, template = _candidates(), _history(), _template()
        expected = json.dumps({"predictions": [
            {"candidate_id": candidate["candidate_id"],
             "predicted_metrics": {name: float(index + metric + 1)
                                   for metric, name in enumerate(METRICS)}}
            for index, candidate in enumerate(candidates)]})
        results = {}
        for version in ("v1", "compact_v2"):
            prompts = []

            def client(prompt):
                prompts.append(prompt)
                return expected

            results[version] = predictor.predict_candidate_performance(
                candidates, records, CIRCUIT, template, retrieval_k=3,
                committee_size=5, prompt_version=version, llm_client=client)
            self.assertEqual(len(prompts), 5)
            self.assertEqual(len(set(prompts)), 1)
            self.assertEqual(results[version][0]["committee_attempt_counts"],
                             [1, 1, 1, 1, 1])
        self.assertEqual(results["v1"], results["compact_v2"])
        implicit = predictor.predict_candidate_performance(
            candidates, records, CIRCUIT, template, retrieval_k=3,
            committee_size=5, llm_client=lambda _: expected)
        self.assertEqual(implicit, results["v1"])
        with self.assertRaisesRegex(ValueError, "SCIntegrator only"):
            predictor.predict_candidate_performance(
                candidates, records, {**CIRCUIT, "circuit_type": "OpAmp"},
                template, prompt_version="compact_v2", llm_client=lambda _: expected)

    def test_preparation_cli_is_opt_in_and_defaults_to_v1(self):
        with patch.object(production, "preparation", return_value={"status": "mock"}) as call:
            production.main(["preparation", "--batch-id", "MTV2_SCI_PREP_082",
                             "--allow-spice", "--allow-llm"])
            self.assertEqual(call.call_args.kwargs["prediction_prompt_version"], "v1")
            production.main(["preparation", "--batch-id", "MTV2_SCI_PREP_082",
                             "--allow-spice", "--allow-llm",
                             "--prediction-prompt-version", "compact_v2"])
            self.assertEqual(call.call_args.kwargs["prediction_prompt_version"],
                             "compact_v2")


if __name__ == "__main__":
    unittest.main()
