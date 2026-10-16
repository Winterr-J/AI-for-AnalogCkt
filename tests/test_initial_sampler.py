from __future__ import annotations

import copy
import inspect
import math
import unittest
from pathlib import Path

import numpy as np
import yaml

from ActiveLearning.InitializeD0.sampling import (
    denormalize_parameters,
    generate_v1_global_samples,
    generate_v2_local_global_samples,
    generate_v3_physical_local_global_samples,
    normalize_parameters,
)
from ActiveLearning.InitializeD0.sampling import initial_sampler


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    ROOT
    / "ActiveLearning"
    / "InitializeD0"
    / "examples"
    / "CurrentMirror"
    / "circuit_0469"
    / "parameter_template.yaml"
)


class InitialSamplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))
        cls.variable_names = cls.template["optimization_space"]["variables"]
        cls.parameters = {
            parameter["name"]: parameter for parameter in cls.template["parameters"]
        }

    def test_default_v1_samples_follow_template_and_bounds(self) -> None:
        samples = generate_v1_global_samples(self.template)

        self.assertEqual(len(samples), 20)
        self.assertEqual(samples[0]["sampling_source"], "anchor")
        self.assertEqual(
            samples[0]["params"],
            {name: self.parameters[name]["nominal_value"] for name in self.variable_names},
        )
        self.assertTrue(
            all(sample["sampling_source"] == "global" for sample in samples[1:])
        )

        for sample in samples:
            for name in self.variable_names:
                bounds = self.parameters[name]["admissible_range"]
                self.assertGreaterEqual(sample["params"][name], bounds["lower"])
                self.assertLessEqual(sample["params"][name], bounds["upper"])
                self.assertGreaterEqual(sample["normalized_params"][name], 0.0)
                self.assertLessEqual(sample["normalized_params"][name], 1.0)

    def test_seed_reproducibility_and_variation(self) -> None:
        first = generate_v1_global_samples(self.template, seed=42)
        repeated = generate_v1_global_samples(self.template, seed=42)
        different = generate_v1_global_samples(self.template, seed=43)

        self.assertEqual(first, repeated)
        self.assertEqual(first[0], different[0])
        self.assertNotEqual(first[1:], different[1:])

    def test_normalization_round_trip(self) -> None:
        anchor = {
            name: self.parameters[name]["nominal_value"] for name in self.variable_names
        }
        normalized = normalize_parameters(anchor, self.template)
        reconstructed = denormalize_parameters(normalized, self.template)

        self.assertEqual(list(normalized), self.variable_names)
        for name in self.variable_names:
            self.assertAlmostEqual(reconstructed[name], anchor[name])

    def test_discrete_variable_is_rejected(self) -> None:
        template = copy.deepcopy(self.template)
        first_name = template["optimization_space"]["variables"][0]
        next(
            parameter
            for parameter in template["parameters"]
            if parameter["name"] == first_name
        )["variable_type"] = "discrete"

        with self.assertRaisesRegex(NotImplementedError, "continuous variables"):
            generate_v1_global_samples(template)

    def test_default_v2_composition_local_box_and_parameter_bounds(self) -> None:
        samples = generate_v2_local_global_samples(self.template)
        anchor = samples[0]

        self.assertEqual(len(samples), 20)
        self.assertEqual(
            [sample["sampling_source"] for sample in samples],
            ["anchor"] + ["local"] * 10 + ["global"] * 9,
        )
        expected_anchor = {
            name: self.parameters[name]["nominal_value"] for name in self.variable_names
        }
        self.assertEqual(anchor["params"], expected_anchor)
        self.assertEqual(
            anchor["normalized_params"],
            normalize_parameters(expected_anchor, self.template),
        )

        for sample in samples[1:11]:
            for name in self.variable_names:
                center = anchor["normalized_params"][name]
                value = sample["normalized_params"][name]
                self.assertGreaterEqual(value, max(0.0, center - 0.05))
                self.assertLessEqual(value, min(1.0, center + 0.05))

        for sample in samples[11:]:
            for value in sample["normalized_params"].values():
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

        for sample in samples:
            for name in self.variable_names:
                bounds = self.parameters[name]["admissible_range"]
                self.assertGreaterEqual(sample["params"][name], bounds["lower"])
                self.assertLessEqual(sample["params"][name], bounds["upper"])

    def test_v2_seed_reproducibility_and_variation(self) -> None:
        first = generate_v2_local_global_samples(self.template, seed=42)
        repeated = generate_v2_local_global_samples(self.template, seed=42)
        different = generate_v2_local_global_samples(self.template, seed=43)

        self.assertEqual(first, repeated)
        self.assertEqual(first[0], different[0])
        self.assertNotEqual(first[1:], different[1:])

    def test_v2_can_return_anchor_only(self) -> None:
        samples = generate_v2_local_global_samples(
            self.template,
            n_local=0,
            n_global=0,
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["sampling_source"], "anchor")

    def test_default_v3_composition_and_physical_local_ranges(self) -> None:
        samples = generate_v3_physical_local_global_samples(self.template)
        nominal = samples[0]

        self.assertEqual(len(samples), 20)
        self.assertEqual(
            [sample["sample_id"] for sample in samples],
            [f"d0_v3_{index:03d}" for index in range(20)],
        )
        self.assertEqual(
            [sample["sampling_source"] for sample in samples],
            ["nominal"] + ["local"] * 10 + ["global"] * 9,
        )
        self.assertEqual(
            nominal["params"],
            {
                name: self.parameters[name]["nominal_value"]
                for name in self.variable_names
            },
        )

        for sample in samples[1:11]:
            for name in self.variable_names:
                nominal_value = nominal["params"][name]
                value = sample["params"][name]
                self.assertEqual(value < 0.0, nominal_value < 0.0)
                ratio = abs(value) / abs(nominal_value)
                self.assertGreaterEqual(ratio, 1.0 / 1.25)
                self.assertLessEqual(ratio, 1.25)

        for sample in samples:
            for name in self.variable_names:
                bounds = self.parameters[name]["admissible_range"]
                self.assertGreaterEqual(sample["params"][name], bounds["lower"])
                self.assertLessEqual(sample["params"][name], bounds["upper"])

        for sample in samples[11:]:
            for value in sample["normalized_params"].values():
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_v3_fixed_sign_values_are_sampled_uniformly_in_log_magnitude(self) -> None:
        samples = generate_v3_physical_local_global_samples(
            self.template,
            n_local=1,
            n_global=0,
            local_factor=1.25,
            seed=42,
        )
        local = samples[1]["params"]
        rng = np.random.default_rng(42)
        expected = {}
        for name in self.variable_names:
            nominal = float(self.parameters[name]["nominal_value"])
            magnitude = math.exp(
                rng.uniform(
                    math.log(abs(nominal) / 1.25),
                    math.log(abs(nominal) * 1.25),
                )
            )
            expected[name] = math.copysign(magnitude, nominal)

        for name in self.variable_names:
            self.assertAlmostEqual(local[name], expected[name])

    def test_v3_seed_reproducibility_and_variation(self) -> None:
        first = generate_v3_physical_local_global_samples(self.template, seed=42)
        repeated = generate_v3_physical_local_global_samples(self.template, seed=42)
        different = generate_v3_physical_local_global_samples(self.template, seed=43)

        self.assertEqual(first, repeated)
        self.assertEqual(first[0], different[0])
        self.assertNotEqual(first[1:], different[1:])

    def test_v3_cross_zero_range_uses_bounded_linear_fallback(self) -> None:
        template = {
            "optimization_space": {"variables": ["X"]},
            "parameters": [
                {
                    "name": "X",
                    "variable_type": "continuous",
                    "nominal_value": 0.2,
                    "admissible_range": {"lower": -1.0, "upper": 1.0},
                }
            ],
        }
        samples = generate_v3_physical_local_global_samples(
            template,
            n_local=10,
            n_global=0,
            local_factor=1.25,
        )

        for sample in samples[1:]:
            self.assertGreaterEqual(sample["params"]["X"], 0.15)
            self.assertLessEqual(sample["params"]["X"], 0.25)

    def test_sampler_has_no_circuit_or_spice_dependency(self) -> None:
        source = inspect.getsource(initial_sampler)
        for forbidden in (
            "circuit_0469",
            "IB1",
            "R0",
            "R1",
            "PySpice",
            "ngspice",
            "evaluate_current_mirror",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
