"""No PySpice import or physical work: numerical failure and D0 isolation contracts."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import typing
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
METRICS = ("integration_step_magnitude_v", "zero_input_drift_v_per_cycle",
           "step_consistency_v", "virtual_ground_error_v", "output_headroom_v")


def selected(path: Path, names: set[str], scope: dict) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in names]
    assert {node.name for node in nodes} == names
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, str(path), "exec"), scope)
    return scope


class FakeCommandError(Exception):
    pass


class SCIntegratorD0IsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.child = selected(
            ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_child.py",
            {"_run_transient", "_failed_transient_evaluation"}, {})

    def _case(self, *, baseline_fails=False, perturbed_fails=False):
        events = []

        class Simulator:
            def __init__(self, name, fails):
                self.name, self.fails = name, fails

            def transient(self, **kwargs):
                events.append((self.name, kwargs))
                if self.fails:
                    raise FakeCommandError("Command 'run' failed")
                return {"case": self.name}

        result = None
        for index, (name, fails) in enumerate(
                (("baseline", baseline_fails), ("perturbed", perturbed_fails)), start=1):
            analysis, failure = self.child["_run_transient"](
                Simulator(name, fails), {"step_time_s": 1e-9, "end_time_s": 1e-6},
                FakeCommandError)
            if failure:
                result = self.child["_failed_transient_evaluation"](
                    name, failure, index, METRICS)
                break
            self.assertEqual(analysis, {"case": name})
        return result, events

    def test_successful_baseline_and_perturbed_run(self):
        result, events = self._case()
        self.assertIsNone(result)
        self.assertEqual([name for name, _ in events], ["baseline", "perturbed"])

    def test_baseline_run_failure_skips_perturbed_and_has_no_metrics(self):
        result, events = self._case(baseline_fails=True)
        self.assertEqual([name for name, _ in events], ["baseline"])
        self.assertFalse(result["simulation_valid"])
        self.assertFalse(result["functional_valid"])
        self.assertEqual(result["diagnostics"]["native_simulator_attempts"], 1)
        self.assertEqual(result["diagnostics"]["native_simulator_returns"], 0)
        self.assertEqual(result["diagnostics"]["spice_exception_type"], "FakeCommandError")
        self.assertEqual(result["diagnostics"]["spice_error"], "Command 'run' failed")
        self.assertTrue(all(value is None for value in result["metrics"].values()))

    def test_perturbed_run_failure_invalidates_whole_candidate(self):
        result, events = self._case(perturbed_fails=True)
        self.assertEqual([name for name, _ in events], ["baseline", "perturbed"])
        self.assertEqual(result["diagnostics"]["native_simulator_attempts"], 2)
        self.assertEqual(result["diagnostics"]["native_simulator_returns"], 1)
        self.assertEqual(result["diagnostics"]["failed_case"], "perturbed")
        self.assertTrue(all(value is None for value in result["metrics"].values()))

    def test_python_and_other_spice_errors_still_raise(self):
        class Simulator:
            def __init__(self, error):
                self.error = error

            def transient(self, **_kwargs):
                raise self.error

        for error in (ValueError("bad fixture"), FakeCommandError("model missing")):
            with self.assertRaises(type(error)):
                self.child["_run_transient"](
                    Simulator(error), {"step_time_s": 1e-9, "end_time_s": 1e-6},
                    FakeCommandError)

    def test_real_validator_keeps_failed_meets_spec_false(self):
        core = selected(ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_core.py",
                        {"SCIntegratorFunctionalValidator"}, {
                            "METRICS": METRICS,
                            "check_meets_spec": lambda *_args: {"meets_spec": True}})
        validator = core["SCIntegratorFunctionalValidator"]()
        failure = self.child["_failed_transient_evaluation"](
            "baseline", {"exception_type": "NgSpiceCommandError",
                          "message": "Command 'run' failed"}, 1, METRICS)
        status = validator.validate({}, failure)["status"]
        self.assertFalse(status["simulation_valid"])
        self.assertFalse(status["functional_valid"])
        self.assertFalse(status["meets_spec"])
        normal = {**failure, "metrics": dict.fromkeys(METRICS, 0.1),
                  "simulation_valid": True, "measurement_valid": True,
                  "functional_valid": True}
        self.assertTrue(validator.validate({}, normal)["status"]["meets_spec"])

    def test_middle_d0_sample_failure_continues_without_retry_or_substitute(self):
        path = ROOT / "ActiveLearning/InitializeD0/d0_builder.py"
        calls = []
        failure = self.child["_failed_transient_evaluation"](
            "perturbed", {"exception_type": "NgSpiceCommandError",
                          "message": "Command 'run' failed"}, 2, METRICS)

        class Evaluator:
            def evaluate(self, params):
                calls.append(params["x"])
                if params["x"] == 2:
                    return failure
                return {"metrics": dict.fromkeys(METRICS, 0.1),
                        "parameter_valid": True, "simulation_valid": True,
                        "measurement_valid": True, "functional_valid": True,
                        "failure_reasons": [], "diagnostics": {}}

        def validate(_params, evaluation, target_specs=None):
            valid = evaluation["simulation_valid"]
            return {"metrics": evaluation["metrics"], "status": {
                "parameter_valid": True, "simulation_valid": valid,
                "functional_valid": valid, "functional_status": "valid" if valid else "invalid",
                "meets_spec": valid}, "failure_reasons": evaluation["failure_reasons"],
                "diagnostics": evaluation["diagnostics"]}

        runtime = SimpleNamespace(param_template={},
                                  create_evaluator=lambda: Evaluator(),
                                  create_functional_validator=lambda: SimpleNamespace(
                                      validate=validate))
        scope = selected(path, {"build_runtime_d0", "_empty_runtime_record",
                                "_apply_structured_validation"}, {
            "Mapping": typing.Mapping, "Sequence": typing.Sequence,
            "validate_parameters": lambda *_args: {"parameter_valid": True},
            "_runtime_metric_names": lambda _runtime: list(METRICS)})
        samples = [{"sample_id": f"s{index}", "sampling_source": "mock",
                    "params": {"x": index}, "normalized_params": {"x": index}}
                   for index in (1, 2, 3)]
        records = scope["build_runtime_d0"](runtime, target_specs={"target": 1},
                                             prepared_samples=samples)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(len(records), 3)
        self.assertEqual([row["status"]["simulation_valid"] for row in records],
                         [True, False, True])
        self.assertFalse(records[1]["status"]["functional_valid"])
        self.assertFalse(records[1]["status"]["meets_spec"])
        self.assertTrue(all(value is None for value in records[1]["metrics"].values()))
        self.assertEqual(records[1]["diagnostics"]["spice_exception_type"],
                         "NgSpiceCommandError")
        persistence = selected(path, {"_ordered_keys", "save_d0_dataset"}, {
            "Path": Path, "json": json, "csv": csv})
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = persistence["save_d0_dataset"](records, temporary, "d0_offline")
            persisted = [json.loads(line) for line in artifacts["jsonl"].read_text(
                encoding="utf-8").splitlines()]
            with artifacts["csv"].open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(persisted), len(rows))
            self.assertEqual(persisted[1]["diagnostics"]["spice_exception_type"],
                             "NgSpiceCommandError")
            self.assertEqual(rows[1]["simulation_valid"], "False")
            self.assertEqual(rows[1]["integration_step_magnitude_v"], "")

    def test_ledger_requires_closed_completed_attempts_and_preserves_budget(self):
        path = ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_native.py"
        scope = selected(path, {"SCIntegratorPhysicalExecutor"}, {
            "json": json, "AutoConfigError": ValueError,
            "_events": lambda ledger: [json.loads(line) for line in ledger.read_text(
                encoding="utf-8").splitlines()] if ledger.exists() else []})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = {"schema": "sc_integrator_bulk_native_v1"}
            (root / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
            ledger = root / "physical_attempt_ledger.jsonl"
            claim = {"event": "reserved", "attempt": 1,
                     "vector_sha256": "v1", "identity_sha256": "id"}
            obj = SimpleNamespace(root=root, identity=identity, ledger=ledger,
                                  limit=1, identity_sha256="id")
            ledger.write_text(json.dumps(claim) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unclosed"):
                scope["SCIntegratorPhysicalExecutor"].verify_existing(obj)
            failed = {"event": "finished", "attempt": 1,
                      "vector_sha256": "v1", "outcome": "failed_reaped"}
            ledger.write_text(json.dumps(claim) + "\n" + json.dumps(failed) + "\n",
                              encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "failed"):
                scope["SCIntegratorPhysicalExecutor"].verify_existing(obj)
            failed["outcome"] = "complete"
            ledger.write_text(json.dumps(claim) + "\n" + json.dumps(failed) + "\n",
                              encoding="utf-8")
            scope["SCIntegratorPhysicalExecutor"].verify_existing(obj)

    def test_invalid_spice_sample_is_cached_and_budgeted_once(self):
        path = ROOT / "ActiveLearning/onboarding/sc_integrator_bulk_native.py"
        failure = self.child["_failed_transient_evaluation"](
            "baseline", {"exception_type": "NgSpiceCommandError",
                          "message": "Command 'run' failed"}, 1, METRICS)
        calls = []

        def digest(data):
            return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

        def file_digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()

        def durable(path, data):
            path.write_text(json.dumps(data), encoding="utf-8")

        def append(path, data):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(data) + "\n")

        def events(path):
            return [json.loads(line) for line in path.read_text(
                encoding="utf-8").splitlines()] if path.exists() else []

        def isolated(command, **_kwargs):
            calls.append(command)
            candidate = Path(command[command.index("--candidate") + 1])
            request = json.loads((candidate / "request.json").read_text(encoding="utf-8"))
            durable(candidate / "native_result.json", {
                "mock": False, "vector_sha256": request["vector_sha256"],
                "identity_sha256": request["identity_sha256"], "evaluation": failure})
            return SimpleNamespace(returncode=0, stdout="", stderr="",
                                   job_evidence={"contained_before_resume": True,
                                                 "active_processes_at_return": 0})

        scope = selected(path, {"SCIntegratorPhysicalExecutor"}, {
            "json": json, "os": os, "sys": sys, "time": time, "uuid": uuid,
            "Path": Path, "ROOT": ROOT, "AutoConfigError": ValueError,
            "canonical_sha256": digest, "sha256_file": file_digest,
            "_durable_json": durable, "_append": append, "_events": events,
            "run_isolated": isolated})
        with tempfile.TemporaryDirectory() as temporary:
            obj = object.__new__(scope["SCIntegratorPhysicalExecutor"])
            obj.root = Path(temporary) / "stage"
            obj.ledger = obj.root / "physical_attempt_ledger.jsonl"
            obj.identity = {"schema": "sc_integrator_bulk_native_v1"}
            obj.identity_sha256 = digest(obj.identity)
            obj.limit, obj.timeout = 1, 10
            first = obj({"x": 1})
            second = obj({"x": 1})
            self.assertEqual(first, failure)
            self.assertEqual(second, failure)
            self.assertEqual(len(calls), 1)
            self.assertEqual([row["event"] for row in events(obj.ledger)],
                             ["reserved", "finished"])
            self.assertEqual(events(obj.ledger)[1]["outcome"], "complete")
            self.assertFalse((obj.root / ".lock").exists())
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                obj({"x": 2})


if __name__ == "__main__":
    unittest.main()
