"""Offline SCIntegrator runtime admission tests; no physical execution."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ActiveLearning.batch_runner_v2.schema import canonical_sha256, verify_job
from ActiveLearning.batch_runner_v2.storage import sha256_file
from ActiveLearning.batch_runner_v2 import sc_integrator_production
from ActiveLearning.onboarding import sc_integrator_bulk_core
from ActiveLearning.onboarding import sc_integrator_bulk_native
from ActiveLearning.onboarding.errors import AutoConfigError


class SCIntegratorSchemaStopPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        preparation_root = root / "preparation"
        circuit_id = "circuit_2519"
        preparation = preparation_root / circuit_id
        preparation.mkdir(parents=True)
        source = root / "dataset" / circuit_id / "topology" / f"{circuit_id}.py"
        source.parent.mkdir(parents=True)
        source.write_text("# synthetic topology identity\n", encoding="utf-8")
        closure = root / "ActiveLearning" / "batch_runner_v2" / "schema.py"
        closure.parent.mkdir(parents=True)
        closure.write_text("# synthetic closure identity\n", encoding="utf-8")
        self.config_path = preparation / "config.json"
        d0_path = preparation / "d0.jsonl"
        d0_path.write_text('{"sample_id":"d0_001"}\n', encoding="utf-8")
        self.config = {
            "target_specs": {"integration_step_magnitude_v": {
                "operator": ">=", "value": 0.1}},
            "active_learning": {"max_iterations": 5, "stop_on_meets_spec": True},
            "data": {"d0_mode": "independent", "seed": 42, "n_samples": 20},
        }
        self.job = {
            "job_id": circuit_id, "circuit_id": circuit_id,
            "circuit_type": "SCIntegrator", "max_iterations": 5, "seed": 42,
            "execution_mode": "al_only", "sc_integrator_execution_id":
                f"native/{circuit_id}",
            "config_path": str(self.config_path), "d0_path": str(d0_path),
            "d0_sha256": sha256_file(d0_path),
            "source_path": source.relative_to(root).as_posix(),
            "source_sha256": sha256_file(source),
            "target_spec_sha256": canonical_sha256(self.config["target_specs"]),
        }
        self.manifest = {
            "schema_version": "sc_integrator_bulk_runtime_v1",
            "batch_id": "MTV2_SCI_PREP_011",
            "repository_root": str(root),
            "preparation_root": str(preparation_root),
            "candidate_manifest_sha256": "a" * 64,
            "source_closure": {closure.relative_to(root).as_posix(): sha256_file(closure)},
        }
        self.spec = {"preparation_root": str(preparation_root),
                     "candidate_manifest_sha256": "a" * 64,
                     "circuit_ids": [circuit_id]}
        self.receipt = {"status": "PREPARATION_READY",
                        "config_path": self.config_path.name,
                        "d0_path": d0_path.name}
        self._save_config()

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.job["config_sha256"] = sha256_file(self.config_path)

    def _verify(self):
        with (patch.object(sc_integrator_production, "inspect_manifest",
                           return_value=self.spec),
              patch.object(sc_integrator_production, "_receipt",
                           return_value=self.receipt)):
            verify_job(self.manifest, self.job)

    def test_explicit_true_and_legacy_false_pass(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.config["active_learning"]["stop_on_meets_spec"] = value
                self._save_config()
                self._verify()

    def test_missing_null_string_and_number_are_rejected(self):
        for value in (None, "true", "false", 0, 1, 0.0, 1.0):
            with self.subTest(value=value):
                self.config["active_learning"]["stop_on_meets_spec"] = value
                self._save_config()
                with self.assertRaisesRegex(ValueError, "config/D0/source hash mismatch"):
                    self._verify()
        del self.config["active_learning"]["stop_on_meets_spec"]
        self._save_config()
        with self.assertRaisesRegex(ValueError, "config/D0/source hash mismatch"):
            self._verify()

    def test_existing_identity_hash_guards_still_reject_mismatch(self):
        for field in ("config_sha256", "d0_sha256", "source_sha256",
                      "target_spec_sha256"):
            with self.subTest(field=field):
                original = self.job[field]
                self.job[field] = "0" * 64
                try:
                    with self.assertRaisesRegex(ValueError, "config/D0/source hash mismatch"):
                        self._verify()
                finally:
                    self.job[field] = original
        name = next(iter(self.manifest["source_closure"]))
        self.manifest["source_closure"][name] = "0" * 64
        with self.assertRaisesRegex(ValueError, "asset hash mismatch"):
            self._verify()


class SCIntegratorALGuardStopPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.circuit_id = "circuit_2519"
        self.batch_id = "MTV2_SCI_PREP_011"
        self.preparation = self.root / "preparation" / self.circuit_id
        self.preparation.mkdir(parents=True)
        self.config_path = self.preparation / "config.json"
        self.d0_path = self.preparation / "d0.jsonl"
        self.d0_path.write_text('{"sample_id":"d0_001"}\n', encoding="utf-8")
        self.output = self.root / "batch_output"
        self.native_root = self.output / self.batch_id / "native" / self.circuit_id
        self.plan = {"fixture_sha256": "b" * 64, "stimulus_sha256": "c" * 64}
        self.spec = {"preparation_root": str(self.preparation.parent),
                     "circuit_ids": [self.circuit_id],
                     "candidate_manifest_sha256": "a" * 64}
        self.config = {
            "target_specs": {"integration_step_magnitude_v": {
                "operator": ">=", "value": 0.1}},
            "active_learning": {"max_iterations": 5, "stop_on_meets_spec": True,
                                "prediction_prompt_version": "compact_v2"},
            "data": {"d0_mode": "independent", "seed": 42, "n_samples": 20,
                     "sampling_mode": "v4"},
            "sc_integrator_bulk": {
                "batch_id": self.batch_id,
                "candidate_manifest_sha256": self.spec["candidate_manifest_sha256"],
                "receipt_path": str(self.preparation / "result.json"),
                "al_native_root": str(self.native_root),
                "fixture_sha256": self.plan["fixture_sha256"],
                "stimulus_sha256": self.plan["stimulus_sha256"],
            },
        }
        self.receipt = {"status": "PREPARATION_READY",
                        "config_path": self.config_path.name,
                        "d0_path": self.d0_path.name,
                        "target_spec_sha256": canonical_sha256(self.config["target_specs"])}
        self._save_config()

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.receipt["artifacts"] = {
            self.config_path.name: sha256_file(self.config_path),
            self.d0_path.name: sha256_file(self.d0_path),
        }

    def _validate(self):
        executor = SimpleNamespace(verify_existing=lambda: None)
        with (patch.object(sc_integrator_production, "inspect_manifest",
                           return_value=self.spec),
              patch.object(sc_integrator_production, "_receipt",
                           return_value=self.receipt),
              patch.object(sc_integrator_production, "OUTPUT_ROOT", self.output),
              patch.object(sc_integrator_bulk_core, "derive_plan",
                           return_value=self.plan),
              patch.object(sc_integrator_bulk_native, "SCIntegratorPhysicalExecutor",
                           return_value=executor) as constructor):
            result = sc_integrator_bulk_core.validate_bulk_al(
                SimpleNamespace(circuit_id=self.circuit_id), self.config,
                self.config_path, self.d0_path)
        return result, constructor

    def test_new_true_and_legacy_false_reach_isolated_executor(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.config["active_learning"]["stop_on_meets_spec"] = value
                self._save_config()
                executor, constructor = self._validate()
                self.assertIsNotNone(executor)
                self.assertEqual(constructor.call_args.kwargs["limit"], 5)

    def test_missing_and_non_boolean_policy_fail_closed(self):
        for value in (None, "true", 0, 1, 0.0):
            with self.subTest(value=value):
                self.config["active_learning"]["stop_on_meets_spec"] = value
                self._save_config()
                with self.assertRaisesRegex(AutoConfigError, "iteration/D0 policy"):
                    self._validate()
        del self.config["active_learning"]["stop_on_meets_spec"]
        self._save_config()
        with self.assertRaisesRegex(AutoConfigError, "iteration/D0 policy"):
            self._validate()

    def test_target_config_d0_and_fixture_identity_still_fail_closed(self):
        for key in (self.config_path.name, self.d0_path.name):
            with self.subTest(artifact=key):
                original = self.receipt["artifacts"][key]
                self.receipt["artifacts"][key] = "0" * 64
                try:
                    with self.assertRaisesRegex(AutoConfigError, "source/target/D0 identity"):
                        self._validate()
                finally:
                    self.receipt["artifacts"][key] = original
        original = self.receipt["target_spec_sha256"]
        self.receipt["target_spec_sha256"] = "0" * 64
        with self.assertRaisesRegex(AutoConfigError, "source/target/D0 identity"):
            self._validate()
        self.receipt["target_spec_sha256"] = original
        self.plan["fixture_sha256"] = "0" * 64
        with self.assertRaisesRegex(AutoConfigError, "source/target/D0 identity"):
            self._validate()


    def test_real_receipt_guard_accepts_only_explicit_boolean(self):
        self.spec["batch_id"] = self.batch_id
        self.plan["source_sha256"] = "d" * 64
        self.config.update({
            "circuit_info": {"circuit_type": "SCIntegrator",
                             "circuit_id": self.circuit_id},
            "target_source": "auto_generated",
            "target_provenance": {"profiling_backend": "spice"},
        })
        profile_path = self.preparation / "profile.json"
        profile_path.write_text(json.dumps({"evaluation_backend": "spice",
                                            "anchor": {"functional_valid": True}}),
                                encoding="utf-8")
        (self.preparation / "target_generation.json").write_text(
            '{"status":"TARGET_VALIDATED"}', encoding="utf-8")
        self.d0_path.write_text("".join(json.dumps({
            "sample_id": f"d0_{index:03d}",
            "status": {"functional_valid": index == 0}}) + "\n"
            for index in range(20)), encoding="utf-8")
        code = {
            "executor_sha256": "ActiveLearning/onboarding/sc_integrator_bulk_native.py",
            "ledger_helpers_sha256": "ActiveLearning/onboarding/sample_hold_bulk_native.py",
            "model_binding_sha256": "ActiveLearning/onboarding/sample_hold_bulk_core.py",
            "parameter_builder_sha256": "ActiveLearning/onboarding/sample_hold.py",
            "posix_sha256": "ActiveLearning/sample_hold_posix.py",
            "ngspice_config_sha256": "run/single_stage_amplifier/run_ideal_dataset_simulations.py",
            "child_sha256": "ActiveLearning/onboarding/sc_integrator_bulk_child.py",
            "binding_sha256": "ActiveLearning/onboarding/sc_integrator_bulk_core.py",
            "measurement_sha256": "ActiveLearning/onboarding/sc_integrator_bulk_core.py",
        }
        for stage, limit in (("profile", 40), ("d0", 20)):
            native = self.preparation / f"physical_{stage}"
            native.mkdir()
            identity = {
                "schema": "sc_integrator_bulk_native_v1",
                "circuit_id": self.circuit_id, "stage": stage, "limit": limit,
                "source_sha256": self.plan["source_sha256"],
                "fixture_sha256": self.plan["fixture_sha256"],
                "stimulus_sha256": self.plan["stimulus_sha256"],
                "extra": {"candidate_manifest_sha256":
                          self.spec["candidate_manifest_sha256"]},
                **{key: sha256_file(sc_integrator_production.ROOT / path)
                   for key, path in code.items()},
            }
            (native / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
            (native / "physical_attempt_ledger.jsonl").write_text(
                '{"event":"reserved"}\n', encoding="utf-8")
        receipt = {
            "status": "PREPARATION_READY", "batch_id": self.batch_id,
            "circuit_id": self.circuit_id,
            "candidate_manifest_sha256": self.spec["candidate_manifest_sha256"],
            "source_sha256": self.plan["source_sha256"],
            "fixture_sha256": self.plan["fixture_sha256"],
            "stimulus_sha256": self.plan["stimulus_sha256"],
            "target_spec_sha256": canonical_sha256(self.config["target_specs"]),
            "config_path": self.config_path.name, "d0_path": self.d0_path.name,
            "profile_path": profile_path.name,
            "physical_attempts": {"profile": 1, "d0": 1},
        }
        for value in (True, False, None, "true", 1, "missing"):
            with self.subTest(value=value):
                if value == "missing":
                    self.config["active_learning"].pop("stop_on_meets_spec", None)
                else:
                    self.config["active_learning"]["stop_on_meets_spec"] = value
                self._save_config()
                receipt["artifacts"] = dict(self.receipt["artifacts"])
                (self.preparation / "result.json").write_text(
                    json.dumps(receipt), encoding="utf-8")
                d0_identity = self.preparation / "physical_d0" / "identity.json"
                identity = json.loads(d0_identity.read_text(encoding="utf-8"))
                identity["extra"]["config_sha256"] = sha256_file(self.config_path)
                d0_identity.write_text(json.dumps(identity), encoding="utf-8")
                with patch.object(sc_integrator_bulk_core, "derive_plan",
                                  return_value=self.plan):
                    if type(value) is bool:
                        self.assertEqual(sc_integrator_production._receipt(
                            self.spec, self.circuit_id)["status"], "PREPARATION_READY")
                    else:
                        with self.assertRaisesRegex(ValueError, "target/config/profile invalid"):
                            sc_integrator_production._receipt(self.spec, self.circuit_id)


if __name__ == "__main__":
    unittest.main()
