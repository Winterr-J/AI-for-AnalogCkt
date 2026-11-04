"""SC-4C.3 static and fake-process coverage. No simulator is imported or run."""

import copy
import importlib.metadata
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ActiveLearning.sc_sample_hold_canary import (
    _approval, _native_environment_verified, load_plan, main, run_guarded,
    _native_launcher,
)
from ActiveLearning.sc_sample_hold_canary_child import _constructor_arguments
from ActiveLearning.sc_sample_hold_generic import (
    GenericCanaryError, ROOT, _topology_contract, build_stimulus,
    evaluate_physical, physical_waveforms,
)


def _linear(points, t):
    for (ta, va), (tb, vb) in zip(points, points[1:]):
        if ta <= t <= tb:
            return va + (vb - va) * (t - ta) / (tb - ta)
    return points[-1][1]


def _clock(pulse, t):
    phase = t % pulse["period_s"]
    rise, fall = pulse["rise_s"], pulse["fall_s"]
    plateau = pulse["high_plateau_s"]
    low, high = pulse["low_v"], pulse["high_v"]
    if phase < rise:
        return low + (high - low) * phase / rise
    if phase < rise + plateau:
        return high
    if phase < rise + plateau + fall:
        return high - (high - low) * (phase - rise - plateau) / fall
    return low


def _waveforms(plan, *, offset=0.0, stuck=False):
    fixture, stimulus = plan["fixture"], plan["stimulus"]
    step = fixture.max_timestep_s
    time = [i * step for i in range(round(stimulus["end_time_s"] / step) + 1)]
    traces = {"time": time, fixture.mapping.clock: [_clock(stimulus["clock_pulse"], t)
                                                  for t in time]}
    for name, points in stimulus["input_pwl_points_s_v"].items():
        traces[name] = [_linear(points, t) for t in time]
    if len(fixture.mapping.inputs) == 1:
        input_signal = traces[fixture.mapping.inputs[0]]
    else:
        plus, minus = fixture.mapping.inputs
        input_signal = [a - b for a, b in zip(traces[plus], traces[minus])]
    output = []
    for t, value in zip(time, input_signal):
        index = int((t + 1e-15) / fixture.conditions.clock_period_s) - fixture.startup_cycles
        if index < 0:
            output.append(0.0)
        else:
            cycle = fixture.cycles[min(index, 5)]
            output.append((0.0 if stuck else value if t < cycle.switch_off_s
                           else cycle.intended_v) + offset)
    if len(fixture.mapping.outputs) == 1:
        traces[fixture.mapping.outputs[0]] = output
        traces["net3"] = [v + 0.4 for v in output]
    else:
        cm = fixture.conditions.fixed_common_mode_v
        positive, negative = fixture.mapping.outputs
        traces[positive] = [cm + v / 2 for v in output]
        traces[negative] = [cm - v / 2 for v in output]
    return traces


class FakeLauncher:
    def __init__(self, plan, *, outcome="success"):
        self.plan, self.outcome = plan, outcome
        self.calls = 0
        self.saw_reservation = False

    def __call__(self, command, *, cwd, timeout, environment):
        self.calls += 1
        assert command[0] == sys.executable
        assert cwd == ROOT
        assert timeout <= 180
        assert "PYTHONPATH" not in environment
        experiment = Path(command[command.index("--experiment") + 1])
        ledger = [json.loads(line) for line in
                  (experiment / "physical_attempt_ledger.jsonl").read_text().splitlines()]
        self.saw_reservation = len(ledger) == 1 and ledger[0]["event"] == "native_attempt_reserved"
        if self.outcome == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        if self.outcome == "uncertain":
            from ActiveLearning.sc_0259_canary import UncertainProcessError
            raise UncertainProcessError("fake process-tree state unknown")
        if self.outcome == "exit_error":
            return subprocess.CompletedProcess(command, 2, "", "fake failure")
        (experiment / "raw_waveforms.json").write_text(json.dumps({
            "mock": True,
            "fixture_sha256": self.plan["fixture"].fixture_hash,
            "stimulus_sha256": self.plan["config"]["stimulus_sha256"],
            "waveforms": _waveforms(self.plan),
        }), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "fake stdout", "")


class StaticAndEvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plans = {key: load_plan(ROOT / "ActiveLearning/docs/reference_audit" /
                                   f"SC_{key}_CANARY_FIXTURE_CANDIDATE_SC4C4.json")
                     for key in ("0605", "0263")}

    def test_offline_cli_does_not_launch_or_import_native(self):
        with mock.patch("ActiveLearning.sc_sample_hold_canary._native_launcher") as native:
            for plan in self.plans.values():
                self.assertEqual(main(["--candidate", str(plan["path"])]), 0)
            native.assert_not_called()
        self.assertNotIn("PySpice", sys.modules)

    def test_topology_and_explicit_nominal_assembly(self):
        for key, plan in self.plans.items():
            config = plan["config"]
            _topology_contract(config, plan["paths"])
            kwargs = _constructor_arguments(config, plan["stimulus"])
            self.assertEqual(set(kwargs), set(config["assembly"]["constructor_arguments"]))
            self.assertEqual(kwargs["NMOS_MODEL"], "NMOS_IDEAL")
            self.assertEqual(len(config["original_netlist_lines"]), 4)
            if key == "0605":
                self.assertEqual(kwargs["R0"], 10000.0)
                self.assertEqual(kwargs["VDD"], 3.3)
                self.assertIn("R0 (VOUT1 VSS) resistor", config["original_netlist_lines"])
                self.assertEqual(config["mapping"]["outputs"], ["VOUT1"])
                self.assertEqual(config["mapping"]["diagnostic_nodes"], ["net3"])
            else:
                self.assertNotIn("VDD", kwargs)
                self.assertIn("PWL(", kwargs["VIN1"])
                self.assertIn("PWL(", kwargs["VIN2"])
                self.assertNotEqual(kwargs["VIN1"], kwargs["VIN2"])

    def test_duplicate_sources_and_wrong_model_rejected(self):
        plan = self.plans["0263"]
        for mutation in (lambda c: c["mapping"]["inputs"].__setitem__(1, "VIN1"),
                         lambda c: c["assembly"]["model"].__setitem__("VTO", 0.8),
                         lambda c: c["assembly"]["constructor_arguments"]["C0"].__setitem__("value", 2e-12)):
            config = copy.deepcopy(plan["config"])
            mutation(config)
            with self.assertRaises(GenericCanaryError):
                _topology_contract(config, plan["paths"])

    def test_six_cycle_timing_and_windows(self):
        for key, plan in self.plans.items():
            fixture, stimulus = plan["fixture"], plan["stimulus"]
            self.assertEqual(len(fixture.cycles), 6)
            self.assertEqual([cycle.level for cycle in fixture.cycles],
                             ["LOW", "MID", "HIGH", "MID", "LOW", "HIGH"])
            self.assertEqual(stimulus["end_time_s"], 8e-6)
            self.assertEqual(fixture.max_timestep_s, 1e-9)
            for cycle in fixture.cycles:
                self.assertLess(cycle.track_step_s + fixture.conditions.vin_edge_s,
                                cycle.track_end_s)
                self.assertLess(cycle.track_end_s, cycle.switch_off_s)
                self.assertLess(cycle.early_hold_s, cycle.quiet_start_s)
                self.assertLess(cycle.quiet_end_s, cycle.disturbance_s)
                self.assertLess(cycle.disturbance_s + fixture.conditions.vin_edge_s,
                                cycle.post_disturbance_s)
                self.assertLess(cycle.post_disturbance_s, cycle.hold_end_s)
            self.assertAlmostEqual(stimulus["clock_pulse"]["high_plateau_s"],
                                   745e-9 if key == "0605" else 495e-9)

    def test_physical_stimulus_bounds_and_common_mode(self):
        for key, plan in self.plans.items():
            points = plan["stimulus"]["input_pwl_points_s_v"]
            for name, samples in points.items():
                self.assertEqual(samples[0][0], 0)
                self.assertAlmostEqual(samples[-1][0], 8e-6)
                self.assertTrue(all(a[0] < b[0] for a, b in zip(samples, samples[1:])))
                low, high = plan["config"]["physical_input_bounds_v"][name]
                self.assertTrue(all(low <= value <= high for _, value in samples))
            if key == "0263":
                plus, minus = points.values()
                self.assertTrue(all(math.isclose(a[1] + b[1], 2.4, abs_tol=1e-12)
                                    for a, b in zip(plus, minus)))
                self.assertAlmostEqual(min(v for _, v in plus), 1.05)
                self.assertAlmostEqual(max(v for _, v in plus), 1.35)

    def test_invalid_bounds_and_events_rejected(self):
        for key, plan in self.plans.items():
            bad = copy.deepcopy(plan["config"]["physical_input_bounds_v"])
            first = next(iter(bad))
            bad[first] = [1.1, 1.2]
            with self.assertRaises(GenericCanaryError):
                build_stimulus(plan["fixture"], startup_input_v=plan["config"]["startup_input_v"],
                               physical_input_bounds_v=bad)
        altered = copy.copy(self.plans["0605"]["fixture"])
        cycle = copy.copy(altered.cycles[0])
        object.__setattr__(cycle, "disturbance_s", cycle.quiet_end_s)
        object.__setattr__(altered, "cycles", (cycle, *altered.cycles[1:]))
        with self.assertRaises(GenericCanaryError):
            build_stimulus(altered, startup_input_v=1.2,
                           physical_input_bounds_v={"VIN1": [0.9, 1.5]})

    def test_external_buffer_offset_not_corrected_and_stuck_fails(self):
        plan = self.plans["0605"]
        nominal = evaluate_physical(plan["fixture"], _waveforms(plan), mock=True)["evaluation"]
        offset = evaluate_physical(plan["fixture"], _waveforms(plan, offset=-0.2), mock=True)["evaluation"]
        stuck = evaluate_physical(plan["fixture"], _waveforms(plan, stuck=True), mock=True)["evaluation"]
        self.assertTrue(nominal["functional_valid"])
        self.assertTrue(offset["functional_valid"])
        self.assertGreater(offset["metrics"]["track_end_error_v"],
                           nominal["metrics"]["track_end_error_v"] + 0.19)
        self.assertFalse(stuck["functional_valid"])
        self.assertIsNone(offset["meets_spec"])
        self.assertEqual(offset["diagnostics"]["active_metrics"],
                         ["track_end_error_v", "sampling_error_v", "hold_isolation_error_v"])

    def test_differential_sign_six_cycles_and_raw_names(self):
        plan = self.plans["0263"]
        traces = _waveforms(plan)
        result = evaluate_physical(plan["fixture"], traces, mock=True)
        self.assertTrue(result["evaluation"]["functional_valid"])
        self.assertIsNone(result["evaluation"]["meets_spec"])
        self.assertEqual(len(result["evaluation"]["diagnostics"]["cycles"]), 6)
        self.assertEqual(result["normalized_diagnostics"]["provenance"],
                         {"VIN_DIFF": "VIN1-VIN2", "VOUT_DIFF": "VOUT1-VOUT2"})
        self.assertEqual(result["normalized_diagnostics"]["VIN_DIFF"][3000],
                         traces["VIN1"][3000] - traces["VIN2"][3000])
        self.assertEqual(set(result["raw_physical_signal_names"]), set(traces))

    def test_missing_misaligned_nonfinite_physical_traces_rejected(self):
        plan = self.plans["0263"]
        traces = _waveforms(plan)
        for broken in (lambda data: data.pop("VOUT2"),
                       lambda data: data["VIN1"].pop(),
                       lambda data: data["VIN1"].__setitem__(0, float("nan"))):
            bad = copy.deepcopy(traces)
            broken(bad)
            with self.assertRaises(GenericCanaryError):
                evaluate_physical(plan["fixture"], bad, mock=True)
        class Analysis:
            time = [0, 1, 2]
            nodes = {"vin1": [0, 1, 2]}
        with self.assertRaises(GenericCanaryError):
            physical_waveforms(Analysis(), plan["fixture"])

    def test_unapproved_requests_and_environment_fail_closed(self):
        for key, plan in self.plans.items():
            request = (ROOT / "ActiveLearning/docs/reference_audit" /
                       f"SC_{key}_CANARY_APPROVAL_REQUEST_SC4C4.json")
            with self.assertRaises(GenericCanaryError):
                _approval(plan, request)
            actual = importlib.metadata.version
            with mock.patch("ActiveLearning.sc_sample_hold_canary.importlib.metadata.version",
                            side_effect=lambda name: "305.1" if name == "pywin32" else actual(name)):
                self.assertFalse(_native_environment_verified(plan["closure"]))

    def test_candidate_fixture_source_and_closure_tamper_rejected(self):
        original = self.plans["0605"]["config"]
        for mutate in (
                lambda c: c.__setitem__("fixture_sha256", "0" * 64),
                lambda c: c["source_assets"]["topology"].__setitem__("sha256", "0" * 64),
                lambda c: c["source_closure"].__setitem__("sha256", "0" * 64)):
            with tempfile.TemporaryDirectory(dir=ROOT) as directory:
                candidate = Path(directory) / "mock_tampered_candidate.json"
                altered = copy.deepcopy(original)
                mutate(altered)
                candidate.write_text(json.dumps(altered), encoding="utf-8")
                with self.assertRaises(GenericCanaryError):
                    load_plan(candidate)


class FakeProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_plan(ROOT / "ActiveLearning/docs/reference_audit/"
                             "SC_0605_CANARY_FIXTURE_CANDIDATE_SC4C4.json")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output_root = Path(temp.name) / "fake_runs"
        self.fake_approval = Path(temp.name) / "not_an_approval.json"
        self.fake_approval.write_text("{}", encoding="utf-8")

    def _run(self, launcher, *, acknowledgement=True):
        with mock.patch("ActiveLearning.sc_sample_hold_canary._approval",
                        return_value={"output_root": str(self.output_root)}):
            return run_guarded(self.plan, self.fake_approval,
                               acknowledgement=acknowledgement, launcher=launcher)

    def _ledger(self):
        path = (self.output_root / self.plan["config"]["experiment_id"] /
                "physical_attempt_ledger.jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_fake_success_is_audited_and_never_replayed(self):
        fake = FakeLauncher(self.plan)
        result = self._run(fake)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["mock"])
        self.assertTrue(fake.saw_reservation)
        self.assertEqual(fake.calls, 1)
        self.assertEqual([item["event"] for item in self._ledger()],
                         ["native_attempt_reserved", "native_attempt_finished"])
        with self.assertRaises(GenericCanaryError):
            self._run(fake)
        self.assertEqual(fake.calls, 1)

    def test_fake_timeout_consumes_only_attempt_and_no_retry(self):
        fake = FakeLauncher(self.plan, outcome="timeout")
        with self.assertRaises(subprocess.TimeoutExpired):
            self._run(fake)
        self.assertTrue(fake.saw_reservation)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self._ledger()[-1]["outcome"], "timeout")
        with self.assertRaises(GenericCanaryError):
            self._run(fake)
        self.assertEqual(fake.calls, 1)

    def test_uncertain_tree_preserves_lock(self):
        fake = FakeLauncher(self.plan, outcome="uncertain")
        from ActiveLearning.sc_0259_canary import UncertainProcessError
        with self.assertRaises(UncertainProcessError):
            self._run(fake)
        self.assertTrue((self.output_root /
                         (self.plan["config"]["experiment_id"] + ".lock")).exists())
        self.assertEqual(self._ledger()[-1]["outcome"], "interrupted_or_uncertain")

    def test_no_acknowledgement_or_invalid_approval_never_launches(self):
        fake = FakeLauncher(self.plan)
        with self.assertRaises(GenericCanaryError):
            self._run(fake, acknowledgement=False)
        with self.assertRaises(GenericCanaryError):
            run_guarded(self.plan, self.fake_approval,
                        acknowledgement=True, launcher=fake)
        self.assertEqual(fake.calls, 0)
        self.assertFalse(self.output_root.exists())

    def test_native_launcher_cannot_start_with_unverified_environment(self):
        with mock.patch("ActiveLearning.sc_sample_hold_canary._approval",
                        return_value={"output_root": str(self.output_root)}), mock.patch(
                            "ActiveLearning.sc_sample_hold_canary._native_environment_verified",
                            return_value=False):
            with self.assertRaises(GenericCanaryError):
                run_guarded(self.plan, self.fake_approval,
                            acknowledgement=True, launcher=_native_launcher)
        self.assertFalse(self.output_root.exists())

    def test_fake_child_nonzero_exit_is_recorded_without_replay(self):
        fake = FakeLauncher(self.plan, outcome="exit_error")
        with self.assertRaises(GenericCanaryError):
            self._run(fake)
        self.assertEqual(fake.calls, 1)
        self.assertTrue(fake.saw_reservation)
        self.assertEqual(self._ledger()[-1]["outcome"], "failed")
        self.assertTrue((self.output_root / self.plan["config"]["experiment_id"] /
                         "failure_diagnostics.json").exists())


if __name__ == "__main__":
    unittest.main()
