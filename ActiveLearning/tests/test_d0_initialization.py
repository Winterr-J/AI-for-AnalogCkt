from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ActiveLearning import d0_initialization


class ConfiguredD0InitializationTests(unittest.TestCase):
    def test_autoconfig_structure_and_experiment_sampling_are_forwarded(self):
        runtime = SimpleNamespace(
            circuit_type="CurrentMirror",
            param_template={"source": "autoconfig"},
            adapter=SimpleNamespace(vout_nominal=1.65),
        )
        config = {
            "data": {
                "sampling_mode": "v3",
                "seed": 42,
                "n_local": 10,
                "n_global": 9,
                "local_factor": 1.25,
            }
        }
        targets = {
            "current_error": {"operator": "<=", "value": 0.10},
            "output_resistance": {"operator": ">=", "value": 55e6},
        }
        records = [{"sample_id": "d0_v3_000"}]

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "d0_v3_seed42.jsonl"
            artifacts = {
                "jsonl": destination,
                "csv": destination.with_suffix(".csv"),
            }
            with (
                patch.object(
                    d0_initialization,
                    "build_runtime_d0",
                    return_value=records,
                ) as builder,
                patch.object(
                    d0_initialization,
                    "save_d0_dataset",
                    return_value=artifacts,
                ) as saver,
            ):
                result = d0_initialization.initialize_configured_d0(
                    runtime,
                    config,
                    targets,
                    destination,
                )

        kwargs = builder.call_args.kwargs
        self.assertIs(builder.call_args.args[0], runtime)
        self.assertAlmostEqual(
            kwargs["functional_config"]["compliance_vout_start"],
            0.65,
        )
        self.assertEqual(kwargs["target_specs"], targets)
        self.assertEqual(kwargs["sampling_mode"], "v3")
        self.assertEqual(kwargs["seed"], 42)
        self.assertEqual(kwargs["n_local"], 10)
        self.assertEqual(kwargs["n_global"], 9)
        self.assertEqual(kwargs["local_factor"], 1.25)
        saver.assert_called_once_with(records, destination.parent, destination.stem)
        self.assertEqual(result["records"], records)


if __name__ == "__main__":
    unittest.main()
