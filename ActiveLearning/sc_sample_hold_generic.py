"""Configuration-driven E1 sample/hold preparation; importing never loads SPICE."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from ActiveLearning.sample_hold_offline import (
    CircuitMapping, OperatingConditions, SampleHoldFixture, build_fixture,
    evaluate_waveforms,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "sc_sample_hold_generic_candidate_sc4c3_v1"


class GenericCanaryError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _asset(asset: Mapping[str, str]) -> Path:
    if set(asset) != {"path", "sha256"} or len(asset["sha256"]) != 64:
        raise GenericCanaryError("malformed source asset")
    path = (ROOT / asset["path"]).resolve(strict=True)
    if not path.is_relative_to(ROOT) or not path.is_file() or sha256_file(path) != asset["sha256"]:
        raise GenericCanaryError("source asset path or raw hash mismatch")
    return path


def _finite(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise GenericCanaryError(f"{name} must be finite")
    return float(value)


def _topology_contract(config: dict, paths: dict[str, Path]) -> None:
    """Check the declared topology and converted constructor without importing it."""
    lines = paths["topology"].read_text(encoding="utf-8").splitlines()
    if lines != config["original_netlist_lines"] or len(lines) != len(set(lines)):
        raise GenericCanaryError("topology differs from declared device inventory")
    mapping, assembly = config["mapping"], config["assembly"]
    inputs, outputs = mapping["inputs"], mapping["outputs"]
    clocks, supplies = [mapping["clock"]], list(assembly["supply_sources"])
    sources = inputs + clocks + supplies
    if (len(inputs) not in (1, 2) or len(outputs) != len(inputs)
            or len(set(sources)) != len(sources) or set(outputs) & set(sources)
            or "VSS" in sources or len(set(outputs)) != len(outputs)):
        raise GenericCanaryError("missing, duplicate or conflicting physical ports/sources")
    ports = " ".join(lines)
    if any(f"{name} " not in ports and f"{name})" not in ports
           for name in (*inputs, *outputs, *clocks, *supplies, *mapping["diagnostic_nodes"])):
        raise GenericCanaryError("mapped port absent from original topology")
    loads = []
    for line in lines:
        parts = line.split()
        if len(parts) == 4 and parts[0].startswith("R") and parts[-1] == "resistor":
            loads.append(f"{parts[0]}:{parts[1].lstrip('(')}-{parts[2].rstrip(')')}")
    if assembly.get("external_load_connections") != loads:
        raise GenericCanaryError("external resistor load mapping differs from topology")
    tree = ast.parse(paths["converted"].read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "build_circuit"]
    if len(functions) != 1:
        raise GenericCanaryError("converted source has no unique build_circuit")
    required = {arg.arg for arg in functions[0].args.kwonlyargs}
    args = assembly["constructor_arguments"]
    if set(args) != required or any(not isinstance(value, dict) or
                                    value.get("kind") not in {"input", "clock", "fixed"} or
                                    set(value) != ({"kind", "name"} if value.get("kind") in
                                                   {"input", "clock"} else {"kind", "value"})
                                    for value in args.values()):
        raise GenericCanaryError("converted constructor arguments do not match explicit assembly")
    if ({value["name"] for value in args.values() if value["kind"] == "input"} != set(inputs)
            or {value["name"] for value in args.values() if value["kind"] == "clock"} != set(clocks)):
        raise GenericCanaryError("physical voltage source assembly mismatch")
    if any(name not in args or args[name] != {"kind": "fixed", "value": voltage}
           for name, voltage in assembly["supply_sources"].items()):
        raise GenericCanaryError("supply source assembly differs from topology")
    if any(spec["kind"] == "fixed" and not (
            isinstance(spec["value"], str) or
            type(spec["value"]) in (int, float) and math.isfinite(spec["value"]))
           for spec in args.values()):
        raise GenericCanaryError("nonfinite or unsupported fixed constructor value")
    if not isinstance(assembly["model"], dict) or set(assembly["model"]) != {
            "LEVEL", "VTO", "KP", "LAMBDA"}:
        raise GenericCanaryError("missing explicit Level-1 MOS model")
    try:
        import yaml
    except ImportError as error:
        raise GenericCanaryError("PyYAML required for parameter provenance") from error
    archive = yaml.safe_load(paths["ideal_parameters"].read_text(encoding="utf-8"))
    bindings = config["parameter_bindings"]
    fixed = {name for name, spec in args.items() if spec["kind"] == "fixed"}
    if set(bindings) != fixed | {"model.LEVEL", "model.VTO", "model.KP", "model.LAMBDA"}:
        raise GenericCanaryError("not every fixed model/device value has source provenance")
    for name, assertion in bindings.items():
        if set(assertion) != {"path", "scale"} or not assertion["path"]:
            raise GenericCanaryError("malformed parameter provenance binding")
        actual = archive
        for key in assertion["path"]:
            if not isinstance(actual, dict) or key not in actual:
                raise GenericCanaryError("nominal parameter path missing from archive")
            actual = actual[key]
        expected = (assembly["model"][name.removeprefix("model.")]
                    if name.startswith("model.") else args[name]["value"])
        scale = assertion["scale"]
        if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
            raise GenericCanaryError("invalid nominal unit conversion")
        observed = actual * scale if type(actual) in (int, float) else actual
        if (not math.isclose(observed, expected, rel_tol=1e-12, abs_tol=0.0)
                if type(observed) in (int, float) and type(expected) in (int, float)
                else observed != expected):
            raise GenericCanaryError("nominal model/device value differs from archive")


def build_stimulus(fixture: SampleHoldFixture, *, startup_input_v: float,
                   physical_input_bounds_v: Mapping[str, list[float]]) -> dict:
    """Compile physical PWL sources; never emit only an abstract differential source."""
    c, mapping = fixture.conditions, fixture.mapping
    startup = _finite(startup_input_v, "startup input")
    if not c.vin_range_v[0] <= startup <= c.vin_range_v[1]:
        raise GenericCanaryError("startup input outside proposed range")
    if not c.clock_rise_s or not c.clock_fall_s or c.vin_edge_s <= 0:
        raise GenericCanaryError("finite clock and input edges required")
    plateau = c.clock_duty * c.clock_period_s - c.clock_rise_s
    if plateau <= 0 or plateau + c.clock_rise_s + c.clock_fall_s >= c.clock_period_s:
        raise GenericCanaryError("clock phase cannot fit its finite edges")
    if set(physical_input_bounds_v) != set(mapping.inputs):
        raise GenericCanaryError("every physical input requires a bound")
    points = [[0.0, startup]]
    previous = startup
    for cycle in fixture.cycles:
        start = cycle.hold_end_s - c.clock_period_s
        points.extend([[start, previous], [start + c.vin_edge_s, cycle.track_initial_v],
                       [cycle.track_step_s, cycle.track_initial_v],
                       [cycle.track_step_s + c.vin_edge_s, cycle.intended_v],
                       [cycle.disturbance_s, cycle.intended_v],
                       [cycle.disturbance_s + c.vin_edge_s, cycle.disturbed_v]])
        previous = cycle.disturbed_v
    end = (fixture.startup_cycles + len(fixture.cycles)) * c.clock_period_s
    points.append([end, previous])
    if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise GenericCanaryError("input PWL times are not strictly increasing")
    if not all(c.vin_range_v[0] <= value <= c.vin_range_v[1] for _, value in points):
        raise GenericCanaryError("normalized input exceeds proposed working range")
    for cycle in fixture.cycles:
        if not (cycle.track_pre_s > cycle.hold_end_s - c.clock_period_s + c.vin_edge_s
                and cycle.track_step_s + c.vin_edge_s + fixture.guard_time_s < cycle.track_end_s
                and cycle.quiet_start_s < cycle.quiet_end_s < cycle.disturbance_s
                and cycle.disturbance_s + c.vin_edge_s + fixture.guard_time_s
                < cycle.post_disturbance_s < cycle.hold_end_s - fixture.guard_time_s):
            raise GenericCanaryError("source transitions overlap V1 measurement windows")
        if (abs(cycle.intended_v - cycle.track_initial_v) <
                (c.vin_range_v[1] - c.vin_range_v[0]) * 0.1 or
                abs(cycle.disturbed_v - cycle.intended_v) <
                (c.vin_range_v[1] - c.vin_range_v[0]) * 0.1):
            raise GenericCanaryError("Track or Hold stimulus lacks meaningful step")
    if len(mapping.inputs) == 1:
        physical = {mapping.inputs[0]: points}
    else:
        cm = _finite(c.fixed_common_mode_v, "fixed common mode")
        physical = {
            mapping.inputs[0]: [[t, cm + value / 2] for t, value in points],
            mapping.inputs[1]: [[t, cm - value / 2] for t, value in points],
        }
    for name, samples in physical.items():
        low, high = map(lambda x: _finite(x, "terminal bound"), physical_input_bounds_v[name])
        if low >= high or any(not low <= value <= high for _, value in samples):
            raise GenericCanaryError(f"physical input {name} exceeds terminal bound")
        if samples[0][0] != 0 or samples[-1][0] != end:
            raise GenericCanaryError("physical input PWL has wrong endpoints")
    if len(mapping.inputs) == 2 and any(abs((a[1] + b[1]) / 2 - cm) > 1e-12
                                         for a, b in zip(*physical.values())):
        raise GenericCanaryError("differential common mode changed")
    return {"input_pwl_points_s_v": physical,
            "clock_pulse": {"source": mapping.clock, "low_v": c.clock_low_v,
                            "high_v": c.clock_high_v, "delay_s": 0.0,
                            "rise_s": c.clock_rise_s, "fall_s": c.clock_fall_s,
                            "high_plateau_s": plateau, "period_s": c.clock_period_s},
            "end_time_s": end, "max_timestep_s": fixture.max_timestep_s}


def load_candidate(path: Path) -> dict:
    """Read-only source/fixture/assembly validation; never opens a native backend."""
    path = path.resolve(strict=True)
    if not path.is_relative_to(ROOT):
        raise GenericCanaryError("candidate must reside in the repository")
    config = json.loads(path.read_text(encoding="utf-8"))
    if (config.get("schema") != SCHEMA or config.get("status") != "candidate_unapproved"
            or config.get("real_execution_authorized") is not False
            or config.get("operator_fixture_approved") is not False):
        raise GenericCanaryError("not an unapproved generic E1 candidate")
    assets = config["source_assets"]
    if (re.fullmatch(r"circuit_[0-9]{4}", config["circuit_id"]) is None
            or config["mapping"]["circuit_id"] != config["circuit_id"]
            or not config["experiment_id"].startswith("sc" + config["circuit_id"][-4:] + "_")
            or not config["experiment_id"].replace("_", "").isalnum()
            or not Path(config["output_root"]).is_absolute()
            or Path(config["output_root"]).resolve().is_relative_to(ROOT)):
        raise GenericCanaryError("invalid circuit/experiment/output-root identity")
    if set(assets) != {"topology", "converted", "ideal_parameters", "fixture_resolution", "generic_worker",
                       "native_child", "evaluator", "job_isolation", "legacy_guarded_runner",
                       "ngspice_configuration"}:
        raise GenericCanaryError("source asset set incomplete or unexpected")
    paths = {name: _asset(asset) for name, asset in assets.items()}
    _topology_contract(config, paths)
    m, c = config["mapping"], config["conditions"]
    fixture = build_fixture(
        CircuitMapping(config["circuit_id"], tuple(m["inputs"]), tuple(m["outputs"]),
                       m["clock"], m["track_clock_level"], assets["topology"]["sha256"],
                       tuple(m["diagnostic_nodes"])),
        OperatingConditions(tuple(c["vin_range_v"]), c["clock_period_s"], c["clock_low_v"],
                            c["clock_high_v"], c["clock_duty"], c["clock_rise_s"],
                            c["clock_fall_s"], c["provenance"],
                            fixed_common_mode_v=c["fixed_common_mode_v"],
                            vin_edge_s=c["vin_edge_s"]),
        startup_cycles=config["startup_cycles"], measurement_cycles=6,
        guard_time_s=config["guard_time_s"])
    if fixture.fixture_hash != config["fixture_sha256"]:
        raise GenericCanaryError("abstract V1 fixture hash mismatch")
    stimulus = build_stimulus(fixture, startup_input_v=config["startup_input_v"],
                              physical_input_bounds_v=config["physical_input_bounds_v"])
    if canonical_sha256(stimulus) != config["stimulus_sha256"]:
        raise GenericCanaryError("physical stimulus hash mismatch")
    if config["budget"] != {"native_transient_attempts_max": 1,
                            "automatic_retries": 0,
                            "single_native_attempt_timeout_s": 180,
                            "overall_execution_timeout_s": 240}:
        raise GenericCanaryError("unapproved or expanded physical budget")
    if config.get("numeric_targets") is not None:
        raise GenericCanaryError("no S0 targets approved")
    return {"config": config, "path": path, "file_sha256": sha256_file(path),
            "fixture": fixture, "stimulus": stimulus, "paths": paths}


def physical_waveforms(analysis: Any, fixture: SampleHoldFixture) -> dict:
    """Extract same-axis physical nodes; no differential synthesis from fake nodes."""
    required = (fixture.mapping.clock, *fixture.mapping.inputs, *fixture.mapping.outputs)
    optional = fixture.mapping.diagnostic_nodes
    nodes = getattr(analysis, "nodes", None)
    if not isinstance(nodes, Mapping):
        raise GenericCanaryError("native analysis has no physical node mapping")
    keys = [str(name).casefold() for name in nodes]
    if len(keys) != len(set(keys)):
        raise GenericCanaryError("ambiguous case-insensitive physical node identity")
    lookup = {str(name).casefold(): values for name, values in nodes.items()}
    if any(name.casefold() not in lookup for name in required):
        raise GenericCanaryError("required physical waveform unavailable")
    time = [float(v) for v in analysis.time]
    traces = {"time": time}
    for name in (*required, *optional):
        if name.casefold() in lookup:
            traces[name] = [float(v) for v in lookup[name.casefold()]]
    if (len(time) < 3 or any(len(trace) != len(time) for trace in traces.values())
            or any(not math.isfinite(v) for trace in traces.values() for v in trace)
            or any(b <= a for a, b in zip(time, time[1:]))):
        raise GenericCanaryError("physical waveforms are nonfinite, misaligned or unordered")
    return traces


def evaluate_physical(fixture: SampleHoldFixture, traces: Mapping[str, list[float]],
                      *, mock: bool) -> dict:
    """Keep raw physical arrays and derived differential provenance separate."""
    required = ("time", fixture.mapping.clock, *fixture.mapping.inputs,
                *fixture.mapping.outputs)
    if any(name not in traces for name in required):
        raise GenericCanaryError("required physical waveform unavailable")
    time = traces["time"]
    if (len(time) < 3 or any(len(values) != len(time) for values in traces.values())
            or any(not math.isfinite(float(v)) for values in traces.values() for v in values)
            or any(b <= a for a, b in zip(time, time[1:]))):
        raise GenericCanaryError("raw physical waveform alignment or finiteness invalid")
    result = evaluate_waveforms(fixture, traces, synthetic=mock)
    normalized = {}
    if len(fixture.mapping.inputs) == 2:
        plus, minus = fixture.mapping.inputs
        out_plus, out_minus = fixture.mapping.outputs
        normalized = {
            "VIN_DIFF": [a - b for a, b in zip(traces[plus], traces[minus])],
            "VOUT_DIFF": [a - b for a, b in zip(traces[out_plus], traces[out_minus])],
            "provenance": {"VIN_DIFF": f"{plus}-{minus}",
                           "VOUT_DIFF": f"{out_plus}-{out_minus}"},
        }
    return {"evaluation": result.to_dict(), "normalized_diagnostics": normalized,
            "raw_physical_signal_names": list(traces), "mock": mock}
