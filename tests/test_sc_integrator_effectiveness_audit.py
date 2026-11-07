"""Synthetic-only tests; never touches Ubuntu production results."""
import csv
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "ActiveLearning/diagnostics/sc_integrator_effectiveness_audit_v1.py"
SPEC = importlib.util.spec_from_file_location("sc_integrator_effectiveness_audit", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def save_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class EffectivenessAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.prep = self.base / "preparation"
        self.batch = self.base / "batch"
        self.output = self.base / "output"
        for path in (self.repo, self.prep, self.batch):
            path.mkdir()
        self.batch_id = "MTV2_SCI_PREP_001"
        self.circuit = "circuit_0611"
        self.source_path = f"dataset/{self.circuit}/topology/{self.circuit}.py"
        self.ids = [f"circuit_{611 + i:04d}" for i in range(10)]
        self.metrics = audit.METRICS
        self.specs = {
            name: {"operator": ">=" if i < 2 else "<=", "value": 2.0 if i < 2 else 1.0}
            for i, name in enumerate(self.metrics)
        }
        self.template = {"performance_template": {
            "active_metrics": list(self.metrics),
            "metrics": {name: {"direction": "maximize" if i < 2 else "minimize"}
                        for i, name in enumerate(self.metrics)}}}
        template_path = self.repo / "ActiveLearning/circuit_types/SCIntegrator/performance_template.yaml"
        template_path.parent.mkdir(parents=True)
        template_path.write_text(yaml.safe_dump(self.template), encoding="utf-8")
        master_path = self.repo / "ActiveLearning/candidate_manifests/sc_integrator_v2/master_manifest.json"
        full_ids = self.ids + [f"circuit_{10000 + i}" for i in range(801)]
        save_json(master_path, {"candidate_count": 811,
                                "candidates": [{"circuit_id": cid} for cid in full_ids]})
        manifest_path = master_path.parent / "batches" / f"{self.batch_id}.json"
        save_json(manifest_path, {"schema_version": "sc_integrator_preparation_candidate_batch_v1",
                                 "batch_id": self.batch_id, "circuit_type": "SCIntegrator",
                                 "status": "CANDIDATE_IDS_ONLY_NOT_PREPARATION_READY",
                                 "master_sha256": audit.sha(master_path),
                                 "circuit_ids": self.ids,
                                 "candidates": [{"circuit_id": cid} for cid in self.ids]})
        self.manifest_hash = audit.sha(manifest_path)
        self.prep_dir = self.prep / self.batch_id / self.circuit
        self.config_path = self.prep_dir / "auto_target/run1/active_learning_config.yaml"
        self.profile_path = self.prep_dir / "auto_target/run1/profiling.json"
        self.target_path = self.prep_dir / "auto_target/run1/target_generation.json"
        self.d0_path = self.prep_dir / "auto_target/run1/d0/d0_v4_seed42.jsonl"
        self.config = {"circuit_info": {"circuit_type": "SCIntegrator", "circuit_id": self.circuit},
                       "target_source": "auto_generated", "target_specs": self.specs,
                       "target_provenance": {"profiling_backend": "spice",
                                             "joint_supporting_sample_ids": ["profile_witness"]},
                       "data": {"d0_mode": "independent", "seed": 42, "n_samples": 20,
                                "sampling_mode": "v4"},
                       "active_learning": {"max_iterations": 5, "stop_on_meets_spec": False}}
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        self.profile_rows = [
            self.profile_record("profile_witness", [2.1, 2.2, .8, .9, .7], 1),
            self.profile_record("profile_other", [1.2, 1.1, 1.3, 1.4, 1.2], 2),
        ]
        self.write_profile()
        save_json(self.target_path, {"status": "TARGET_VALIDATED",
                                     "validation": {"target_specs": self.specs,
                                                    "joint_supporting_sample_ids": ["profile_witness"]}})
        self.d0 = [self.real_record(f"d0_v4_{i:03d}",
                                    [1.5, 1.5, 1.5, 1.5, 1.5], i + 10)
                   for i in range(20)]
        save_jsonl(self.d0_path, self.d0)
        self.d0_meta_path = self.d0_path.with_name(self.d0_path.stem + "_initialization.json")
        save_json(self.d0_meta_path, {"d0_mode": "independent", "seed": 42,
                                     "records": 20,
                                     "sampling": {"sampling_mode": "v4", "seed": 42,
                                                  "n_samples": 20, "n_local": 10, "n_global": 9}})
        self.write_receipt()
        self.write_al()

    def profile_record(self, sample_id, values, parameter):
        return {"sample_id": sample_id, "params": {"C0": parameter},
                "parameter_valid": True, "simulation_valid": True,
                "functional_valid": True,
                **dict(zip(self.metrics, values))}

    def real_record(self, sample_id, values, parameter, iteration=None, functional=True):
        row = {"sample_id": sample_id, "params": {"C0": parameter},
               "sampling_source": ("anchor" if parameter == 10 else
                                   "local" if 11 <= parameter <= 20 else
                                   "global" if 21 <= parameter <= 29 else "active_learning_query"),
               "metrics": dict(zip(self.metrics, values)),
               "status": {"parameter_valid": True, "simulation_valid": True,
                          "measurement_valid": True,
                          "functional_valid": functional, "meets_spec": False}}
        if iteration is not None:
            row["iteration"] = iteration
        return row

    def write_profile(self):
        save_json(self.profile_path, {"circuit_id": self.circuit, "evaluation_backend": "spice",
                                      "total_samples": len(self.profile_rows),
                                      "records": self.profile_rows,
                                      "valid_candidate_metrics": [
                                          {"sample_id": row["sample_id"],
                                           "metrics": {m: row[m] for m in self.metrics}}
                                          for row in self.profile_rows if row["functional_valid"]]})

    def write_receipt(self):
        for stage, count, limit in (("profile", 1, 40), ("d0", 20, 20)):
            physical = self.prep_dir / f"physical_{stage}"
            extra = {"candidate_manifest_sha256": self.manifest_hash}
            if stage == "d0":
                extra["config_sha256"] = audit.sha(self.config_path)
            identity = {"schema": "sc_integrator_bulk_native_v1",
                        "stage": stage, "circuit_id": self.circuit,
                        "limit": limit, "extra": extra}
            save_json(physical / "identity.json", identity)
            events = []
            for index in range(1, count + 1):
                events.extend(({"event": "reserved", "attempt": index,
                                "vector_sha256": f"vector{index}",
                                "identity_sha256": audit.canonical_sha(identity)},
                               {"event": "finished", "attempt": index,
                                "vector_sha256": f"vector{index}", "outcome": "complete"}))
            save_jsonl(physical / "physical_attempt_ledger.jsonl", events)
        artifacts = {}
        physical_paths = [self.prep_dir / f"physical_{stage}" / name
                          for stage in ("profile", "d0")
                          for name in ("identity.json", "physical_attempt_ledger.jsonl")]
        for path in (self.config_path, self.profile_path, self.target_path,
                     self.d0_path, self.d0_meta_path, *physical_paths):
            artifacts[path.relative_to(self.prep_dir).as_posix()] = audit.sha(path)
        self.receipt = {"status": "PREPARATION_READY", "batch_id": self.batch_id,
                        "circuit_id": self.circuit,
                        "source_sha256": {
                            self.source_path: "synthetic-source",
                            f"dataset/{self.circuit}/topology/{self.circuit}.cir":
                                "synthetic-netlist-source"},
                        "candidate_manifest_sha256": self.manifest_hash,
                        "target_spec_sha256": audit.canonical_sha(self.specs),
                        "config_path": self.config_path.relative_to(self.prep_dir).as_posix(),
                        "profile_path": self.profile_path.relative_to(self.prep_dir).as_posix(),
                        "d0_path": self.d0_path.relative_to(self.prep_dir).as_posix(),
                        "artifacts": artifacts}
        save_json(self.prep_dir / "result.json", self.receipt)
        save_json(self.prep / self.batch_id / "summary.json",
                  {"batch_id": self.batch_id, "status": "PREPARATION_COMPLETE",
                   "circuits": {self.circuit: self.receipt}})

    def write_al(self):
        batch_dir = self.batch / self.batch_id
        attempt_id, run_id = "abc123", "run123"
        attempt = batch_dir / "jobs" / self.circuit / "attempts" / attempt_id
        run_dir = attempt / "al_runs/SCIntegrator" / self.circuit / run_id
        al_row = self.real_record("iter_001_candidate_a", [1.9, 1.9, 1.1, 1.1, 1.1], 99, 1)
        save_jsonl(run_dir / "active_learning_records.jsonl", self.d0 + [al_row])
        save_jsonl(run_dir / "iteration_log.jsonl",
                   [{"run_id": run_id, "iteration": 1, "selected_candidate_id": "candidate_a"}])
        save_json(run_dir / "summary.json",
                  {"run_id": run_id, "circuit_id": self.circuit,
                   "circuit_type": "SCIntegrator", "iterations_completed": 1})
        save_json(run_dir / "config_snapshot.json", self.config)
        save_json(attempt / "al_run_context.json",
                  {"run_id": run_id, "run_directory": str(run_dir)})
        result_path = attempt / "result.json"
        summary_path = run_dir / "summary.json"
        save_json(result_path, {"run_id": run_id, "iterations_completed": 1,
                                "result_path": str(summary_path),
                                "al_result_sha256": audit.sha(summary_path)})
        manifest = {"batch_id": self.batch_id,
                    "candidate_manifest_sha256": self.manifest_hash,
                    "jobs": [{"job_id": self.circuit, "circuit_id": self.circuit,
                              "config_path": str(self.config_path), "d0_path": str(self.d0_path),
                              "source_path": self.source_path,
                              "source_sha256": "synthetic-source",
                              "config_sha256": audit.sha(self.config_path),
                              "d0_sha256": audit.sha(self.d0_path),
                              "target_spec_sha256": audit.canonical_sha(self.specs)}]}
        manifest["manifest_sha256"] = audit.canonical_sha({"runtime": manifest})
        save_json(batch_dir / "manifest.json", manifest)
        save_json(batch_dir / "status.json",
                  {"manifest_sha256": manifest["manifest_sha256"],
                   "jobs": {self.circuit: {"state": "COMPLETED",
                                           "attempts": [{"attempt_id": attempt_id,
                                                         "result_sha256": audit.sha(result_path)}]}}})
        identity = {"schema": "sc_integrator_bulk_native_v1",
                    "stage": "al", "circuit_id": self.circuit, "limit": 5,
                    "extra": {"batch_id": self.batch_id,
                              "config_sha256": audit.sha(self.config_path),
                              "d0_sha256": audit.sha(self.d0_path)}}
        save_json(batch_dir / "native" / self.circuit / "identity.json", identity)
        save_jsonl(batch_dir / "native" / self.circuit / "physical_attempt_ledger.jsonl",
                   [{"event": "reserved", "attempt": 1, "vector_sha256": "vector",
                     "identity_sha256": audit.canonical_sha(identity)},
                    {"event": "finished", "attempt": 1, "vector_sha256": "vector",
                     "outcome": "complete"}])

    def rewrite_al_job(self, change):
        batch_dir = self.batch / self.batch_id
        manifest_path = batch_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        change(manifest["jobs"][0])
        manifest["manifest_sha256"] = audit.canonical_sha({
            "runtime": {key: value for key, value in manifest.items()
                        if key != "manifest_sha256"}})
        save_json(manifest_path, manifest)
        status_path = batch_dir / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["manifest_sha256"] = manifest["manifest_sha256"]
        save_json(status_path, status)

    def execute(self):
        return audit.run(self.repo, self.prep, self.batch, 1, 1, self.output)

    def result(self):
        with (self.output / "sc_integrator_effectiveness.csv").open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))[0]

    def test_end_to_end_witness_and_real_al_improvement(self):
        report = self.execute()
        row = self.result()
        self.assertEqual(report["selected_circuit_count"], 10)
        self.assertEqual(report["complete_evidence_count"], 1)
        self.assertEqual(row["witness_ids"], '["profile_witness"]')
        self.assertEqual(row["d0_best_id"], "d0_v4_000")
        self.assertEqual(row["al_best_id"], "iter_001_candidate_a")
        self.assertEqual(row["al_improved_d0"], "True")
        self.assertEqual(row["d0_joint_success_count"], "0")
        self.assertEqual(row["al_joint_success_count"], "0")
        self.assertEqual(len(list(self.output.iterdir())), 4)

    def test_source_map_to_scalar_binding_succeeds(self):
        self.assertEqual(self.receipt["source_sha256"][self.source_path],
                         "synthetic-source")
        report = self.execute()
        self.assertEqual(report["circuits"][self.circuit]["status"], "OK")

    def test_source_path_missing_fails_closed(self):
        self.rewrite_al_job(lambda job: job.pop("source_path"))
        report = self.execute()
        self.assertEqual(report["circuits"][self.circuit]["status"], "INTEGRITY_UNKNOWN")
        self.assertIn("Batch job not bound to Preparation",
                      report["circuits"][self.circuit]["error"])

    def test_source_path_not_in_preparation_map_fails_closed(self):
        self.rewrite_al_job(lambda job: job.update({"source_path": "dataset/other.py"}))
        report = self.execute()
        self.assertEqual(report["circuits"][self.circuit]["status"], "INTEGRITY_UNKNOWN")
        self.assertIn("Batch job not bound to Preparation",
                      report["circuits"][self.circuit]["error"])

    def test_source_hash_mismatch_fails_closed(self):
        self.rewrite_al_job(lambda job: job.update({"source_sha256": "wrong"}))
        report = self.execute()
        self.assertEqual(report["circuits"][self.circuit]["status"], "INTEGRITY_UNKNOWN")
        self.assertIn("Batch job not bound to Preparation",
                      report["circuits"][self.circuit]["error"])

    def test_invalid_source_binding_types_fail_closed(self):
        cases = (("path_list", {"source_path": [self.source_path]}, None),
                 ("hash_list", {"source_sha256": ["synthetic-source"]}, None),
                 ("receipt_list", {}, ["synthetic-source"]),
                 ("map_value_number", {}, {self.source_path: 123}))
        for label, job_fields, receipt_hashes in cases:
            with self.subTest(label=label):
                self.write_al()
                self.write_receipt()
                if job_fields:
                    self.rewrite_al_job(lambda job: job.update(job_fields))
                if receipt_hashes is not None:
                    self.receipt["source_sha256"] = receipt_hashes
                    save_json(self.prep_dir / "result.json", self.receipt)
                    save_json(self.prep / self.batch_id / "summary.json", {
                        "batch_id": self.batch_id, "status": "PREPARATION_COMPLETE",
                        "circuits": {self.circuit: self.receipt}})
                report = audit.run(self.repo, self.prep, self.batch, 1, 1,
                                   self.base / f"output_{label}")
                self.assertEqual(report["circuits"][self.circuit]["status"],
                                 "INTEGRITY_UNKNOWN")
                self.assertIn("Batch job not bound to Preparation",
                              report["circuits"][self.circuit]["error"])

    def test_no_metric_stitching_and_invalid_filter(self):
        rows = [audit.as_sample(self.real_record("a", [3, 1, .5, .5, .5], 1),
                                "D0", self.batch_id, self.circuit, self.d0_path),
                audit.as_sample(self.real_record("b", [1, 3, .5, .5, .5], 2),
                                "D0", self.batch_id, self.circuit, self.d0_path),
                audit.as_sample(self.real_record("invalid", [3, 3, 0, 0, 0], 3,
                                                 functional=False),
                                "AL", self.batch_id, self.circuit, self.d0_path)]
        target = audit.target_contract(self.config, self.template)
        scales = {m: 1.0 for m in self.metrics}
        audit.annotate(rows, target, scales)
        self.assertEqual([r["computed_meets_spec"] for r in rows], [False, False, False])
        self.assertFalse(rows[2]["eligible"])
        self.assertNotEqual(audit.best(rows)["sample_id"], "invalid")

    def test_witness_conflict_fails_closed(self):
        self.profile_rows[0]["functional_valid"] = False
        self.write_profile()
        self.write_receipt()
        report = self.execute()
        self.assertEqual(report["complete_evidence_count"], 0)
        self.assertIn("Witness", report["circuits"][self.circuit]["error"])

    def test_hash_conflict_and_output_isolation(self):
        self.profile_path.write_text(self.profile_path.read_text() + " ", encoding="utf-8")
        report = self.execute()
        self.assertEqual(report["circuits"][self.circuit]["status"], "INTEGRITY_UNKNOWN")
        self.assertIn("hash conflict", report["circuits"][self.circuit]["error"])
        with self.assertRaises(audit.EvidenceError):
            audit.run(self.repo, self.prep, self.batch, 1, 1, self.prep / "bad_output")

    def test_missing_and_duplicate_data_fail_closed(self):
        self.d0_path.unlink()
        report = self.execute()
        self.assertEqual(report["circuits"][self.circuit]["status"], "INTEGRITY_UNKNOWN")
        self.assertEqual(report["complete_evidence_count"], 0)

    def test_zero_scale_refused(self):
        target = audit.target_contract(self.config, self.template)
        target[self.metrics[0]]["value"] = 0
        profile = [audit.as_sample(self.profile_record("p", [0, 1, 1, 1, 1], 1),
                                   "PROFILE", self.batch_id, self.circuit, self.profile_path)]
        with self.assertRaises(audit.EvidenceError):
            audit.scales_from_profile(profile, target)

    def test_target_generation_conflict_fails_closed(self):
        target = json.loads(self.target_path.read_text(encoding="utf-8"))
        target["validation"]["target_specs"][self.metrics[0]]["value"] = 9.0
        save_json(self.target_path, target)
        self.write_receipt()
        report = self.execute()
        self.assertEqual(report["circuits"][self.circuit]["status"], "INTEGRITY_UNKNOWN")
        self.assertIn("Target", report["circuits"][self.circuit]["error"])

    def test_duplicate_sample_and_malformed_jsonl_fail_closed(self):
        self.d0[1]["sample_id"] = self.d0[0]["sample_id"]
        save_jsonl(self.d0_path, self.d0)
        self.write_receipt()
        report = self.execute()
        self.assertIn("duplicate sample ID", report["circuits"][self.circuit]["error"])

    def test_truncated_jsonl_fails_closed(self):
        self.d0_path.write_text(self.d0_path.read_text(encoding="utf-8") + "{", encoding="utf-8")
        self.write_receipt()
        report = self.execute()
        self.assertEqual(report["complete_evidence_count"], 0)
        self.assertIn("JSONL", report["circuits"][self.circuit]["error"])

    def test_directions_and_tie_break(self):
        target = audit.target_contract(self.config, self.template)
        self.assertEqual([target[m]["operator"] for m in self.metrics],
                         [">=", ">=", "<=", "<=", "<="])
        rows = [audit.as_sample(self.real_record("b", [1.5, 2, 1, 1, 1], 1),
                                "D0", self.batch_id, self.circuit, self.d0_path),
                audit.as_sample(self.real_record("a", [2, 1.5, 1, 1, 1], 2),
                                "D0", self.batch_id, self.circuit, self.d0_path)]
        audit.annotate(rows, target, {m: 1 for m in self.metrics})
        self.assertEqual(audit.best(rows)["sample_id"], "a")

    def test_invalid_success_status_rejected(self):
        self.d0[0]["status"]["functional_valid"] = False
        self.d0[0]["status"]["meets_spec"] = True
        save_jsonl(self.d0_path, self.d0)
        self.write_receipt()
        # The saved AL history still contains the old D0, so even before the
        # contradictory success flag is reached its identity conflict closes.
        report = self.execute()
        self.assertEqual(report["circuits"][self.circuit]["status"], "INTEGRITY_UNKNOWN")

    def test_unknown_validity_retained_but_not_compared(self):
        raw = self.real_record("failed", [None] * 5, 123, functional=False)
        raw["status"]["simulation_valid"] = None
        raw["status"]["functional_valid"] = None
        row = audit.as_sample(raw, "D0", self.batch_id, self.circuit, self.d0_path)
        self.assertFalse(row["eligible"])
        self.assertIn("simulation_valid", row["validity_unknown_fields_json"])

    def test_nonfinite_invalid_profile_sample_is_retained(self):
        bad = self.profile_record("profile_bad", [math.nan, 3, 0, 0, 0], 333)
        bad["functional_valid"] = False
        self.profile_rows.append(bad)
        self.write_profile()
        self.write_receipt()
        report = self.execute()
        self.assertEqual(report["complete_evidence_count"], 1)
        with (self.output / "sc_integrator_samples.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        row = next(r for r in rows if r["sample_id"] == "profile_bad")
        self.assertEqual(row["eligible"], "False")
        self.assertIn(self.metrics[0], row["metric_missing_or_nonfinite_fields_json"])


if __name__ == "__main__":
    unittest.main()
