"""Pre-physical SCIntegrator deployment retry checks; no simulator or LLM calls."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ActiveLearning.batch_runner_v2 import sc_integrator_production as production


class DeploymentRetryTests(unittest.TestCase):
    circuit_id = "circuit_0662"
    batch_id = "MTV2_SCI_PREP_001"

    def _receipts(self, root: Path) -> tuple[dict, Path, bytes]:
        directory = root / self.batch_id / self.circuit_id
        directory.mkdir(parents=True)
        old = {"status": "BLOCKED", "batch_id": self.batch_id,
               "circuit_id": self.circuit_id, "reason": production._AUTOCONFIG_ERROR}
        (directory / "result.blocked_autoconfig_original.json").write_text(
            json.dumps(old), encoding="utf-8")
        current = {**old, "reason": "FileNotFoundError: " + production._DEPLOYMENT_RESOURCES[0]}
        raw = json.dumps(current, sort_keys=True).encode("utf-8")
        (directory / "result.json").write_bytes(raw)
        spec = {**production.inspect_manifest(self.batch_id),
                "preparation_root": str(directory.parent)}
        return spec, directory, raw

    def test_deployment_resources_and_historical_sources(self):
        spec = production.inspect_manifest(self.batch_id)
        self.assertEqual(len(spec["circuit_ids"]), 10)
        for circuit_id in spec["circuit_ids"]:
            self.assertEqual(len(production._verify_historical_sources(circuit_id)), 7)
        for relative in production._DEPLOYMENT_RESOURCES:
            path = production.ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertGreater(path.stat().st_size, 0, relative)
        fixtures = json.loads((production.ROOT / production._DEPLOYMENT_RESOURCES[0])
                              .read_text(encoding="utf-8"))
        self.assertEqual(fixtures["schema_version"],
                         "sc_integrator_bulk_fixture_overrides_v1")
        self.assertIn("circuit_0662", fixtures["circuits"])
        self.assertIn("circuit_0644", fixtures["circuits"])

    def test_repaired_resource_retry_archives_both_receipts_and_is_one_shot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec, directory, current = self._receipts(root)
            old = (directory / "result.blocked_autoconfig_original.json").read_bytes()
            with patch.object(production, "OUTPUT_ROOT", root / "batch_output"), patch.object(
                    production, "_verify_deployment_resources", return_value={
                        path: "a" * 64 for path in production._DEPLOYMENT_RESOURCES}):
                production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
                with self.assertRaisesRegex(ValueError, "already claimed"):
                    production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
            self.assertEqual((directory / "result.json").read_bytes(), current)
            self.assertEqual((directory / "result.blocked_missing_resource_original.json")
                             .read_bytes(), current)
            self.assertEqual((directory / "result.blocked_autoconfig_original.json")
                             .read_bytes(), old)
            claim = json.loads((directory / "retry_missing_resource_claim.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual(claim["candidate_manifest_sha256"],
                             spec["candidate_manifest_sha256"])
            self.assertEqual(len(claim["source_sha256"]), 7)

    def test_missing_resource_and_source_change_fail_before_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec, directory, _ = self._receipts(root)
            missing = "ActiveLearning/onboarding/nonexistent_fixture_for_offline_test.json"
            with patch.object(production, "OUTPUT_ROOT", root / "batch_output"), patch.object(
                    production, "_DEPLOYMENT_RESOURCES", (missing,)):
                (directory / "result.json").write_text(json.dumps({
                    "status": "BLOCKED", "batch_id": self.batch_id,
                    "circuit_id": self.circuit_id,
                    "reason": "FileNotFoundError: " + missing}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "still missing"):
                    production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
            self.assertFalse((directory / "retry_missing_resource_claim.json").exists())
            with patch.object(production, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "historical source changed"):
                    production._verify_historical_sources(self.circuit_id)

    def test_only_line_ending_normalization_is_accepted(self):
        archived = hashlib.sha256(b"alpha\r\nbeta\r\n").hexdigest()
        self.assertTrue(production._matches_archived_text(b"alpha\r\nbeta\r\n", archived))
        self.assertTrue(production._matches_archived_text(b"alpha\nbeta\n", archived))
        self.assertFalse(production._matches_archived_text(b"alpha\nchanged\n", archived))

    def test_original_autoconfig_retry_remains_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / self.circuit_id
            directory.mkdir()
            old = {"status": "BLOCKED", "batch_id": self.batch_id,
                   "circuit_id": self.circuit_id, "reason": production._AUTOCONFIG_ERROR}
            raw = json.dumps(old).encode()
            (directory / "result.json").write_bytes(raw)
            spec = {"batch_id": self.batch_id, "preparation_root": str(root)}
            with patch.object(production, "OUTPUT_ROOT", root / "output"):
                production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
            self.assertEqual((directory / "result.blocked_autoconfig_original.json")
                             .read_bytes(), raw)
            self.assertFalse((directory / "retry_missing_resource_claim.json").exists())

    def test_physical_artifacts_identity_and_unrelated_failures_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec, directory, current = self._receipts(root)
            output = root / "batch_output"
            with patch.object(production, "OUTPUT_ROOT", output):
                (directory / "auto_target").mkdir()
                with self.assertRaisesRegex(ValueError, "existing Preparation artifacts"):
                    production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
                (directory / "auto_target").rmdir()
                (output / self.batch_id / "native" / self.circuit_id).mkdir(parents=True)
                with self.assertRaisesRegex(ValueError, "physical history"):
                    production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
                (directory / "result.json").write_text(json.dumps({
                    "status": "BLOCKED", "batch_id": "OTHER", "circuit_id": self.circuit_id,
                    "reason": "FileNotFoundError: " + production._DEPLOYMENT_RESOURCES[0]}),
                    encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "physical history"):
                    production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
                (output / self.batch_id / "native" / self.circuit_id).rmdir()
                with self.assertRaisesRegex(ValueError, "limited to"):
                    production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
                (directory / "result.json").write_bytes(current)
                (directory / "result.blocked_autoconfig_original.json").write_text("{}")
                with self.assertRaisesRegex(ValueError, "original AutoConfig receipt"):
                    production._preserve_autoconfig_blocked_receipt(spec, self.circuit_id)
            self.assertFalse((directory / "retry_missing_resource_claim.json").exists())

    def test_only_allowlisted_file_not_found_reason(self):
        path = production._DEPLOYMENT_RESOURCES[0]
        self.assertEqual(production._deployment_missing_path(
            f"FileNotFoundError: [Errno 2] No such file or directory: '{production.ROOT / path}'"),
            path)
        for reason in ("FileNotFoundError: dataset/circuit_0662/topology/circuit_0662.py",
                       "RuntimeError: " + path,
                       "FileNotFoundError: " + path + " and another error"):
            self.assertIsNone(production._deployment_missing_path(reason))

    def test_both_preparation_summaries_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec, directory, _ = self._receipts(root)
            parent = directory.parent
            old = json.dumps({"batch_id": self.batch_id, "blocked": 10,
                              "reason": "AutoConfig"}).encode()
            current = json.dumps({"batch_id": self.batch_id, "blocked": 10,
                                  "reason": "missing fixture"}).encode()
            (parent / "summary.blocked_autoconfig_original.json").write_bytes(old)
            (parent / "summary.json").write_bytes(current)
            production._preserve_autoconfig_blocked_summary(spec)
            self.assertEqual((parent / "summary.blocked_autoconfig_original.json")
                             .read_bytes(), old)
            self.assertEqual((parent / "summary.blocked_missing_resource_original.json")
                             .read_bytes(), current)


if __name__ == "__main__":
    unittest.main()
