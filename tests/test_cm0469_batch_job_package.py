"""Read-only CM0469 Batch input checks; no provider or SPICE calls."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from ActiveLearning import main as al_main
from ActiveLearning.active_learning import _recompute_initial_meets_spec
from ActiveLearning.batch_runner_v2.schema import verify_job


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / (
    "ActiveLearning/batch_inputs/CurrentMirror/circuit_0469/"
    "independent_d0_v2_93a6a79bac93"
)


class CM0469BatchJobPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.job = json.loads((PACKAGE / "job.json").read_text(encoding="utf-8"))
        cls.manifest = {
            "repository_root": str(ROOT),
            "schema_version": "batch_runner_v2b2a_candidate_v1",
        }

    def test_job_binds_copied_config_and_real_d0(self):
        verify_job(self.manifest, self.job)
        config = PACKAGE / "active_learning_config.yaml"
        _paths, loaded, targets, d0 = al_main._resolve_experiment(
            "CurrentMirror", "circuit_0469", config, None
        )
        al_main._check_independent_d0(loaded, d0)
        self.assertEqual(d0, PACKAGE / "d0/d0_v1_seed42.jsonl")
        rows = al_main._load_jsonl(d0)
        self.assertEqual(len(rows), 20)
        self.assertIsNone(_recompute_initial_meets_spec(rows, targets))
        for sample_id in ("d0_v3_013", "d0_v3_018"):
            row = next(row for row in rows if row["sample_id"] == sample_id)
            self.assertIs(row["status"]["functional_feasible"], False)
            self.assertIsNone(row["status"]["meets_spec"])

    def test_missing_or_wrong_d0_fails_closed(self):
        for replacement in (
            {"d0_path": self.job["d0_path"] + ".missing"},
            {"d0_sha256": "0" * 64},
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaises((OSError, ValueError)):
                    verify_job(self.manifest, {**self.job, **replacement})

    def test_config_or_source_mismatch_fails_closed(self):
        for replacement in (
            {"config_sha256": "0" * 64},
            {"source_sha256": "0" * 64},
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaises(ValueError):
                    verify_job(self.manifest, {**self.job, **replacement})


if __name__ == "__main__":
    unittest.main()
