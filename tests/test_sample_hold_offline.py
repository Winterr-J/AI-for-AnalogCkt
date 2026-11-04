"""Synthetic-only SC-4A tests. No simulator or AL imports."""

import hashlib
import json
import math
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from ActiveLearning.sample_hold_offline import (
    CircuitMapping, FixtureError, OperatingConditions, build_fixture,
    evaluate_waveforms, resolve_conditions,
)


SOURCE_SHA = hashlib.sha256(b"static source").hexdigest()


def fixture(*, differential=False, circuit_id="circuit_0259", **kwargs):
    mapping = CircuitMapping(circuit_id, ("VIN1", "VIN2") if differential else ("VIN1",),
                             ("VOUT1", "VOUT2") if differential else ("VOUT1",),
                             "VCLK1", "HIGH", SOURCE_SHA)
    conditions = OperatingConditions((0.2, 1.2), 1e-6, 0, 3.3, 0.4, 10e-9, 10e-9,
                                     {"vin_range_v": "explicit", "clock": "archived fixture"},
                                     fixed_common_mode_v=1.65)
    return build_fixture(mapping, conditions, **kwargs)


def traces(fx, *, offset=0.0, early_offset=0.0, drift=0.0, leak=0.0,
           stuck=None, continuous=False, no_track=False):
    period = fx.conditions.clock_period_s
    count = int((fx.startup_cycles + 6) * period / 2e-9) + 1
    time = [i * 2e-9 for i in range(count)]
    vin, vout, clk = [], [], []
    for t in time:
        phase = (t / period) % 1
        track_fraction = (fx.conditions.clock_duty if fx.mapping.track_clock_level == "HIGH"
                          else 1 - fx.conditions.clock_duty)
        clk.append((3.3 if fx.mapping.track_clock_level == "HIGH" else 0.0)
                   if phase < track_fraction else
                   (0.0 if fx.mapping.track_clock_level == "HIGH" else 3.3))
        index = int(t / period) - fx.startup_cycles
        if index < 0:
            value, out = 0.7, 0.7
        else:
            index = min(index, 5)
            cycle = fx.cycles[index]
            value = cycle.track_initial_v if t < cycle.track_step_s else cycle.intended_v
            if t >= cycle.disturbance_s:
                value = cycle.disturbed_v
            if t < cycle.track_step_s:
                out = cycle.track_initial_v + offset
            elif t < cycle.switch_off_s:
                out = cycle.track_initial_v + offset if no_track else cycle.intended_v + offset
            else:
                out = cycle.intended_v + offset + early_offset + drift * (t - cycle.switch_off_s)
                if t >= cycle.disturbance_s:
                    out += leak * (cycle.disturbed_v - cycle.intended_v)
        if stuck is not None:
            out = stuck
        if continuous:
            out = value
        vin.append(value)
        vout.append(out)
    if len(fx.mapping.inputs) == 1:
        return {"time": time, "VIN1": vin, "VOUT1": vout, "VCLK1": clk}
    cm = fx.conditions.fixed_common_mode_v
    return {"time": time, "VIN1": [cm + v / 2 for v in vin],
            "VIN2": [cm - v / 2 for v in vin],
            "VOUT1": [cm + v / 2 for v in vout],
            "VOUT2": [cm - v / 2 for v in vout], "VCLK1": clk}


class SampleHoldOfflineTests(unittest.TestCase):
    def test_fixture_reproducible_six_levels_and_step(self):
        fx = fixture()
        self.assertEqual(fx.fixture_hash, fixture().fixture_hash)
        self.assertEqual([c.level for c in fx.cycles],
                         ["LOW", "MID", "HIGH", "MID", "LOW", "HIGH"])
        self.assertEqual(fx.startup_cycles, 2)
        self.assertEqual(fx.max_timestep_s, 2e-9)
        self.assertTrue(all(c.track_initial_v != c.intended_v and
                            c.disturbed_v != c.intended_v for c in fx.cycles))
        self.assertEqual(len(fx.input_scenarios), 6)

    def test_ideal_and_aggregation(self):
        result = evaluate_waveforms(fixture(), traces(fixture()))
        self.assertTrue(result.functional_valid)
        self.assertTrue(result.measurement_valid)
        self.assertIsNone(result.meets_spec)
        self.assertAlmostEqual(result.metrics["sampling_error_v"], 0, places=8)
        self.assertEqual(len(result.diagnostics["cycles"]), 6)
        for name in result.metrics:
            agg = result.diagnostics["aggregation"][name]
            self.assertEqual(len(agg["six_cycle_values"]), 6)
            self.assertEqual(set(agg["per_level"]), {"LOW", "MID", "HIGH"})
            self.assertAlmostEqual(agg["mean"], result.metrics[name])
            self.assertAlmostEqual(agg["max"], max(agg["six_cycle_values"]))
        self.assertEqual(result.diagnostics["native_simulator_attempts"], 0)
        self.assertTrue(result.diagnostics["synthetic"])

    def test_imprecise_but_functional_and_targets(self):
        fx = fixture()
        result = evaluate_waveforms(
            fx, traces(fx, offset=0.17),
            target_specs={"sampling_error_v": {"operator": "<=", "value": 0.02}})
        self.assertTrue(result.functional_valid)
        self.assertFalse(result.meets_spec)
        self.assertAlmostEqual(result.metrics["sampling_error_v"], 0.17, places=6)
        good = evaluate_waveforms(
            fx, traces(fx), target_specs={"sampling_error_v": {"operator": "<=", "value": 0.02}})
        self.assertTrue(good.meets_spec)

    def test_track_error_vs_sampling_error(self):
        fx = fixture()
        result = evaluate_waveforms(fx, traces(fx, offset=0.1, early_offset=0.03))
        self.assertTrue(result.functional_valid)
        self.assertAlmostEqual(result.metrics["track_end_error_v"], 0.1, places=6)
        self.assertAlmostEqual(result.metrics["sampling_error_v"], 0.13, places=5)

    def test_drift_baseline_removes_linear_slope(self):
        fx = fixture()
        result = evaluate_waveforms(fx, traces(fx, drift=20000))
        self.assertTrue(result.functional_valid)
        self.assertAlmostEqual(result.metrics["hold_droop_v_per_s"], 20000, delta=1)
        self.assertLess(result.metrics["hold_isolation_error_v"], 1e-6)
        self.assertNotIn("hold_droop_v_per_s", result.diagnostics["active_metrics"])

    def test_hold_disturbance_with_drift(self):
        fx = fixture()
        result = evaluate_waveforms(fx, traces(fx, drift=20000, leak=0.2))
        self.assertTrue(result.functional_valid)
        self.assertAlmostEqual(result.metrics["hold_isolation_error_v"], 0.05, places=5)

    def test_provisional_noise_floor_is_not_a_target(self):
        fx = fixture()
        result = evaluate_waveforms(fx, traces(fx), noise_floor_v=0.2)
        self.assertTrue(result.measurement_valid)
        self.assertFalse(result.functional_valid)
        self.assertIsNone(result.meets_spec)

    def test_differential_and_circuit_id_independence(self):
        fx = fixture(differential=True)
        result = evaluate_waveforms(fx, traces(fx, offset=0.04))
        self.assertTrue(result.functional_valid)
        self.assertAlmostEqual(result.metrics["sampling_error_v"], 0.04, places=6)
        altered = fixture(differential=True, circuit_id="another_valid_topology")
        other = evaluate_waveforms(altered, traces(altered, offset=0.04))
        self.assertEqual(result.metrics, other.metrics)

    def test_low_clock_track_phase(self):
        high = fixture()
        low = build_fixture(replace(high.mapping, track_clock_level="LOW"),
                            high.conditions)
        result = evaluate_waveforms(low, traces(low))
        self.assertTrue(result.functional_valid)

    def test_stuck_or_no_track_fails_function(self):
        fx = fixture()
        for kw in ({"stuck": 0.0}, {"stuck": 2.4}, {"no_track": True},
                   {"continuous": True}):
            result = evaluate_waveforms(fx, traces(fx, **kw))
            self.assertTrue(result.measurement_valid, kw)
            self.assertFalse(result.functional_valid, kw)

    def test_clock_missing_or_wrong_phase(self):
        fx = fixture()
        wave = traces(fx)
        wave["VCLK1"] = [3.3] * len(wave["time"])
        self.assertFalse(evaluate_waveforms(fx, wave).measurement_valid)
        wrong = replace(fx, mapping=replace(fx.mapping, track_clock_level="LOW"))
        self.assertFalse(evaluate_waveforms(wrong, traces(fx)).measurement_valid)

    def test_missing_output_and_nonfinite_time(self):
        fx = fixture()
        wave = traces(fx)
        del wave["VOUT1"]
        self.assertFalse(evaluate_waveforms(fx, wave).measurement_valid)
        for mutation in ("nonmonotonic", "nan", "infinity"):
            wave = traces(fx)
            if mutation == "nonmonotonic":
                wave["time"][100] = wave["time"][99]
            elif mutation == "nan":
                wave["VIN1"][100] = math.nan
            else:
                wave["VOUT1"][100] = math.inf
            self.assertFalse(evaluate_waveforms(fx, wave).measurement_valid)

    def test_incomplete_cycle_gap_and_no_disturbance(self):
        fx = fixture()
        wave = traces(fx)
        for key in wave:
            wave[key] = wave[key][:-200]
        self.assertFalse(evaluate_waveforms(fx, wave).measurement_valid)
        wave = traces(fx)
        cycle = fx.cycles[1]
        wave["VIN1"] = [cycle.intended_v if cycle.disturbance_s <= t < cycle.hold_end_s
                        else v for t, v in zip(wave["time"], wave["VIN1"])]
        self.assertFalse(evaluate_waveforms(fx, wave).measurement_valid)
        wave = traces(fx)
        keep = [not (cycle.quiet_start_s <= t <= cycle.quiet_end_s) for t in wave["time"]]
        wave = {key: [v for v, allowed in zip(values, keep) if allowed]
                for key, values in wave.items()}
        self.assertFalse(evaluate_waveforms(fx, wave).measurement_valid)
        wave = traces(fx)
        gap_start = cycle.quiet_start_s + 0.08 * (cycle.quiet_end_s - cycle.quiet_start_s)
        gap_end = cycle.quiet_end_s - 0.08 * (cycle.quiet_end_s - cycle.quiet_start_s)
        keep = [not (gap_start < t < gap_end) for t in wave["time"]]
        wave = {key: [v for v, allowed in zip(values, keep) if allowed]
                for key, values in wave.items()}
        self.assertFalse(evaluate_waveforms(fx, wave).measurement_valid)

    def test_configuration_validation(self):
        fx = fixture()
        with self.assertRaises(FixtureError):
            build_fixture(fx.mapping, replace(fx.conditions, vin_range_v=(1, 1)))
        with self.assertRaises(FixtureError):
            build_fixture(fx.mapping, fx.conditions, guard_time_s=2e-7)
        with self.assertRaises(FixtureError):
            build_fixture(fx.mapping, replace(fx.conditions, clock_rise_s=0,
                                                 clock_fall_s=None))
        with self.assertRaises(FixtureError):
            build_fixture(replace(fx.mapping, track_clock_level=None), fx.conditions)

    def test_statuses_and_target_participation(self):
        fx = fixture()
        self.assertFalse(evaluate_waveforms(fx, None, simulation_valid=False).simulation_valid)
        self.assertFalse(evaluate_waveforms(fx, None, parameter_valid=False).parameter_valid)
        self.assertIsNone(evaluate_waveforms(fx, traces(fx)).meets_spec)
        with self.assertRaises(ValueError):
            evaluate_waveforms(fx, traces(fx),
                               target_specs={"hold_droop_v_per_s": {"operator": "<=", "value": 1}})
        included = evaluate_waveforms(fx, traces(fx),
                                      active_metrics=("sampling_error_v", "hold_droop_v_per_s"),
                                      target_specs={"hold_droop_v_per_s": {"operator": "<=", "value": 1}})
        self.assertTrue(included.meets_spec)

    def test_precedence(self):
        values, provenance = resolve_conditions(
            {"clock_period_s": 1e-6}, {"clock_period_s": 2e-6, "clock_high_v": 3.3},
            {"clock_period_s": 3e-6, "clock_low_v": 0})
        self.assertEqual(values["clock_period_s"], 1e-6)
        self.assertEqual(provenance["clock_high_v"], "source_or_configuration")
        self.assertEqual(provenance["clock_low_v"], "documented_default")

    def test_static_representative_mappings_match_sources(self):
        root = Path(__file__).resolve().parents[1]
        file = root / "ActiveLearning/docs/reference_audit/sample_hold_v1_mappings_sc4a.json"
        entries = json.loads(file.read_text(encoding="utf-8"))["circuits"]
        self.assertEqual(set(entries), {"circuit_0259", "circuit_0605", "circuit_0263"})
        for entry in entries.values():
            self.assertEqual(hashlib.sha256((root / entry["source"]).read_bytes()).hexdigest(),
                             entry["source_sha256"])
            self.assertIsNone(entry.get("vin_range_v", entry.get("vin_diff_range_v")))
        self.assertEqual(entries["circuit_0605"]["storage_nodes"], ["net3"])
        self.assertEqual(entries["circuit_0263"]["inputs"], ["VIN1", "VIN2"])

    def test_no_simulator_import(self):
        # Other test modules may legitimately import PySpice in this pytest
        # process. Check this module's import side effect in a fresh process.
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import ActiveLearning.sample_hold_offline; "
             "assert not any(name.startswith('PySpice') for name in sys.modules)"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
