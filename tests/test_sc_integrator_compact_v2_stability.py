"""Offline-only failure, usage and parser checks; no provider or SPICE calls."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from ActiveLearning.batch_runner_v2 import runner
from ActiveLearning.batch_runner_v2 import sc_integrator_production as production
from ActiveLearning.batch_runner_v2.storage import atomic_json, sha256_file
from ActiveLearning.experiment_logging import append_jsonl_record
from ActiveLearning.inverse_design.llm_client import (
    LLMTransientError, call_inverse_design_llm,
)
from ActiveLearning.performance_prediction import performance_predictor as predictor


METRICS = (
    "integration_step_magnitude_v", "zero_input_drift_v_per_cycle",
    "step_consistency_v", "virtual_ground_error_v", "output_headroom_v",
)
IDS = [f"candidate_{index:02d}" for index in range(10)]


_AL_SOURCE = Path(__file__).resolve().parents[1] / "ActiveLearning/active_learning.py"
_AL_TREE = ast.parse(_AL_SOURCE.read_text(encoding="utf-8"), filename=str(_AL_SOURCE))
_AL_NAMES = {"_DurableUsageLog", "_AttemptEvents", "_sum_token_usage",
             "_make_logged_llm_client", "run_active_learning"}
_AL_NODES = [node for node in _AL_TREE.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in _AL_NAMES]
assert {node.name for node in _AL_NODES} == _AL_NAMES
_AL = {"Path": Path, "append_jsonl_record": append_jsonl_record,
       "inspect": inspect, "hashlib": hashlib, "json": json, "time": time,
       "call_inverse_design_llm": call_inverse_design_llm,
       "_TOKEN_FIELDS": ("input_tokens", "output_tokens", "total_tokens"),
       "_STAGE_NAMES": ("inverse_design", "data_expansion", "performance_prediction",
                        "acquisition", "real_spice"),
       "VALIDITY_V1_DEFAULTS": {"enabled": False, "mode": "performance_tiebreak"},
       "_recompute_initial_meets_spec": lambda *_args, **_kwargs: None}
exec(compile(ast.Module(body=_AL_NODES, type_ignores=[]), str(_AL_SOURCE), "exec"), _AL)


def _response(ids=IDS, metrics=METRICS):
    return json.dumps({"predictions": [
        {"candidate_id": candidate_id,
         "predicted_metrics": {name: float(index + offset + 1)
                               for offset, name in enumerate(metrics)}}
        for index, candidate_id in enumerate(ids)]})


def _rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


class CompactV2StabilityTests(unittest.TestCase):
    def _worker_fixture(self, root, job_id="sc2510", attempt_id="attempt_1"):
        attempt = Path(root) / "mock" / "jobs" / job_id / "attempts" / attempt_id
        attempt.mkdir(parents=True)
        job = {"job_id": job_id, "circuit_id": "circuit_2510"}
        run_id = "mock_run"
        run_dir = attempt / "al_runs" / "SCIntegrator" / job["circuit_id"] / run_id
        run_dir.mkdir(parents=True)
        atomic_json(attempt / "al_run_context.json", {
            "run_id": run_id, "run_directory": str(run_dir)})
        return job, attempt, run_dir

    @staticmethod
    def _mark_reaped(attempt):
        atomic_json(attempt / "child_reaped.json", {
            "attempt_id": attempt.name, "exit_confirmed": True, "exit_code": 124})

    @staticmethod
    def _usage_row(tokens=5, **identity):
        return {"input_tokens": tokens - 1, "output_tokens": 1,
                "total_tokens": tokens, **identity}

    def test_worker_timeout_recovers_known_usage_and_keeps_terminal_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, run_dir = self._worker_fixture(directory)
            for tokens in (5, 7):
                append_jsonl_record(run_dir / "llm_usage.jsonl", self._usage_row(tokens))
            with patch("ActiveLearning.sample_hold_posix.run_isolated",
                       side_effect=subprocess.TimeoutExpired("mock", 1)):
                outcome = production._worker({"manifest_sha256": "mock", "batch_id": "mock"},
                                             job, attempt)
            self.assertEqual(outcome["state"], "FAILED_TERMINAL")
            self.assertEqual(outcome["error_code"], "WORKER_TIMEOUT_REAPED")
            self.assertEqual(outcome["llm_calls"], 2)
            self.assertEqual(outcome["total_tokens"]["total_tokens"], 12)
            self.assertTrue(outcome["usage_unknown"])
            self.assertTrue(json.loads((attempt / "child_reaped.json").read_text())["exit_confirmed"])
            self.assertEqual(len(_rows(run_dir / "llm_usage.jsonl")), 2)
            state = {"batch_id": "mock", "offline_only": False,
                     "execution_scope": "sc_integrator_production",
                     "jobs": {job["job_id"]: {**outcome, "circuit_type": "SCIntegrator",
                                              "pipeline_success": False, "meets_spec": None,
                                              "attempts": [{}]}}}
            with patch.object(runner, "status_batch", return_value=state):
                report = runner.report_batch("mock", directory)
            self.assertEqual(report["sc_integrator_usage"]["reported_total_tokens"], 12)
            self.assertTrue(report["api_usage"]["usage_unknown"])
            self.assertIsNone(report["api_usage"]["total_tokens"])
            self.assertEqual(report["jobs"][job["job_id"]]["state"], "FAILED_TERMINAL")

    def test_worker_timeout_without_usage_keeps_cost_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, _ = self._worker_fixture(directory)
            with patch("ActiveLearning.sample_hold_posix.run_isolated",
                       side_effect=subprocess.TimeoutExpired("mock", 1)):
                outcome = production._worker({"manifest_sha256": "mock", "batch_id": "mock"},
                                             job, attempt)
            self.assertNotIn("total_tokens", outcome)
            self.assertNotIn("llm_calls", outcome)
            self.assertTrue(outcome["usage_unknown"])
            self.assertEqual(outcome["usage_recovery_issue"], "USAGE_LOG_MISSING")

    def test_worker_timeout_keeps_complete_prefix_before_partial_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, run_dir = self._worker_fixture(directory)
            path = run_dir / "llm_usage.jsonl"
            append_jsonl_record(path, self._usage_row(5))
            append_jsonl_record(path, self._usage_row(7))
            with path.open("ab") as stream:
                stream.write(b'{"input_tokens":9')
            original = path.read_bytes()
            with patch("ActiveLearning.sample_hold_posix.run_isolated",
                       side_effect=subprocess.TimeoutExpired("mock", 1)):
                outcome = production._worker({"manifest_sha256": "mock", "batch_id": "mock"},
                                             job, attempt)
            self.assertEqual(outcome["total_tokens"]["total_tokens"], 12)
            self.assertEqual(outcome["llm_calls"], 2)
            self.assertEqual(outcome["usage_recovery_issue"], "USAGE_TRAILING_PARTIAL")
            self.assertTrue(outcome["usage_unknown"])
            self.assertEqual(path.read_bytes(), original)

    def test_child_exit_recovers_usage_without_changing_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, run_dir = self._worker_fixture(directory)
            append_jsonl_record(run_dir / "llm_usage.jsonl", self._usage_row(5))
            completed = subprocess.CompletedProcess(["mock"], 1, "", "mock child failure")
            with patch("ActiveLearning.sample_hold_posix.run_isolated", return_value=completed):
                outcome = production._worker({"manifest_sha256": "mock", "batch_id": "mock"},
                                             job, attempt)
            self.assertEqual(outcome["error_code"], "CHILD_EXIT_1")
            self.assertEqual(outcome["total_tokens"]["total_tokens"], 5)
            self.assertTrue(outcome["usage_unknown"])

    def test_completed_worker_uses_child_result_without_usage_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, run_dir = self._worker_fixture(directory)
            append_jsonl_record(run_dir / "llm_usage.jsonl", self._usage_row(5))
            result_path = attempt / "summary.json"
            atomic_json(result_path, {"llm_calls": 1})
            child_result = {"state": "COMPLETED", "result_path": str(result_path),
                            "al_result_sha256": sha256_file(result_path),
                            "llm_calls": 1, "total_tokens": {"total_tokens": 5}}
            atomic_json(attempt / "worker_result.json", child_result)
            completed = subprocess.CompletedProcess(["mock"], 0, "", "")
            with (patch("ActiveLearning.sample_hold_posix.run_isolated", return_value=completed),
                  patch.object(production, "_failed_usage") as recovery):
                outcome = production._worker({"manifest_sha256": "mock", "batch_id": "mock"},
                                             job, attempt)
            recovery.assert_not_called()
            self.assertEqual(outcome["llm_calls"], 1)
            self.assertEqual(outcome["total_tokens"]["total_tokens"], 5)
            self.assertEqual(len(_rows(run_dir / "llm_usage.jsonl")), 1)

    def test_usage_from_another_job_or_attempt_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, own_run = self._worker_fixture(directory)
            _, foreign_attempt, foreign_run = self._worker_fixture(
                directory, job_id="sc2511", attempt_id="attempt_2")
            append_jsonl_record(foreign_run / "llm_usage.jsonl", self._usage_row(9))
            atomic_json(attempt / "al_run_context.json", {
                "run_id": "mock_run", "run_directory": str(foreign_run)})
            self._mark_reaped(attempt)
            outcome = production._failed_usage({"batch_id": "mock"}, job, attempt)
            self.assertNotIn("total_tokens", outcome)
            self.assertEqual(outcome["usage_recovery_issue"], "USAGE_IDENTITY_MISMATCH")
            self.assertTrue((foreign_attempt / "al_run_context.json").exists())

            _, sibling_attempt, _ = self._worker_fixture(
                directory, job_id=job["job_id"], attempt_id="attempt_2")
            atomic_json(sibling_attempt / "al_run_context.json", {
                "run_id": "mock_run", "run_directory": str(own_run)})
            self._mark_reaped(sibling_attempt)
            sibling = production._failed_usage({"batch_id": "mock"}, job, sibling_attempt)
            self.assertNotIn("total_tokens", sibling)
            self.assertEqual(sibling["usage_recovery_issue"], "USAGE_IDENTITY_MISMATCH")

    def test_recovery_requires_confirmed_reap_and_matching_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, run_dir = self._worker_fixture(directory)
            append_jsonl_record(run_dir / "llm_usage.jsonl", self._usage_row(9))
            unreaped = production._failed_usage({"batch_id": "mock"}, job, attempt)
            self.assertNotIn("total_tokens", unreaped)
            self.assertEqual(unreaped["usage_recovery_issue"], "USAGE_REAP_UNCONFIRMED")
            self._mark_reaped(attempt)
            wrong_batch = production._failed_usage({"batch_id": "other"}, job, attempt)
            self.assertNotIn("total_tokens", wrong_batch)
            self.assertEqual(wrong_batch["usage_recovery_issue"],
                             "USAGE_CONTEXT_MISSING_OR_UNOWNED")

    def test_unreaped_process_never_reads_usage(self):
        from ActiveLearning.sample_hold_posix import ProcessGroupUncertain
        from ActiveLearning.batch_runner_v2.runner import UnverifiedOrphanError

        with tempfile.TemporaryDirectory() as directory:
            job, attempt, run_dir = self._worker_fixture(directory)
            append_jsonl_record(run_dir / "llm_usage.jsonl", self._usage_row(9))
            with (patch("ActiveLearning.sample_hold_posix.run_isolated",
                        side_effect=ProcessGroupUncertain("still alive")),
                  patch.object(production, "_failed_usage") as recovery):
                with self.assertRaises(UnverifiedOrphanError):
                    production._worker({"manifest_sha256": "mock", "batch_id": "mock"},
                                       job, attempt)
            recovery.assert_not_called()
            self.assertFalse((attempt / "child_reaped.json").exists())

    def test_corrupt_record_keeps_verified_prefix_and_explicit_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, run_dir = self._worker_fixture(directory)
            path = run_dir / "llm_usage.jsonl"
            append_jsonl_record(path, self._usage_row(5))
            with path.open("ab") as stream:
                stream.write(b"not json\n")
                stream.write((json.dumps(self._usage_row(9)) + "\n").encode())
            self._mark_reaped(attempt)
            outcome = production._failed_usage({"batch_id": "mock"}, job, attempt)
            self.assertEqual(outcome["total_tokens"]["total_tokens"], 5)
            self.assertEqual(outcome["usage_recovery_issue"], "USAGE_CORRUPT_RECORD")
            self.assertTrue(outcome["usage_unknown"])

    def test_record_identity_mismatch_does_not_count_foreign_row(self):
        with tempfile.TemporaryDirectory() as directory:
            job, attempt, run_dir = self._worker_fixture(directory)
            path = run_dir / "llm_usage.jsonl"
            append_jsonl_record(path, self._usage_row(5))
            append_jsonl_record(path, self._usage_row(9, job_id="sc-other"))
            self._mark_reaped(attempt)
            outcome = production._failed_usage({"batch_id": "mock"}, job, attempt)
            self.assertEqual(outcome["total_tokens"]["total_tokens"], 5)
            self.assertEqual(outcome["usage_recovery_issue"],
                             "USAGE_RECORD_IDENTITY_MISMATCH")

    def test_batch_timeout_usage_isolated_and_pending_job_continues(self):
        jobs = [{"job_id": circuit, "circuit_id": circuit,
                 "circuit_type": "SCIntegrator", "configuration_id": circuit,
                 "config_sha256": "a" * 64, "d0_sha256": "b" * 64,
                 "source_sha256": "c" * 64, "max_iterations": 5, "seed": 42}
                for circuit in ("circuit_2510", "circuit_2513")]
        manifest = {"batch_id": "MTV2_SCI_PREP_001", "manifest_sha256": "d" * 64,
                    "provider": "openai", "model": "mock", "credential_scope": "mock",
                    "retry_policy": {"automatic_native_retries": 0},
                    "resource_limits": {"worker_timeout_seconds": 2400}, "jobs": jobs}
        calls = []

        def worker(job, attempt):
            calls.append(job["job_id"])
            if job["job_id"] == "circuit_2510":
                atomic_json(attempt / "child_spawn_intent.json", {
                    "manifest_sha256": manifest["manifest_sha256"],
                    "job_id": job["job_id"], "attempt_id": attempt.name})
                atomic_json(attempt / "child_reaped.json", {
                    "attempt_id": attempt.name, "exit_confirmed": True,
                    "exit_code": 124})
                return {"state": "FAILED_TERMINAL", "error_code": "WORKER_TIMEOUT_REAPED",
                        "llm_calls": 2, "total_tokens": {"input_tokens": 10,
                                                         "output_tokens": 2,
                                                         "total_tokens": 12},
                        "usage_unknown": True,
                        "usage_recovery_issue": "USAGE_TRAILING_PARTIAL"}
            return {"state": "COMPLETED", "stop_reason": "max_iterations",
                    "iterations_completed": 5, "meets_spec": False,
                    "parameter_valid": True, "simulation_valid": True,
                    "functional_valid": True, "final_metrics": {},
                    "llm_calls": 1, "total_tokens": {"total_tokens": 20},
                    "total_timing": {"total_seconds": 1.0}}

        with tempfile.TemporaryDirectory() as directory, patch.object(
                runner, "verify_job", return_value=None):
            report = runner._run_batch(manifest, directory, worker, offline_only=False,
                                       execution_scope="sc_integrator_production")
            self.assertEqual(report["jobs"]["circuit_2510"]["state"], "FAILED_TERMINAL")
            self.assertEqual(report["jobs"]["circuit_2510"]["error_code"],
                             "WORKER_TIMEOUT_REAPED")
            self.assertEqual(report["jobs"]["circuit_2510"]["usage_recovery_issue"],
                             "USAGE_TRAILING_PARTIAL")
            self.assertEqual(report["jobs"]["circuit_2513"]["state"], "COMPLETED")
            self.assertEqual(report["sc_integrator_usage"]["reported_total_tokens"], 32)
            self.assertTrue(report["sc_integrator_usage"]["usage_unknown"])
            runner._run_batch(manifest, directory, worker, offline_only=False,
                              execution_scope="sc_integrator_production")
        self.assertEqual(calls, ["circuit_2510", "circuit_2513"])

    def test_v2_example_is_real_json_and_lists_exact_metric_keys(self):
        template = (Path(__file__).resolve().parents[1] /
                    "ActiveLearning/performance_prediction/prompts/circuit_types/"
                    "sc_integrator_compact_v2.txt").read_text(encoding="utf-8")
        example = next(line for line in template.splitlines()
                       if line.startswith('{"predictions":'))
        parsed = json.loads(example)
        self.assertEqual(set(parsed["predictions"][0]["predicted_metrics"]), set(METRICS))
        self.assertIn("{candidate_ids}", template)
        self.assertIn("{metric_names}", template)

    def test_json_fences_bom_and_safe_categories(self):
        valid = _response()
        for raw in (valid, f"```json\n{valid}\n```", "\ufeff" + valid):
            self.assertEqual(list(predictor._parse_predictions(
                raw, IDS, METRICS, detailed=True)), IDS)
        cases = [
            ("explanation " + valid, "JSON_SYNTAX"),
            ('{"predictions":[', "JSON_SYNTAX"),
            (_response(IDS[:-1]), "CANDIDATE_MISSING"),
            (_response(IDS[:-1] + [IDS[0]]), "CANDIDATE_DUPLICATE"),
        ]
        for raw, category in cases:
            with self.subTest(category=category):
                with self.assertRaises(predictor.PredictionFormatError) as caught:
                    predictor._parse_predictions(raw, IDS, METRICS, detailed=True)
                self.assertEqual(caught.exception.category, category)
        for replacement in (math.nan, math.inf, -math.inf):
            document = json.loads(valid)
            document["predictions"][0]["predicted_metrics"][METRICS[0]] = replacement
            with self.assertRaises(predictor.PredictionFormatError) as caught:
                predictor._parse_predictions(json.dumps(document), IDS, METRICS,
                                             detailed=True)
            self.assertEqual(caught.exception.category, "METRIC_NONFINITE")
        document = json.loads(valid)
        del document["predictions"][0]["predicted_metrics"][METRICS[0]]
        with self.assertRaises(predictor.PredictionFormatError) as caught:
            predictor._parse_predictions(json.dumps(document), IDS, METRICS,
                                         detailed=True)
        self.assertEqual(caught.exception.category, "METRIC_MISSING")

    def test_three_format_failures_keep_durable_usage_and_attempt_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = _AL["_DurableUsageLog"](directory)
            events = _AL["_AttemptEvents"](directory)

            def bad_client(_prompt, *, usage_log, usage_context):
                usage_log.append({**usage_context, "input_tokens": 10,
                                  "output_tokens": 1, "total_tokens": 11})
                return "not json"

            client = _AL["_make_logged_llm_client"](
                bad_client, usage, 2, "performance_prediction", events)
            with self.assertRaisesRegex(ValueError, "round 1 failed after 3 attempts"):
                predictor._collect_committee_rounds(
                    client, "TASK", IDS, METRICS, 5, detailed=True)
            saved = _rows(Path(directory) / "llm_usage.jsonl")
            self.assertEqual(len(saved), 3)
            self.assertEqual(sum(row["total_tokens"] for row in saved), 33)
            self.assertEqual([row["format_attempt"] for row in saved], [1, 2, 3])
            self.assertTrue(all(row["committee_member"] == 1 for row in saved))
            failures = [row for row in _rows(Path(directory) / "llm_attempt_events.jsonl")
                        if row["event"] == "format_failed"]
            self.assertEqual([row["category"] for row in failures], ["JSON_SYNTAX"] * 3)
            self.assertFalse(events.usage_unknown)

    def test_three_transport_timeouts_are_distinct_from_format_attempts(self):
        create = MagicMock(side_effect=TimeoutError("mock timeout"))
        api = SimpleNamespace(responses=SimpleNamespace(create=create))
        fake_openai = SimpleNamespace(OpenAI=MagicMock(return_value=api))
        with tempfile.TemporaryDirectory() as directory:
            usage = _AL["_DurableUsageLog"](directory)
            journal = _AL["_AttemptEvents"](directory)
            client = _AL["_make_logged_llm_client"](
                None, usage, 3, "performance_prediction", journal)
            client.set_prediction_attempt(4, 2)
            with (patch.dict(sys.modules, {"openai": fake_openai}),
                  patch.dict(os.environ, {
                      "AL_LLM_PROVIDER": "openai", "AL_OPENAI_TRANSPORT": "responses",
                      "OPENAI_API_KEY": "mock-key", "OPENAI_MODEL": "mock-model",
                      "OPENAI_BASE_URL": "https://mock.invalid"}),
                  patch("ActiveLearning.inverse_design.llm_client.time.sleep")):
                with self.assertRaises(LLMTransientError):
                    client("TASK")
            events = _rows(Path(directory) / "llm_attempt_events.jsonl")
            self.assertTrue(journal.usage_unknown)
            self.assertEqual(len(usage), 0)
        self.assertEqual(create.call_count, 3)
        self.assertEqual([row["transport_attempt"] for row in events
                          if row["event"] == "transport_failed"], [1, 2, 3])
        self.assertTrue(all(row["category"] == "timeout" for row in events
                            if row["event"] == "transport_failed"))
        self.assertTrue(all(row["committee_member"] == 4 and row["format_attempt"] == 2
                            for row in events if row["event"] == "transport_failed"))
        self.assertFalse(any("mock-key" in json.dumps(row) for row in events))

    def test_failed_al_iteration_retains_known_usage_without_iteration_completion(self):
        def usage_client(_prompt, *, usage_log, usage_context):
            usage_log.append({**usage_context, "input_tokens": 7,
                              "output_tokens": 2, "total_tokens": 9})
            return "{}"

        def inverse(*_args, llm_client, **_kwargs):
            llm_client("INVERSE")
            return {"x_seed": {"X": 1.0}, "inverse_context": [],
                    "parameter_valid": True}

        def prediction(*_args, llm_client, **_kwargs):
            llm_client.set_prediction_attempt(1, 1)
            llm_client("PREDICTION")
            raise RuntimeError("mock mid-iteration failure")

        record = {"sample_id": "d0", "metrics": {"metric": 2.0},
                  "status": {"parameter_valid": True, "simulation_valid": True,
                             "functional_valid": True, "meets_spec": False}}
        parameter_template = {"optimization_space": {"variables": ["X"]},
                              "parameters": [{"name": "X", "variable_type": "continuous",
                                              "admissible_range": {"lower": 0.0,
                                                                   "upper": 2.0}}]}
        performance_template = {"performance_template": {
            "active_metrics": ["metric"], "metrics": {"metric": {"direction": "minimize"}}}}
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(_AL, {
                "propose_inverse_design_seed": inverse,
                "expand_candidates": lambda *_args, **_kwargs: (
                    [{"candidate_id": "candidate_00", "params": {"X": 1.0},
                      "normalized_params": {"X": 0.5}}], {}),
                "predict_candidate_performance": prediction,
            }):
                with self.assertRaisesRegex(RuntimeError, "mid-iteration"):
                    _AL["run_active_learning"](
                        [record], {"circuit_type": "SCIntegrator", "circuit_id": "mock"},
                        parameter_template, performance_template,
                        {"metric": {"operator": "<=", "value": 1.0}},
                        adapter="mock", max_iterations=1, stop_on_meets_spec=False,
                        prediction_prompt_version="compact_v2", output_dir=directory,
                        llm_client=usage_client)
            saved = _rows(Path(directory) / "llm_usage.jsonl")
            self.assertEqual(len(saved), 2)
            self.assertEqual(sum(row["total_tokens"] for row in saved), 18)
            self.assertFalse((Path(directory) / "iteration_log.jsonl").exists())
            self.assertEqual(saved[1]["committee_member"], 1)

    def test_legacy_usage_fields_and_failed_batch_partial_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = _AL["_DurableUsageLog"](directory)
            legacy = {"iteration": 1, "stage": "inverse_design", "round": 1,
                      "input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
            usage.append(legacy)
            self.assertEqual(_rows(Path(directory) / "llm_usage.jsonl"), [legacy])
            self.assertEqual(_AL["_sum_token_usage"](usage)["total_tokens"], 5)
            state = {"batch_id": "mock", "offline_only": False,
                     "execution_scope": "sc_integrator_production",
                     "jobs": {"circuit_2510": {
                         "state": "FAILED_TERMINAL", "circuit_type": "SCIntegrator",
                         "pipeline_success": False, "meets_spec": None,
                         "attempts": [{}], "error_code": "CHILD_EXIT_1",
                         "llm_calls": 1,
                         "total_tokens": {"input_tokens": 3, "output_tokens": 2,
                                          "total_tokens": 5}, "usage_unknown": True}}}
            with patch.object(runner, "status_batch", return_value=state):
                report = runner.report_batch("mock", directory)
            self.assertEqual(report["sc_integrator_usage"]["reported_total_tokens"], 5)
            self.assertTrue(report["sc_integrator_usage"]["usage_unknown"])
            self.assertIsNone(report["api_usage"]["total_tokens"])
            self.assertEqual(report["jobs"]["circuit_2510"]["api_usage"][
                "reported_total_tokens"], 5)

    def test_successful_response_without_usage_is_explicitly_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = _AL["_DurableUsageLog"](directory)
            events = _AL["_AttemptEvents"](directory)
            client = _AL["_make_logged_llm_client"](
                lambda _prompt: "{}", usage, 1, "performance_prediction", events)
            client.set_prediction_attempt(1, 1)
            self.assertEqual(client("TASK"), "{}")
            self.assertEqual(len(usage), 0)
            self.assertTrue(events.usage_unknown)
            self.assertFalse((Path(directory) / "llm_usage.jsonl").exists())
            self.assertEqual(_rows(Path(directory) / "llm_attempt_events.jsonl")[-1][
                "usage_known"], False)


if __name__ == "__main__":
    unittest.main()
