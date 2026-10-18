"""Offline-only contract tests for an external B2A manifest."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ActiveLearning.batch_runner_v2.authorization import (
    AuthorizationError, current_source_revision,
)
from ActiveLearning.batch_runner_v2.runner import plan_batch, run_authorized_batch
from ActiveLearning.batch_runner_v2.schema import (
    _REQUIRED_REAL_AL_CLOSURE, _label, _model, load_manifest,
)
from ActiveLearning.batch_runner_v2.storage import sha256_file


ROOT = Path(__file__).resolve().parents[1]
JOB_PATH = ROOT / (
    "ActiveLearning/batch_inputs/CurrentMirror/circuit_0469/"
    "independent_d0_v2_93a6a79bac93/job.json"
)


class ExternalManifestContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manifest_path = Path(self.temp.name) / "manifest.json"
        self.payload = {
            "schema_version": "batch_runner_v2b2a_candidate_v1",
            "batch_id": "cm0469_contract_test",
            "provider": "gemini",
            "model": "gemini-3.5-flash-lite",
            "credential_scope": "GEMINI_API_KEY",
            "resource_limits": {
                "max_physical_attempts": 60, "worker_timeout_seconds": 14400,
            },
            "retry_policy": {
                "request_timeout_seconds": 120,
                "max_attempts": 3, "max_elapsed_seconds": 1800,
            },
            "source_revision": current_source_revision(ROOT),
            "source_closure": {
                name: sha256_file(ROOT / name)
                for name in sorted(_REQUIRED_REAL_AL_CLOSURE)
            },
            "repository_root": str(ROOT),
            "jobs": [json.loads(JOB_PATH.read_text(encoding="utf-8"))],
        }

    def write(self, payload=None):
        self.manifest_path.write_text(
            json.dumps(self.payload if payload is None else payload),
            encoding="utf-8",
        )

    def test_model_allows_internal_dots_only(self):
        self.assertEqual(_model("gemini-3.5-flash-lite"), "gemini-3.5-flash-lite")
        for value in ("", ".model", "model.", "model..x", "../model",
                      "model/x", "model\\x", "model x", "model\nx", 3):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _model(value)
        with self.assertRaises(ValueError):
            _label("scope.with.dot", "credential_scope")

    def test_external_manifest_uses_executing_release_root_for_plan(self):
        self.write()
        with patch(
            "ActiveLearning.batch_runner_v2.authorization.verify_published_source_closure"
        ) as published:
            manifest = load_manifest(self.manifest_path)
            result = plan_batch(self.manifest_path)
        self.assertEqual(manifest["repository_root"], str(ROOT))
        self.assertEqual(result["status"], "READY_FOR_AUTHORIZATION")
        self.assertEqual(published.call_count, 2)
        self.assertEqual(
            published.call_args.args[0]["source_revision"],
            self.payload["source_revision"],
        )

    def test_external_root_and_revision_must_match_executing_release(self):
        for field, value in (
            ("repository_root", str(Path(self.temp.name))),
            ("repository_root", str(ROOT.parent / ".." / ROOT.name)),
            ("source_revision", "0" * 40),
        ):
            with self.subTest(field=field, value=value):
                payload = copy.deepcopy(self.payload)
                payload[field] = value
                self.write(payload)
                with self.assertRaises(ValueError):
                    load_manifest(self.manifest_path)

    def test_external_manifest_rejects_unpublished_closure_bytes(self):
        self.write()
        with patch(
            "ActiveLearning.batch_runner_v2.authorization.verify_published_source_closure",
            side_effect=AuthorizationError("published bytes differ"),
        ), self.assertRaisesRegex(AuthorizationError, "published bytes differ"):
            load_manifest(self.manifest_path)

    def test_closure_and_job_assets_still_fail_closed(self):
        cases = (
            ("closure", "source_closure", "ActiveLearning/main.py"),
            ("hash", "jobs", "d0_sha256"),
            ("escape", "jobs", "config_path"),
            ("missing", "jobs", "config_path"),
        )
        for label, section, key in cases:
            with self.subTest(label=label):
                payload = copy.deepcopy(self.payload)
                if section == "source_closure":
                    payload[section][key] = "0" * 64
                elif label == "hash":
                    payload["jobs"][0][key] = "0" * 64
                elif label == "escape":
                    payload["jobs"][0][key] = "../outside.yaml"
                else:
                    payload["jobs"][0][key] += ".missing"
                self.write(payload)
                with patch(
                    "ActiveLearning.batch_runner_v2.authorization.verify_published_source_closure"
                ), self.assertRaises((ValueError, OSError)):
                    load_manifest(self.manifest_path)

    def test_production_scheduler_uses_same_loader_before_authorization(self):
        self.write()
        output = Path(self.temp.name) / "output"
        with patch(
            "ActiveLearning.batch_runner_v2.authorization.verify_published_source_closure"
        ), self.assertRaises(AuthorizationError):
            run_authorized_batch(
                self.manifest_path, output,
                Path(self.temp.name) / "nonexistent-approval.json", "0" * 64,
            )
        self.assertFalse(output.exists())
        self.assertFalse(list(Path(self.temp.name).glob("*.claim.json")))


if __name__ == "__main__":
    unittest.main()
