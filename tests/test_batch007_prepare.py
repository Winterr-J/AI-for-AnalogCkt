"""Mock-only contracts for the fixed Batch 007 onboarding wrapper."""
from __future__ import annotations

import hashlib
import json
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from ActiveLearning import batch007_prepare as batch


MANIFEST = batch.ROOT / "ActiveLearning/batch007/legacy_candidate_manifest.json"
SOURCE = batch.ROOT / "dataset"


class Batch007PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "new_output"
        self.rows = batch.load_fixed_candidates(MANIFEST)

    def test_fixed_manifest_order_and_windows_asset_mapping(self):
        self.assertEqual([row["circuit_id"] for row in self.rows], list(batch.IDS))
        self.assertEqual(
            batch._relative_legacy(self.rows[0]["runtime_required_paths"][0], "circuit_1621"),
            Path("circuit_1621/topology/circuit_1621.py"))
        altered = list(reversed(self.rows))
        path = Path(self.temp.name) / "altered.json"
        path.write_text(json.dumps(altered), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "order"):
            batch.load_fixed_candidates(path)

    def test_dry_run_all_ten_is_read_only_and_target_missing(self):
        for row in self.rows:
            result = batch.inspect_candidate(row, SOURCE, self.output)
            self.assertEqual(result["status"], "TARGET_PREPARATION_REQUIRED")
            self.assertEqual(len(result["asset_hashes"]), 8)
            self.assertTrue(result["autoconfig"]["nominal_valid"])
            self.assertEqual(result["evaluator"], "OpAmpEvaluator")
            self.assertEqual(result["functional_validator"], "OpAmpFunctionalValidator")
            self.assertIn("20-row independent D0", result["missing"])
        self.assertFalse(self.output.exists())

    def test_missing_asset_is_blocked_without_output(self):
        with patch.object(batch, "ROOT", Path(self.temp.name)):
            result = batch.inspect_candidate(self.rows[0], SOURCE, self.output)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("topology/circuit_1621.py", result["missing"])
        self.assertFalse(self.output.exists())

    def test_existing_untrusted_directory_is_not_overwritten(self):
        directory = self.output / "circuit_1621"
        directory.mkdir(parents=True)
        marker = directory / "historical.txt"
        marker.write_text("preserve", encoding="utf-8")
        result = batch.inspect_candidate(self.rows[0], SOURCE, self.output)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_complete_hash_bound_d0_reused_and_tamper_refused(self):
        directory = self.output / "circuit_1621"
        run = directory / "run_a"
        d0_dir = run / "d0"
        d0_dir.mkdir(parents=True)
        config = run / "active_learning_config.yaml"
        config.write_text(yaml.safe_dump({"data": {"d0_mode": "independent",
            "d0_path": "d0/d0_v4_seed42.jsonl", "seed": 42,
            "n_samples": 20, "n_local": 10, "n_global": 9,
            "local_radius": 0.02, "sampling_mode": "v4"},
            "target_specs": {"phase_margin_deg": {"operator": ">=", "value": 60.0}}}), encoding="utf-8")
        d0 = d0_dir / "d0_v4_seed42.jsonl"
        d0.write_text("".join(json.dumps({"status": {"functional_valid": True}})
                               + "\n" for _ in range(20)), encoding="utf-8")
        csv = d0.with_suffix(".csv")
        csv.write_text("sample_id\n", encoding="utf-8")
        init = d0_dir / "d0_v4_seed42_initialization.json"
        init.write_text(json.dumps({"d0_mode": "independent", "seed": 42,
            "records": 20, "sampling": {"sampling_mode": "v4"}}),
            encoding="utf-8")
        artifacts = {p.relative_to(directory).as_posix(): batch._sha(p)
                     for p in (config, d0, csv, init)}
        source_hashes = batch.inspect_candidate(self.rows[0], SOURCE, Path(self.temp.name) / "absent")["asset_hashes"]
        receipt = {"circuit_id": "circuit_1621", "status": "PREPARATION_READY",
                   "source_sha256": source_hashes, "artifacts": artifacts,
                   "records": 20, "seed": 42, "sampling_mode": "v4",
                   "local_radius": 0.02,
                   "functional_valid_count": 20}
        (directory / "result.json").write_text(json.dumps(receipt), encoding="utf-8")
        self.assertEqual(batch.inspect_candidate(self.rows[0], SOURCE, self.output)["status"],
                         "PREPARATION_READY")
        d0.write_text("tampered", encoding="utf-8")
        self.assertEqual(batch.inspect_candidate(self.rows[0], SOURCE, self.output)["status"],
                         "BLOCKED")

    def test_real_mode_requires_explicit_external_approval_inputs(self):
        args = ["--candidate-manifest", str(MANIFEST),
                "--source-dataset-root", str(SOURCE), "--output-root", str(self.output),
                "--execute-preparation"]
        with self.assertRaises(SystemExit):
            batch.main(args)
        self.assertFalse(self.output.exists())

    def test_mocked_preparation_preserves_failed_circuit_isolation(self):
        args = SimpleNamespace(seed=42, max_anchor_evaluations=2, profile_samples=2,
                               max_llm_calls=1, provider="gemini", model="offline",
                               d0_local_radius=0.02, target_timeout_seconds=2,
                               d0_timeout_seconds=2)
        def fake_bounded(command, _timeout, _log_path):
            if "ActiveLearning.auto_target" in command:
                circuit_id = command[command.index("--circuit-id") + 1]
                if circuit_id == "circuit_1621":
                    raise RuntimeError("offline synthetic target failure")
                root = self.output / circuit_id / "run_a"
                root.mkdir(parents=True)
                config = root / "active_learning_config.yaml"
                config.write_text(yaml.safe_dump({"data": {"d0_mode": "independent",
                    "d0_path": "d0/d0_v3_seed42.jsonl", "seed": 42},
                    "target_specs": {"phase_margin_deg": {"operator": ">=", "value": 60.0}}}),
                    encoding="utf-8")
                return {"status": "READY_FOR_D0", "artifact_paths": {"config": str(config)}}
            config = Path(command[command.index("--config") + 1])
            data = yaml.safe_load(config.read_text(encoding="utf-8"))["data"]
            d0 = config.parent / data["d0_path"]
            d0.parent.mkdir()
            d0.write_text("".join(json.dumps({"status": {
                "simulation_valid": True, "functional_valid": True,
                "meets_spec": False}}) + "\n" for _ in range(20)), encoding="utf-8")
            d0.with_suffix(".csv").write_text("sample_id\n", encoding="utf-8")
            d0.with_name(d0.stem + "_initialization.json").write_text(
                '{"d0_mode":"independent","seed":42}', encoding="utf-8")
            return {}
        with patch.object(batch, "_bounded", side_effect=fake_bounded):
            first = batch.prepare_candidate(self.rows[0], SOURCE, self.output, args)
            second = batch.prepare_candidate(self.rows[1], SOURCE, self.output, args)
        self.assertEqual(first["status"], "BLOCKED")
        self.assertEqual(second["status"], "PREPARATION_READY")
        self.assertFalse((self.output / "circuit_1621" / "result.json").exists())
        self.assertTrue((self.output / "circuit_1622" / "result.json").is_file())
        self.assertEqual(yaml.safe_load(next((self.output / "circuit_1622").rglob(
            "active_learning_config.yaml")).read_text())["data"]["sampling_mode"], "v4")
    def test_source_asset_mismatch_fails_closed(self):
        circuit_id = "circuit_1621"
        alternate = Path(self.temp.name) / "dataset"
        for template in batch.BUNDLE:
            relative = template.format(id=circuit_id)
            destination = alternate / circuit_id / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((SOURCE / circuit_id / relative).read_bytes())
        (alternate / circuit_id / "metadata.txt").write_text("altered", encoding="utf-8")
        result = batch.inspect_candidate(self.rows[0], alternate, self.output)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("source byte identity mismatch", result["reason"])
        self.assertFalse(self.output.exists())

    def test_bounded_mock_child_log_and_timeout_cleanup(self):
        success_log = Path(self.temp.name) / "success.log"
        result = batch._bounded([sys.executable, "-c", "print('offline-ok')"],
                                5, success_log)
        self.assertEqual(result["stdout"], "offline-ok")
        self.assertIn("timed_out=false", success_log.read_text(encoding="utf-8"))
        timeout_log = Path(self.temp.name) / "timeout.log"
        with self.assertRaises(TimeoutError):
            batch._bounded([sys.executable, "-c",
                            "import time; print('started', flush=True); time.sleep(30)"],
                           1, timeout_log)
        self.assertIn("timed_out=true", timeout_log.read_text(encoding="utf-8"))
        self.assertIn("started", timeout_log.read_text(encoding="utf-8"))




if __name__ == "__main__":
    unittest.main()
