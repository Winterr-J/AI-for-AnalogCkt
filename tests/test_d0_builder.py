from __future__ import annotations

import csv
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from ActiveLearning.InitializeD0 import d0_builder


TEMPLATE = {"template": "sentinel"}
ADAPTER = object()
FUNCTIONAL_CONFIG = {
    "compliance_vout_start": 0.5,
    "compliance_vout_stop": 3.0,
}


def _sample(sample_id="sample_0", source="anchor", value=1.0):
    return {
        "sample_id": sample_id,
        "sampling_source": source,
        "params": {"X": value},
        "normalized_params": {"X": value / 10.0},
    }


@contextmanager
def _patched_pipeline(
    *,
    samples=None,
    parameter_result=None,
    evaluation_result=(0.02, 2.0e6, True),
    functional_feasible=True,
    meets_spec=None,
):
    samples = samples or [_sample()]
    v1 = Mock(return_value=samples)
    v2 = Mock(return_value=samples)
    v3 = Mock(return_value=samples)
    validate = Mock(
        return_value=parameter_result
        if parameter_result is not None
        else {"parameter_valid": True, "violations": []}
    )
    evaluate = Mock(return_value=evaluation_result)
    functional = Mock(
        return_value={
            "functional_feasible": functional_feasible,
            "compliance": {"passed": functional_feasible, "relative_variation": 0.1},
            "reference_tracking": {
                "passed": True,
                "max_tracking_error": 0.02,
            },
        }
    )
    spec = Mock(return_value={"meets_spec": meets_spec})
    with (
        patch.object(d0_builder, "generate_v1_global_samples", v1),
        patch.object(d0_builder, "generate_v2_local_global_samples", v2),
        patch.object(d0_builder, "generate_v3_physical_local_global_samples", v3),
        patch.object(d0_builder, "validate_parameters", validate),
        patch.object(d0_builder, "evaluate_current_mirror", evaluate),
        patch.object(d0_builder, "check_current_mirror_functionality", functional),
        patch.object(d0_builder, "check_meets_spec", spec),
    ):
        yield v1, v2, v3, validate, evaluate, functional, spec


def _build(**kwargs):
    return d0_builder.build_current_mirror_d0(
        TEMPLATE,
        ADAPTER,
        FUNCTIONAL_CONFIG,
        **kwargs,
    )


class D0BuilderTests(unittest.TestCase):
    def test_v1_sampler_is_selected_with_expected_arguments(self) -> None:
        with _patched_pipeline() as (v1, v2, v3, *_):
            _build(sampling_mode="v1", n_samples=7, seed=13)

        v1.assert_called_once_with(TEMPLATE, n_samples=7, seed=13)
        v2.assert_not_called()
        v3.assert_not_called()

    def test_v2_sampler_is_selected_with_expected_arguments(self) -> None:
        with _patched_pipeline() as (v1, v2, v3, *_):
            _build(
                sampling_mode="v2",
                n_local=4,
                n_global=3,
                local_radius=0.2,
                seed=17,
            )

        v2.assert_called_once_with(
            TEMPLATE,
            n_local=4,
            n_global=3,
            local_radius=0.2,
            seed=17,
        )
        v1.assert_not_called()
        v3.assert_not_called()

    def test_v3_sampler_is_selected_with_expected_arguments(self) -> None:
        with _patched_pipeline() as (v1, v2, v3, *_):
            _build(
                sampling_mode="v3",
                n_local=4,
                n_global=3,
                local_factor=1.4,
                seed=17,
            )

        v3.assert_called_once_with(
            TEMPLATE,
            n_local=4,
            n_global=3,
            local_factor=1.4,
            seed=17,
        )
        v1.assert_not_called()
        v2.assert_not_called()

    def test_parameter_invalid_skips_all_downstream_stages(self) -> None:
        with _patched_pipeline(
            parameter_result={"parameter_valid": False, "violations": ["bad"]},
        ) as (*_, evaluate, functional, spec):
            record = _build()[0]

        evaluate.assert_not_called()
        functional.assert_not_called()
        spec.assert_not_called()
        self.assertEqual(
            record["metrics"],
            {"current_error": None, "output_resistance": None},
        )
        self.assertEqual(
            record["status"],
            {
                "parameter_valid": False,
                "simulation_valid": None,
                "functional_feasible": None,
                "meets_spec": None,
            },
        )

    def test_simulation_invalid_skips_functional_and_spec(self) -> None:
        with _patched_pipeline(evaluation_result=(None, None, False)) as (
            *_,
            evaluate,
            functional,
            spec,
        ):
            record = _build()[0]

        evaluate.assert_called_once()
        functional.assert_not_called()
        spec.assert_not_called()
        self.assertEqual(
            record["status"],
            {
                "parameter_valid": True,
                "simulation_valid": False,
                "functional_feasible": None,
                "meets_spec": None,
            },
        )

    def test_simulation_success_calls_functional_with_config(self) -> None:
        with _patched_pipeline() as (*_, functional, _spec):
            _build()

        functional.assert_called_once_with(
            {"X": 1.0},
            adapter=ADAPTER,
            **FUNCTIONAL_CONFIG,
        )

    def test_spec_is_independent_of_failed_functional_check(self) -> None:
        target_specs = {"current_error": {"operator": "<=", "value": 0.05}}
        with _patched_pipeline(
            functional_feasible=False,
            meets_spec=True,
        ) as (*_, functional, spec):
            record = _build(target_specs=target_specs)[0]

        functional.assert_called_once()
        spec.assert_called_once_with(
            {"current_error": 0.02, "output_resistance": 2.0e6},
            target_specs,
        )
        self.assertIs(record["status"]["functional_feasible"], False)
        self.assertIs(record["status"]["meets_spec"], True)

    def test_none_target_specs_preserves_unknown_meets_spec(self) -> None:
        with _patched_pipeline(meets_spec=None):
            record = _build(target_specs=None)[0]

        self.assertIsNone(record["status"]["meets_spec"])

    def test_all_sampled_points_and_failure_states_are_retained(self) -> None:
        samples = [
            _sample("invalid_parameter", "anchor", -1.0),
            _sample("failed_simulation", "global", 0.0),
            _sample("successful", "global", 1.0),
        ]
        with _patched_pipeline(samples=samples) as (
            _v1,
            _v2,
            _v3,
            validate,
            evaluate,
            functional,
            spec,
        ):
            validate.side_effect = lambda params, _template: {
                "parameter_valid": params["X"] >= 0.0,
                "violations": [],
            }
            evaluate.side_effect = lambda params, adapter: (
                (None, None, False)
                if params["X"] == 0.0
                else (0.02, 2.0e6, True)
            )
            records = _build()

        self.assertEqual(
            [record["sample_id"] for record in records],
            ["invalid_parameter", "failed_simulation", "successful"],
        )
        self.assertEqual(len(records), len(samples))
        self.assertEqual(functional.call_count, 1)
        self.assertEqual(spec.call_count, 1)

    def test_success_record_schema_and_raw_metrics(self) -> None:
        with _patched_pipeline(functional_feasible=False, meets_spec=None):
            record = _build()[0]

        self.assertEqual(
            set(record),
            {
                "sample_id",
                "sampling_source",
                "params",
                "normalized_params",
                "metrics",
                "functional_metrics",
                "status",
            },
        )
        self.assertEqual(
            record["metrics"],
            {"current_error": 0.02, "output_resistance": 2.0e6},
        )
        self.assertEqual(
            record["functional_metrics"],
            {
                "compliance_relative_variation": 0.1,
                "tracking_max_error": 0.02,
            },
        )
        self.assertEqual(
            record["status"],
            {
                "parameter_valid": True,
                "simulation_valid": True,
                "functional_feasible": False,
                "meets_spec": None,
            },
        )

    def test_unsupported_sampling_mode_raises(self) -> None:
        with _patched_pipeline() as (v1, v2, v3, *_):
            with self.assertRaisesRegex(ValueError, "sampling_mode"):
                _build(sampling_mode="combined")

        v1.assert_not_called()
        v2.assert_not_called()
        v3.assert_not_called()

    def test_save_d0_dataset_writes_complete_jsonl_and_csv(self) -> None:
        records = [
            {
                "sample_id": f"sample_{index}",
                "sampling_source": "anchor" if index == 0 else "global",
                "params": {"X": float(index + 1)},
                "normalized_params": {"X": float(index) / 2.0},
                "metrics": {
                    "current_error": 0.02 if index == 0 else None,
                    "output_resistance": 2.0e6 if index == 0 else None,
                },
                "functional_metrics": {
                    "compliance_relative_variation": 0.1 if index == 0 else None,
                    "tracking_max_error": 0.02 if index == 0 else None,
                },
                "status": {
                    "parameter_valid": True,
                    "simulation_valid": True if index == 0 else False,
                    "functional_feasible": False if index == 0 else None,
                    "meets_spec": None,
                },
            }
            for index in range(2)
        ]

        with TemporaryDirectory() as directory:
            paths = d0_builder.save_d0_dataset(records, directory, "test_d0")
            self.assertTrue(paths["jsonl"].is_file())
            self.assertTrue(paths["csv"].is_file())
            jsonl_records = [
                json.loads(line)
                for line in paths["jsonl"].read_text(encoding="utf-8").splitlines()
            ]
            with paths["csv"].open(encoding="utf-8", newline="") as stream:
                csv_rows = list(csv.DictReader(stream))

        self.assertEqual(len(jsonl_records), len(records))
        self.assertEqual(len(csv_rows), len(records))
        self.assertEqual(jsonl_records[0]["sample_id"], "sample_0")
        self.assertEqual(jsonl_records[0]["params"], {"X": 1.0})
        self.assertEqual(jsonl_records[0]["metrics"]["current_error"], 0.02)
        self.assertEqual(
            set(jsonl_records[0]["status"]),
            {
                "parameter_valid",
                "simulation_valid",
                "functional_feasible",
                "meets_spec",
            },
        )
        self.assertEqual(csv_rows[0]["sample_id"], "sample_0")
        self.assertEqual(csv_rows[0]["parameter_valid"], "True")


if __name__ == "__main__":
    unittest.main()
