"""Offline-only sample/hold fixture and waveform evaluator; no simulator imports."""

from __future__ import annotations

import hashlib
import json
import math
import operator
from dataclasses import asdict, dataclass, field
from typing import Mapping, Protocol, Sequence

METRICS = ("track_end_error_v", "sampling_error_v",
           "hold_isolation_error_v", "hold_droop_v_per_s")
DEFAULT_ACTIVE = METRICS[:3]
LEVEL_SEQUENCE = ("LOW", "MID", "HIGH", "MID", "LOW", "HIGH")
COMPARATORS = {"<": operator.lt, "<=": operator.le, ">": operator.gt,
               ">=": operator.ge, "==": operator.eq}


class FixtureError(ValueError):
    pass


def _finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise FixtureError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class CircuitMapping:
    circuit_id: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    clock: str
    track_clock_level: str | None
    source_topology_sha256: str
    diagnostic_nodes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.circuit_id or not self.clock or len(self.inputs) not in (1, 2):
            raise FixtureError("explicit circuit, clock and one/two inputs required")
        if len(self.outputs) != len(self.inputs) or not all(self.inputs + self.outputs):
            raise FixtureError("external input/output mapping must be single-ended or differential")
        if self.track_clock_level not in ("HIGH", "LOW"):
            raise FixtureError("clock phase polarity must be explicitly resolved")
        if len(self.source_topology_sha256) != 64:
            raise FixtureError("source topology SHA-256 required")


@dataclass(frozen=True)
class OperatingConditions:
    vin_range_v: tuple[float, float]
    clock_period_s: float
    clock_low_v: float
    clock_high_v: float
    clock_duty: float
    clock_rise_s: float | None
    clock_fall_s: float | None
    provenance: Mapping[str, str]
    fixed_common_mode_v: float | None = None
    vin_edge_s: float = 0.0

    def __post_init__(self) -> None:
        low, high = (_finite(v, "VIN range") for v in self.vin_range_v)
        if high - low <= 1e-9:
            raise FixtureError("VIN range must have distinguishable levels")
        period = _finite(self.clock_period_s, "clock period")
        duty = _finite(self.clock_duty, "clock duty")
        if period <= 0 or not 0 < duty < 1:
            raise FixtureError("clock period/duty invalid")
        if _finite(self.clock_high_v, "clock high") <= _finite(self.clock_low_v, "clock low"):
            raise FixtureError("clock levels invalid")
        for name in ("clock_rise_s", "clock_fall_s"):
            edge = getattr(self, name)
            if edge is not None and (_finite(edge, name) < 0 or edge >= period):
                raise FixtureError(f"{name} invalid")
        if _finite(self.vin_edge_s, "VIN edge") < 0:
            raise FixtureError("VIN edge must be nonnegative")
        if not self.provenance:
            raise FixtureError("condition provenance required")


def resolve_conditions(explicit: Mapping[str, object],
                       recovered: Mapping[str, object],
                       defaults: Mapping[str, object] | None = None) -> tuple[dict, dict]:
    """Resolve only supplied fields: explicit > recovered > documented defaults."""
    values, provenance = {}, {}
    for source, mapping in (("documented_default", defaults or {}),
                            ("source_or_configuration", recovered), ("explicit", explicit)):
        for key, value in mapping.items():
            if value is not None:
                values[key], provenance[key] = value, source
    return values, provenance


@dataclass(frozen=True)
class CycleFixture:
    level: str
    intended_v: float
    track_initial_v: float
    disturbed_v: float
    track_pre_s: float
    track_step_s: float
    track_end_s: float
    switch_off_s: float
    early_hold_s: float
    quiet_start_s: float
    quiet_end_s: float
    disturbance_s: float
    post_disturbance_s: float
    hold_end_s: float


@dataclass(frozen=True)
class SampleHoldFixture:
    version: str
    mapping: CircuitMapping
    conditions: OperatingConditions
    startup_cycles: int
    cycles: tuple[CycleFixture, ...]
    guard_time_s: float
    max_timestep_s: float
    fixture_hash: str
    input_scenarios: tuple[Mapping[str, float], ...]


def build_fixture(mapping: CircuitMapping, conditions: OperatingConditions, *,
                  startup_cycles: int = 2, guard_time_s: float = 5e-9,
                  measurement_cycles: int = 6) -> SampleHoldFixture:
    if type(startup_cycles) is not int or startup_cycles < 0 or measurement_cycles != 6:
        raise FixtureError("V1 requires six measurement cycles and nonnegative fixed startup")
    guard = _finite(guard_time_s, "guard")
    if guard <= 0:
        raise FixtureError("guard must be positive")
    edges = [float(edge) for edge in (conditions.clock_rise_s, conditions.clock_fall_s)
             if edge is not None and edge > 0]
    if not edges:
        raise FixtureError("finite configured edge required for V1 timestep")
    step = min(conditions.clock_period_s / 100, min(edges) / 5)
    if not math.isfinite(step) or step <= 0:
        raise FixtureError("unusable maximum timestep")
    period = conditions.clock_period_s
    track_duration = period * (conditions.clock_duty if mapping.track_clock_level == "HIGH"
                               else 1 - conditions.clock_duty)
    hold_duration = period - track_duration
    longest_edge = max(edge or 0 for edge in
                       (conditions.clock_rise_s, conditions.clock_fall_s))
    if min(track_duration, hold_duration) <= 8 * guard + 2 * longest_edge:
        raise FixtureError("guard and transitions do not fit track/hold phases")
    low, high = conditions.vin_range_v
    mid = (low + high) / 2
    if len(mapping.inputs) == 2 and conditions.fixed_common_mode_v is None:
        raise FixtureError("differential fixture requires explicit common mode")
    levels = {"LOW": low, "MID": mid, "HIGH": high}
    delta = (high - low) / 4
    cycles = []
    scenarios = []
    for index, label in enumerate(LEVEL_SEQUENCE):
        start = (startup_cycles + index) * period
        intended = levels[label]
        inward = intended + delta if intended <= mid else intended - delta
        track_initial = inward
        disturbed = inward
        track_pre = start + longest_edge + 2 * guard
        track_step = start + track_duration * 0.30
        track_end = start + track_duration - 2 * guard
        switch_off = start + track_duration
        early = switch_off + longest_edge + 2 * guard
        quiet_start = switch_off + hold_duration * 0.27
        quiet_end = switch_off + hold_duration * 0.52
        disturb = switch_off + hold_duration * 0.67
        post = switch_off + hold_duration * 0.84
        hold_end = start + period
        if not (track_pre + guard < track_step < track_end - guard < switch_off
                < early + guard < quiet_start < quiet_end + guard < disturb
                < disturb + guard < post < hold_end - guard):
            raise FixtureError("measurement windows do not fit phases")
        if (track_step + conditions.vin_edge_s + guard >= track_end or
                disturb + conditions.vin_edge_s + guard >= post or
                conditions.vin_edge_s + guard >= track_pre - start):
            raise FixtureError("finite VIN edge and guard do not fit measurement windows")
        if not low <= min(intended, track_initial, disturbed) <= max(
                intended, track_initial, disturbed) <= high:
            raise FixtureError("input stimulus outside working range")
        cycles.append(CycleFixture(label, intended, track_initial, disturbed,
                                   track_pre, track_step, track_end, switch_off,
                                   early, quiet_start, quiet_end, disturb, post, hold_end))
        if len(mapping.inputs) == 1:
            scenarios.append({"input": intended, "track_initial": track_initial,
                              "hold_disturbed": disturbed})
        else:
            cm = _finite(conditions.fixed_common_mode_v, "common mode")
            scenarios.append({"input_plus": cm + intended / 2,
                              "input_minus": cm - intended / 2,
                              "track_initial_plus": cm + track_initial / 2,
                              "track_initial_minus": cm - track_initial / 2,
                              "hold_disturbed_plus": cm + disturbed / 2,
                              "hold_disturbed_minus": cm - disturbed / 2})
    condition_payload = asdict(conditions)
    if conditions.vin_edge_s == 0:
        condition_payload.pop("vin_edge_s")  # Preserve SC-4A fixture identities.
    payload = {"version": "sample_hold_fixture_v1", "mapping": asdict(mapping),
               "conditions": condition_payload, "startup_cycles": startup_cycles,
               "cycles": [asdict(c) for c in cycles], "guard_time_s": guard,
               "max_timestep_s": step, "input_scenarios": scenarios}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True,
                               separators=(",", ":")).encode()).hexdigest()
    return SampleHoldFixture("sample_hold_fixture_v1", mapping, conditions,
                             startup_cycles, tuple(cycles), guard, step,
                             fingerprint, tuple(scenarios))


class WaveformAdapter(Protocol):
    """Future adapter contract only; this module never calls native simulation."""
    def get_waveforms(self, fixture: SampleHoldFixture) -> Mapping[str, Sequence[float]]: ...


def _sample(time: Sequence[float], values: Sequence[float], at: float,
            max_gap: float) -> float:
    import bisect
    index = bisect.bisect_left(time, at)
    if index == len(time) or index == 0:
        if index == 0 and time[0] == at:
            return values[0]
        raise FixtureError("trace does not cover required event")
    if time[index] == at:
        return values[index]
    before, after = time[index - 1], time[index]
    if after - before > max_gap:
        raise FixtureError("unresolved waveform gap at measurement event")
    return values[index - 1] + (values[index] - values[index - 1]) * (at - before) / (after - before)


def _fit(times: Sequence[float], values: Sequence[float]) -> tuple[float, float]:
    center = sum(times) / len(times)
    voltage = sum(values) / len(values)
    denom = sum((t - center) ** 2 for t in times)
    if denom <= 0:
        raise FixtureError("quiet Hold fit has zero time span")
    slope = sum((t - center) * (v - voltage) for t, v in zip(times, values)) / denom
    return voltage - slope * center, slope


def _check_targets(metrics: Mapping[str, float], specs: Mapping[str, object] | None) -> bool | None:
    """Mirror the simple target operator/value contract without D0 package imports."""
    if not specs:
        return None
    for name, spec in specs.items():
        if not isinstance(spec, Mapping) or spec.get("operator") not in COMPARATORS:
            raise ValueError("invalid target operator/value contract")
        target = _finite(spec.get("value"), "target value")
        if not COMPARATORS[spec["operator"]](metrics[name], target):
            return False
    return True


@dataclass(frozen=True)
class SampleHoldResult:
    metrics: Mapping[str, float | None]
    metric_status: Mapping[str, str]
    diagnostics: Mapping[str, object]
    parameter_valid: bool
    simulation_valid: bool
    measurement_valid: bool
    functional_valid: bool
    meets_spec: bool | None
    failure_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_waveforms(fixture: SampleHoldFixture,
                       waveforms: Mapping[str, Sequence[float]] | None, *,
                       target_specs: Mapping[str, object] | None = None,
                       active_metrics: Sequence[str] = DEFAULT_ACTIVE,
                       simulation_valid: bool = True,
                       parameter_valid: bool = True,
                       synthetic: bool = True,
                       minimum_track_response_ratio: float = 0.02,
                       maximum_hold_to_track_ratio: float = 0.8,
                       noise_floor_v: float = 0.0) -> SampleHoldResult:
    """Evaluate supplied arrays only. No adapter, native simulation or AL call."""
    if len(set(active_metrics)) != len(active_metrics) or set(active_metrics) - set(METRICS):
        raise ValueError("invalid active metric selection")
    if target_specs is not None and set(target_specs) - set(active_metrics):
        raise ValueError("target must name an active performance metric")
    if (_finite(minimum_track_response_ratio, "minimum track ratio") <= 0 or
            _finite(maximum_hold_to_track_ratio, "maximum hold ratio") <= 0 or
            _finite(noise_floor_v, "noise floor") < 0):
        raise ValueError("functional detection tolerances invalid")
    empty = {name: None for name in METRICS}
    statuses = {name: "not_evaluated" for name in METRICS}
    base = {"fixture_version": fixture.version, "fixture_hash": fixture.fixture_hash,
            "circuit_id": fixture.mapping.circuit_id, "synthetic": bool(synthetic),
            "native_simulator_attempts": 0 if synthetic else None,
            "active_metrics": list(active_metrics),
            "condition_provenance": dict(fixture.conditions.provenance)}

    def fail(reason: str, *, measured: bool = False,
             functional: bool = False, diagnostic: Mapping | None = None) -> SampleHoldResult:
        return SampleHoldResult(empty, statuses, {**base, **dict(diagnostic or {})},
                                parameter_valid, simulation_valid, measured,
                                functional, None, (reason,))

    if not parameter_valid:
        return fail("parameter_invalid")
    if not simulation_valid:
        return fail("simulation_invalid")
    if waveforms is None:
        return fail("simulation_invalid:missing_waveforms")
    required = ("time", fixture.mapping.clock, *fixture.mapping.inputs, *fixture.mapping.outputs)
    if any(name not in waveforms for name in required):
        return fail("measurement_invalid:missing_required_trace")
    try:
        time = [float(v) for v in waveforms["time"]]
        traces = {name: [float(v) for v in waveforms[name]] for name in required[1:]}
        if len(time) < 3 or any(len(v) != len(time) for v in traces.values()):
            raise FixtureError("trace length mismatch")
        if any(not math.isfinite(v) for v in time) or any(
                not math.isfinite(v) for trace in traces.values() for v in trace):
            raise FixtureError("nonfinite waveform")
        if any(b <= a for a, b in zip(time, time[1:])):
            raise FixtureError("nonmonotonic time")
        if time[0] > fixture.cycles[0].track_pre_s or time[-1] < fixture.cycles[-1].post_disturbance_s:
            raise FixtureError("incomplete six-cycle trace")
        input_signal = [sum(sign * traces[name][i] for sign, name in
                        zip((1, -1), fixture.mapping.inputs)) for i in range(len(time))]
        output_signal = [sum(sign * traces[name][i] for sign, name in
                         zip((1, -1), fixture.mapping.outputs)) for i in range(len(time))]
        clock = traces[fixture.mapping.clock]
        clock_mid = (fixture.conditions.clock_low_v + fixture.conditions.clock_high_v) / 2
        sampled = {name: [] for name in METRICS}
        per_cycle, track_ratios, hold_ratios, noise_ratios = [], [], [], []
        for cycle in fixture.cycles:
            sample = lambda values, at: _sample(time, values, at, 4 * fixture.max_timestep_s)
            track_level = fixture.conditions.clock_high_v if fixture.mapping.track_clock_level == "HIGH" else fixture.conditions.clock_low_v
            hold_level = fixture.conditions.clock_low_v if fixture.mapping.track_clock_level == "HIGH" else fixture.conditions.clock_high_v
            if abs(sample(clock, cycle.track_end_s) - track_level) > abs(track_level - clock_mid) * .5 or (
                abs(sample(clock, cycle.track_pre_s) - track_level) > abs(track_level - clock_mid) * .5 or
                abs(sample(clock, cycle.early_hold_s) - hold_level) > abs(hold_level - clock_mid) * .5 or
                abs(sample(clock, cycle.post_disturbance_s) - hold_level) > abs(hold_level - clock_mid) * .5):
                raise FixtureError("clock phase contradicts configured track/hold mapping")
            if not any(cycle.track_end_s <= a <= cycle.early_hold_s and
                       min(x, y) <= clock_mid <= max(x, y) and x != y
                       for a, x, y in zip(time, clock, clock[1:])):
                raise FixtureError("missing switch-off clock transition")
            before_in = sample(input_signal, cycle.track_pre_s)
            before_step = sample(input_signal, cycle.track_step_s - fixture.guard_time_s)
            after_step = sample(input_signal, cycle.track_step_s + fixture.conditions.vin_edge_s + fixture.guard_time_s)
            after_in = sample(input_signal, cycle.track_end_s)
            hold_in_before = sample(input_signal, cycle.quiet_end_s)
            before_disturbance = sample(input_signal, cycle.disturbance_s - fixture.guard_time_s)
            after_disturbance = sample(input_signal, cycle.disturbance_s + fixture.conditions.vin_edge_s + fixture.guard_time_s)
            hold_in_after = sample(input_signal, cycle.post_disturbance_s)
            scale = abs(after_in - before_in)
            if (scale < (fixture.conditions.vin_range_v[1] - fixture.conditions.vin_range_v[0]) * .05
                    or abs(hold_in_after - hold_in_before) < scale * .1):
                raise FixtureError("missing Track step or Hold perturbation")
            if (abs(after_step - before_step) < scale * .5 or
                    abs(after_disturbance - before_disturbance) < scale * .1 or
                    abs(hold_in_before - after_in) > scale * .1):
                raise FixtureError("stimulus event missing or not in prescribed phase")
            if abs(after_in - cycle.intended_v) > 0.1 * scale:
                raise FixtureError("incorrect input level")
            if any(v < fixture.conditions.vin_range_v[0] - 1e-9 or
                   v > fixture.conditions.vin_range_v[1] + 1e-9 for v, t in
                   zip(input_signal, time) if cycle.track_pre_s <= t <= cycle.post_disturbance_s):
                raise FixtureError("input exceeded declared working range")
            out_pre = sample(output_signal, cycle.track_pre_s)
            out_track = sample(output_signal, cycle.track_end_s)
            out_early = sample(output_signal, cycle.early_hold_s)
            quiet = [(t, v) for t, v in zip(time, output_signal)
                     if cycle.quiet_start_s <= t <= cycle.quiet_end_s]
            if len(quiet) < 3:
                raise FixtureError("insufficient quiet Hold samples")
            if any(b[0] - a[0] > 4 * fixture.max_timestep_s
                   for a, b in zip(quiet, quiet[1:])):
                raise FixtureError("unresolved waveform gap in quiet Hold")
            intercept, slope = _fit([v[0] for v in quiet], [v[1] for v in quiet])
            noise = max(noise_floor_v, max(abs(v - (intercept + slope * t))
                                           for t, v in quiet))
            post_out = sample(output_signal, cycle.post_disturbance_s)
            isolation = abs(post_out - (intercept + slope * cycle.post_disturbance_s))
            values = {"track_end_error_v": abs(out_track - after_in),
                      "sampling_error_v": abs(out_early - after_in),
                      "hold_isolation_error_v": isolation,
                      "hold_droop_v_per_s": abs(slope)}
            for key in METRICS:
                sampled[key].append(values[key])
            track_ratio = abs(out_track - out_pre) / scale
            hold_ratio = isolation / abs(hold_in_after - hold_in_before)
            track_ratios.append(track_ratio)
            hold_ratios.append(hold_ratio)
            noise_ratios.append(3 * noise / scale)
            per_cycle.append({"level": cycle.level, "metrics": values,
                              "track_response_ratio": track_ratio,
                              "hold_response_ratio": hold_ratio,
                              "quiet_noise_estimate_v": noise})
        if any(not math.isfinite(v) for values in sampled.values() for v in values):
            raise FixtureError("nonfinite metric")
    except (FixtureError, ValueError, TypeError, IndexError) as error:
        return fail(f"measurement_invalid:{error}")

    metrics = {key: sum(values) / 6 for key, values in sampled.items()}
    aggregated = {key: {"six_cycle_values": values, "mean": metrics[key],
                        "max": max(values),
                        "per_level": {label: {"mean": sum(values[i] for i, cycle in
                                      enumerate(fixture.cycles) if cycle.level == label) / 2,
                                      "max": max(values[i] for i, cycle in
                                      enumerate(fixture.cycles) if cycle.level == label)}
                                      for label in ("LOW", "MID", "HIGH")}}
                  for key, values in sampled.items()}
    functional = all(v >= max(minimum_track_response_ratio, n)
                     for v, n in zip(track_ratios, noise_ratios)) and all(
        h <= max(maximum_hold_to_track_ratio * t, minimum_track_response_ratio)
        for h, t in zip(hold_ratios, track_ratios))
    reasons = () if functional else ("functional_invalid:track_response_or_hold_isolation",)
    meets = _check_targets(metrics, target_specs) if functional else False if target_specs else None
    return SampleHoldResult(metrics, {key: "measured" for key in METRICS},
                            {**base, "aggregation": aggregated, "cycles": per_cycle,
                             "track_response_ratio": track_ratios,
                             "hold_response_ratio": hold_ratios,
                             "quiet_noise_to_step_ratio_x3": noise_ratios,
                             "measurement_validity": "valid"},
                            True, True, True, functional, meets, reasons)
