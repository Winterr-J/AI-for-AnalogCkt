"""Source-bound, target-free SC integrator binding and transient measurement.

The inferred fixture is a hypothesis. Only a real two-case transient with a
measurable, directionally consistent accumulation can make Preparation READY.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Mapping

import numpy as np
import yaml

from ActiveLearning.InitializeD0.validation.parameter_validator import validate_parameters
from ActiveLearning.InitializeD0.validation.spec_validator import check_meets_spec
from ActiveLearning.sc_sample_hold_generic import canonical_sha256
from .errors import AutoConfigError
from .models import ActiveLearningCircuitConfig, RuntimeBinding, RuntimeParameter
from .sample_hold import SampleHoldCandidateBuilder
from .sample_hold_bulk_core import _device_rows, _model_definitions, sha


ROOT = Path(__file__).resolve().parents[2]
MASTER = ROOT / "ActiveLearning/candidate_manifests/sc_integrator_v2/master_manifest.json"
OVERRIDES = Path(__file__).with_name("sc_integrator_bulk_fixtures.json")
METRICS = ("integration_step_magnitude_v", "zero_input_drift_v_per_cycle",
           "step_consistency_v", "virtual_ground_error_v", "output_headroom_v")
_DEVICE = re.compile(r"^(\S+)\s+\(([^)]*)\)\s+(\S+)$")
_SOURCE = re.compile(r"^(VIN|VOUT|VCLK|VB|IB|IREF)\d+$")


@dataclass(frozen=True)
class SCIntegratorSemanticMapping:
    mapping: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.mapping)


def _candidate_ids() -> set[str]:
    import json
    data = json.loads(MASTER.read_text(encoding="utf-8"))
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        raise AutoConfigError("SCIntegrator master candidate list missing")
    return {row["circuit_id"] for row in candidates}


def _constructor(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "build_circuit"]
    if len(functions) != 1:
        raise AutoConfigError("SCIntegrator converted build_circuit is not unique")
    return {arg.arg for arg in functions[0].args.kwonlyargs}


def _topology(path: Path) -> list[tuple[str, tuple[str, ...], str]]:
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        match = _DEVICE.fullmatch(raw.strip())
        if match is None:
            raise AutoConfigError(f"unparsed SCIntegrator topology: {raw}")
        name, nodes, kind = match.groups()
        if kind not in {"nmos4", "pmos4", "npn", "pnp", "diode", "capacitor", "resistor"}:
            raise AutoConfigError(f"unsupported SCIntegrator device {kind}: {name}")
        rows.append((name, tuple(nodes.split()), kind))
    if not rows:
        raise AutoConfigError("empty SCIntegrator topology")
    return rows


def _validate_converted_topology(path: Path, rows: list[tuple[str, tuple[str, ...], str]]) -> None:
    """Require the converted device order and physical pins to match the .cir."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constructors = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "build_circuit"]
    if len(constructors) != 1:
        raise AutoConfigError("SCIntegrator converted topology constructor ambiguous")
    methods = {"MOSFET": "M", "C": "C", "R": "R", "BJT": "Q", "D": "D"}
    converted = []
    for node in ast.walk(constructors[0]):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or node.func.attr not in methods):
            continue
        prefix = methods[node.func.attr]
        try:
            identifier = prefix + ast.literal_eval(node.args[0])
        except (ValueError, TypeError, IndexError) as error:
            raise AutoConfigError("SCIntegrator converted device pin is not source-bound") from error
        converted.append((re.sub(r"_dup\d+$", "", identifier), node))
    if len(converted) != len(rows):
        raise AutoConfigError("SCIntegrator converted topology omits or adds a device")
    kind_method = {"nmos4": "MOSFET", "pmos4": "MOSFET", "capacitor": "C",
                   "resistor": "R", "npn": "BJT", "pnp": "BJT", "diode": "D"}
    for (source_name, source_pins, kind), (name, node) in zip(rows, converted):
        try:
            pins = tuple("VSS" if isinstance(value, ast.Attribute) and value.attr == "gnd"
                         else ast.literal_eval(value)
                         for value in node.args[1:1 + len(source_pins)])
        except (ValueError, TypeError) as error:
            raise AutoConfigError("SCIntegrator converted device pin is not source-bound") from error
        if (name != source_name or pins != source_pins
                or node.func.attr != kind_method[kind]):
            raise AutoConfigError("SCIntegrator converted device topology differs from source .cir")


def _nominal_values(plan: dict) -> dict:
    result = {}
    for name, row in _device_rows(plan["archive"]).items():
        if not isinstance(row, dict):
            continue
        for field in ("W", "L"):
            if f"{field}_um" in row:
                result[f"{field}_{name}"] = float(row[f"{field}_um"]) * 1e-6
        for key in ("R_ohm", "C_f", "C_F"):
            if key in row:
                result[name] = float(row[key])
    operating = plan["archive"].get("operating_parameters") or {}
    for name in plan["constructor_names"]:
        if name.endswith("_MODEL"):
            result[name] = name.removesuffix("_MODEL") + "_IDEAL"
        elif name in operating:
            result[name] = operating[name]
    result.update(plan["fixture"]["bias_overrides"])
    return result


def derive_plan(circuit_id: str, *, root: Path = ROOT) -> dict:
    import json
    root = Path(root).resolve()
    if circuit_id not in _candidate_ids():
        raise AutoConfigError("circuit is not in the historical SCIntegrator master manifest")
    base = root / "dataset" / circuit_id
    paths = {name: base / rel for name, rel in {
        "topology": f"topology/{circuit_id}.py",
        "netlist": f"topology/{circuit_id}.cir",
        "ideal": "simulation/parameter/ideal_param.yaml",
        "stage_a": "parameters/tunable_parameters.yaml",
        "stage_b": "parameters/ideal_design_space.yaml",
    }.items()}
    missing = [name for name, path in paths.items() if not path.is_file() or path.is_symlink()]
    if missing:
        raise AutoConfigError("SCIntegrator required resource missing: " + ", ".join(missing))
    archive = yaml.safe_load(paths["ideal"].read_text(encoding="utf-8"))
    if archive.get("circuit", {}).get("id") != circuit_id:
        raise AutoConfigError("SCIntegrator ideal archive identity mismatch")
    args = _constructor(paths["topology"])
    rows = _topology(paths["netlist"])
    _validate_converted_topology(paths["topology"], rows)
    nodes = {node for _, pins, _ in rows for node in pins}
    override_doc = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    if override_doc.get("schema_version") != "sc_integrator_bulk_fixture_overrides_v1":
        raise AutoConfigError("SCIntegrator fixture override schema changed")
    override = override_doc["circuits"].get(circuit_id, {})
    allowed = {"input", "output", "clocks", "virtual_ground", "integration_phase",
               "virtual_ground_reference",
               "expected_response_sign", "bias_overrides", "bias_shunt",
               "clock_low_v", "cycle_zero_s", "startup_high_until_s",
               "zero_input_v", "model_overrides"}
    if set(override) - allowed:
        raise AutoConfigError("unsupported SCIntegrator fixture override field")
    inputs = ([override["input"]] if "input" in override else
              sorted(name for name in args if re.fullmatch(r"VIN\d+", name) and name in nodes))
    outputs = ([override["output"]] if "output" in override else
               sorted(name for name in nodes if re.fullmatch(r"VOUT\d+", name)))
    clocks = (list(override["clocks"]) if "clocks" in override else
              sorted(name for name in args if re.fullmatch(r"VCLK\d+", name) and name in nodes))
    if len(inputs) != 1 or len(outputs) != 1:
        raise AutoConfigError("ambiguous SCIntegrator input/output: requires one VIN and one VOUT")
    if len(clocks) != 2:
        raise AutoConfigError("unresolved SCIntegrator clock phase relation: requires two physical clocks")
    if (inputs[0] not in args or inputs[0] not in nodes or outputs[0] not in nodes
            or not set(clocks) <= (args & nodes) or len(set(clocks)) != 2):
        raise AutoConfigError("SCIntegrator fixture ports do not bind physical topology")
    for clock in clocks:
        if not any(kind in {"nmos4", "pmos4"} and len(pins) >= 2 and pins[1] == clock
                   for _, pins, kind in rows):
            raise AutoConfigError(f"clock {clock} is not bound to a physical switch gate")
    feedback = [pins[0] if pins[1] == outputs[0] else pins[1]
                for _, pins, kind in rows if kind == "capacitor" and outputs[0] in pins]
    virtual_ground = override.get("virtual_ground") or (feedback[0] if len(feedback) == 1 else None)
    if (virtual_ground is None or virtual_ground not in feedback
            or virtual_ground in {outputs[0], "VSS", "0"}):
        raise AutoConfigError("unresolved SCIntegrator feedback capacitor/virtual-ground node")
    references = {other_pins[1]
                  for _, pins, kind in rows if kind in {"nmos4", "pmos4"}
                  and len(pins) == 4 and pins[1] == virtual_ground
                  for _, other_pins, other_kind in rows
                  if other_kind == kind and len(other_pins) == 4
                  and other_pins[2] == pins[2]
                  and other_pins[1] != virtual_ground
                  and other_pins[1] not in clocks}
    reference = override.get("virtual_ground_reference") or (
        next(iter(references)) if len(references) == 1 else None)
    if reference is None or reference not in nodes | {"VSS", "0"}:
        raise AutoConfigError("unresolved SCIntegrator virtual-ground reference")
    operating = archive.get("operating_parameters") or {}
    vdd = float(operating.get("VDD", float("nan")))
    if not math.isfinite(vdd) or vdd <= 0:
        raise AutoConfigError("SCIntegrator archived VDD is unavailable")
    zero_input = float(override.get("zero_input_v", operating.get(inputs[0], float("nan"))))
    if not math.isfinite(zero_input) or not (0 <= zero_input < vdd - 0.005):
        raise AutoConfigError("SCIntegrator zero-input operating level unavailable/outside supply")
    sampling_nodes = {node for _, pins, kind in rows if kind == "capacitor"
                      and outputs[0] not in pins for node in pins}
    transfer_clocks = {pins[1] for _, pins, kind in rows
                       if kind in {"nmos4", "pmos4"} and len(pins) == 4
                       and virtual_ground in {pins[0], pins[2]}
                       and ({pins[0], pins[2]} - {virtual_ground}) & sampling_nodes
                       and pins[1] in clocks}
    input_clocks = {pins[1] for _, pins, kind in rows
                    if kind in {"nmos4", "pmos4"} and len(pins) == 4
                    and inputs[0] in {pins[0], pins[2]} and pins[1] in clocks}
    if "integration_phase" not in override and (
            len(transfer_clocks) != 1 or not input_clocks
            or next(iter(transfer_clocks)) in input_clocks):
        raise AutoConfigError("unresolved SCIntegrator sampling/charge-transfer clock relation")
    inferred_phase = (f"phi{clocks.index(next(iter(transfer_clocks))) + 1}"
                      if len(transfer_clocks) == 1 else None)
    phase = override.get("integration_phase", inferred_phase)
    sign = override.get("expected_response_sign")
    biases = override.get("bias_overrides", {})
    if (phase not in {"phi1", "phi2"} or sign not in {None, "negative", "positive"}
            or not set(biases) <= args
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in biases.values())):
        raise AutoConfigError("invalid SCIntegrator phase/sign/bias fixture override")
    shunt = override.get("bias_shunt")
    if shunt is not None and (set(shunt) != {"node", "resistance_ohm", "capacitance_f"}
                              or shunt["node"] not in nodes
                              or float(shunt["resistance_ohm"]) <= 0
                              or float(shunt["capacitance_f"]) <= 0):
        raise AutoConfigError("invalid SCIntegrator external bias shunt")
    source = {path.relative_to(root).as_posix(): sha(path) for path in paths.values()}
    source[MASTER.relative_to(root).as_posix()] = sha(MASTER)
    source[OVERRIDES.relative_to(root).as_posix()] = sha(OVERRIDES)
    fixture = {"circuit_id": circuit_id, "input": inputs[0], "output": outputs[0],
               "clocks": clocks, "integration_phase": phase,
               "virtual_ground": virtual_ground,
               "virtual_ground_reference": reference,
               "expected_response_sign": sign,
               "period_s": 1e-6, "initialization_cycles": 8,
               "measurement_cycles": 6, "delta_v": 0.005,
               "supply_v": vdd, "zero_input_v": zero_input,
               "bias_overrides": biases, "bias_shunt": shunt,
               "clock_low_v": float(override.get("clock_low_v", 0.0)),
               "cycle_zero_s": float(override.get("cycle_zero_s", 0.0)),
               "startup_high_until_s": float(override.get("startup_high_until_s", 0.0)),
               "sampling_clocks": sorted(input_clocks),
               "transfer_clocks": sorted(transfer_clocks),
               "topology_status": "source_bound_hypothesis_requires_real_transient"}
    models = _model_definitions(archive)
    required_models = {name.removesuffix("_MODEL") + "_IDEAL" for name in args
                       if name.endswith("_MODEL")}
    if required_models - models.keys():
        raise AutoConfigError("SCIntegrator model definition missing: "
                              + ", ".join(sorted(required_models - models.keys())))
    for name, parameters in override.get("model_overrides", {}).items():
        if (name not in required_models or not isinstance(parameters, dict)
                or not set(parameters) <= {"VTO", "KP", "LAMBDA", "GAMMA", "PHI"}
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in parameters.values())):
            raise AutoConfigError("invalid SCIntegrator ideal model override")
        models[name]["parameters"].update(parameters)
    stimulus = {"period_s": fixture["period_s"], "clock_phase_windows": {
        clocks[0]: [0.12e-6, 0.40e-6], clocks[1]: [0.62e-6, 0.90e-6]},
        "rise_fall_s": 0.02e-6, "delta_v": fixture["delta_v"],
        "end_time_s": 15.2e-6, "step_time_s": 1e-9}
    return {"fixture": fixture, "stimulus": stimulus, "archive": archive,
            "models": {name: models[name] for name in sorted(required_models)},
            "constructor_names": args, "source_sha256": source,
            "fixture_sha256": canonical_sha256(fixture),
            "stimulus_sha256": canonical_sha256(stimulus)}


def build_runtime(circuit_type: str, circuit_id: str, *, repository_root: Path,
                  runtime_provider: Any) -> ActiveLearningCircuitConfig:
    root = Path(repository_root).resolve()
    plan = derive_plan(circuit_id, root=root)
    base = root / "dataset" / circuit_id
    stage_a_path = base / "parameters/tunable_parameters.yaml"
    stage_b_path = base / "parameters/ideal_design_space.yaml"
    stage_a = yaml.safe_load(stage_a_path.read_text(encoding="utf-8"))
    stage_b = yaml.safe_load(stage_b_path.read_text(encoding="utf-8"))
    if stage_a.get("circuit_id") != circuit_id or stage_b.get("circuit_id") != circuit_id:
        raise AutoConfigError("SCIntegrator Stage A/B circuit identity mismatch")
    nominal = _nominal_values(plan)
    parameters, scales, ranged = [], {}, []
    for instance, row in stage_a["device_parameters"].items():
        design = (stage_b.get("device_design_space") or {}).get(instance, {}).get("parameters") or {}
        for field, item in row["tunable_parameters"].items():
            if field not in {"W", "L", "R", "C"} or not item.get("optimizable"):
                continue
            name = f"{field}_{instance}" if field in {"W", "L"} else instance
            bounds = design.get(field)
            if name not in plan["constructor_names"] or name not in nominal or not bounds:
                continue
            if bounds.get("data_type") != "continuous":
                continue
            scale = 1e-6 if field in {"W", "L"} else 1.0
            value = round(float(nominal[name]) / scale, 12)
            low, high = float(bounds["minimum"]), float(bounds["maximum"])
            if not (math.isfinite(value) and low <= value <= high and low < high):
                raise AutoConfigError(f"{name}: archived nominal outside Stage B")
            scales[name] = scale
            binding = RuntimeBinding(f"{instance}.{field}", instance, field, name,
                                     instance, "device_instance", True)
            parameters.append(RuntimeParameter(name, "device_parameter", value, low, high,
                                               "continuous", bounds.get("unit"),
                                               bounds.get("scale"), None, None, (binding,)))
            ranged.append({"name": name, "parameter_id": f"{instance}.{field}",
                           "device_instance": instance, "parameter_kind": field,
                           "nominal_value": value, "unit": bounds.get("unit"),
                           "variable_type": "continuous", "optimization_eligible": True,
                           "admissible_range": {"lower": low, "upper": high,
                                                "scale": bounds.get("scale"),
                                                "transform": None, "fixed_sign": None},
                           "discrete_options": []})
    if not parameters:
        raise AutoConfigError("no physically bound continuous SCIntegrator parameters")
    dynamic = {plan["fixture"]["input"], *plan["fixture"]["clocks"]}
    fixed = {name: value for name, value in nominal.items()
             if name not in scales and name not in dynamic}
    unbound = plan["constructor_names"] - set(fixed) - set(scales) - dynamic
    if unbound:
        raise AutoConfigError("unbound SCIntegrator constructor argument: "
                              + ", ".join(sorted(unbound)))
    template = {"schema_version": "parameter_template_v1",
                "circuit": {"circuit_id": circuit_id, "circuit_type": circuit_type,
                            "simulation_environment": "ideal"},
                "parameters": ranged,
                "optimization_space": {"variables": list(scales),
                                       "effective_dimension_after_binding": len(scales)},
                "bindings": [], "constraints": [],
                "provenance": {"stage_a_sha256": sha(stage_a_path),
                               "stage_b_sha256": sha(stage_b_path),
                               "fixture_sha256": plan["fixture_sha256"]}}
    performance = runtime_provider.performance_template_path(root / "ActiveLearning")
    if not performance.is_file():
        raise AutoConfigError("SCIntegrator performance template missing")
    return ActiveLearningCircuitConfig(
        circuit_id=circuit_id, circuit_type=circuit_type,
        topology_path=base / "topology" / f"{circuit_id}.py",
        ideal_parameter_path=base / "simulation/parameter/ideal_param.yaml",
        stage_a_rows=tuple(stage_a["device_parameters"].items()),
        stage_b_search_space=stage_b, runtime_parameters=tuple(parameters),
        param_template=template, semantic_mapping=SCIntegratorSemanticMapping(plan["fixture"]),
        adapter=None, candidate_builder=SampleHoldCandidateBuilder(template, fixed, scales),
        runtime_provider=runtime_provider, operating_conditions=dict(plan["fixture"]),
        testbench_config={"mapping": dict(plan["fixture"]),
                          "fixture_status": "pending_real_preparation"},
        performance_template_path=performance, auto_bound=len(parameters), unresolved=(),
    )


class SCIntegratorEvaluatorAdapter:
    def __init__(self, runtime: ActiveLearningCircuitConfig, executor: Any):
        self.runtime, self.executor = runtime, executor

    def evaluate(self, params: Mapping[str, Any]) -> dict:
        if self.executor is None:
            raise AutoConfigError("SCIntegrator native executor and attempt ledger required")
        return self.executor(self.runtime.candidate_builder.expand_candidate(params))


class SCIntegratorFunctionalValidator:
    def validate(self, params: Mapping[str, Any], evaluation: Mapping[str, Any], *,
                 target_specs: Mapping[str, Any] | None = None) -> dict:
        result = dict(evaluation)
        metrics = dict(result.get("metrics") or {})
        if set(metrics) != set(METRICS):
            raise ValueError("SCIntegrator evaluation must contain all five metrics")
        status = {key: bool(result.get(key)) for key in
                  ("parameter_valid", "simulation_valid", "measurement_valid", "functional_valid")}
        status["functional_status"] = "valid" if status["functional_valid"] else "invalid"
        status["meets_spec"] = (check_meets_spec(metrics, target_specs)["meets_spec"]
                                if all(status[key] for key in ("parameter_valid", "simulation_valid",
                                                                    "measurement_valid", "functional_valid"))
                                else False if target_specs else None)
        return {"metrics": metrics, "status": status,
                "failure_reasons": list(result.get("failure_reasons") or []),
                "diagnostics": dict(result.get("diagnostics") or {}),
                "functional_checks": dict(result.get("functional_checks") or {})}


def evaluate_traces(fixture: Mapping[str, Any], baseline: Mapping[str, Any],
                    perturbed: Mapping[str, Any]) -> dict:
    """Evaluate actual A/B time traces; never invent missing metric values."""
    empty = {name: None for name in METRICS}
    try:
        period = float(fixture["period_s"])
        phase = fixture["integration_phase"]
        center = 0.35 if phase == "phi1" else 0.75
        times = [float(fixture.get("cycle_zero_s", 0.0)) + (8 + index + center) * period
                 for index in range(7)]

        def sample(case, key):
            t = np.asarray(case["time_s"], dtype=float)
            v = np.asarray(case[key], dtype=float)
            if (len(t) != len(v) or len(t) < 2 or not np.all(np.isfinite(t))
                    or not np.all(np.isfinite(v)) or np.any(np.diff(t) <= 0)
                    or t[0] > times[0] or t[-1] < times[-1]
                    or np.max(np.diff(t)) > 5e-8):
                raise ValueError(f"missing/nonfinite/coarse trace: {key}")
            return np.interp(times, t, v)

        a = sample(baseline, "output_v")
        b = sample(perturbed, "output_v")
        vg = sample(perturbed, "virtual_ground_v")
        input_a = sample(baseline, "input_v")
        input_b = sample(perturbed, "input_v")
        delta = float(fixture["delta_v"])
        zero = float(fixture.get("zero_input_v", 0.0))
        if (np.max(np.abs(input_a - zero)) > abs(delta) * .1
                or abs(input_b[0] - zero) > abs(delta) * .1
                or np.max(np.abs(input_b[1:] - zero - delta)) > abs(delta) * .1):
            raise ValueError("input perturbation not aligned with integration cycles")
        low = float(fixture["clock_low_v"])
        high = float(fixture["supply_v"])
        clocks = fixture["clocks"]
        for case in (baseline, perturbed):
            for index, clock in enumerate(clocks):
                t = np.asarray(case["time_s"], dtype=float)
                values = np.asarray(case["clocks_v"][clock], dtype=float)
                if len(values) != len(t) or not np.all(np.isfinite(values)):
                    raise ValueError(f"clock trace missing/nonfinite: {clock}")
                offset = float(fixture.get("cycle_zero_s", 0.0))
                active = np.asarray([offset + (8 + cycle + (.25 if index == 0 else .75)) * period
                                     for cycle in range(7)])
                inactive = np.asarray([offset + (8 + cycle + (.75 if index == 0 else .25)) * period
                                       for cycle in range(7)])
                dead = np.asarray([offset + (8 + cycle + .5) * period for cycle in range(7)])
                if (np.max(np.abs(np.interp(active, t, values) - high)) > .1 * high
                        or np.max(np.abs(np.interp(inactive, t, values) - low)) > .1 * high
                        or np.max(np.abs(np.interp(dead, t, values) - low)) > .1 * high):
                    raise ValueError(f"clock phase/non-overlap invalid: {clock}")
        response = b - a
        steps = np.diff(response)
        mean = float(np.mean(steps))
        rail = float(fixture["supply_v"])
        headroom = float(min(np.min(a), np.min(b), rail - np.max(a), rail - np.max(b)))
        metrics = {"integration_step_magnitude_v": abs(mean),
                   "zero_input_drift_v_per_cycle": float(np.mean(np.abs(np.diff(a)))),
                   "step_consistency_v": float(np.std(steps)),
                   "virtual_ground_error_v": float(np.sqrt(np.mean(vg**2))),
                   "output_headroom_v": headroom}
        positive = int(np.sum(steps > 1e-5))
        negative = int(np.sum(steps < -1e-5))
        expected = fixture.get("expected_response_sign")
        directional = positive >= 4 if expected == "positive" else (
            negative >= 4 if expected == "negative" else max(positive, negative) >= 4)
        functional = bool(directional and headroom > rail * .01)
        reasons = []
        if not directional:
            reasons.append("undetectable_or_wrong_direction_integration")
        if headroom <= rail * .01:
            reasons.append("sustained_output_clamp")
        return {"metrics": metrics, "parameter_valid": True, "simulation_valid": True,
                "measurement_valid": True, "functional_valid": functional,
                "failure_reasons": reasons,
                "functional_checks": {"positive_steps": positive,
                                      "negative_steps": negative, "headroom_v": headroom},
                "diagnostics": {"baseline_outputs_v": a.tolist(),
                                "perturbed_outputs_v": b.tolist(),
                                "baseline_corrected_steps_v": steps.tolist(),
                                "measured_gain": mean / delta}}
    except (KeyError, ValueError, TypeError, IndexError) as error:
        return {"metrics": empty, "parameter_valid": True, "simulation_valid": True,
                "measurement_valid": False, "functional_valid": False,
                "failure_reasons": [f"measurement_invalid:{error}"],
                "diagnostics": {}}


def validate_bulk_al(runtime: ActiveLearningCircuitConfig, config: dict,
                     config_path: Path, d0_path: Path):
    """Bind AL only to a hash-verified READY receipt and isolated five-slot ledger."""
    from ActiveLearning.batch_runner_v2.sc_integrator_production import (
        OUTPUT_ROOT, _receipt, _sha, inspect_manifest,
    )
    from .sc_integrator_bulk_native import SCIntegratorPhysicalExecutor

    bulk = config.get("sc_integrator_bulk") or {}
    if set(bulk) != {"batch_id", "candidate_manifest_sha256", "receipt_path",
                     "al_native_root", "fixture_sha256", "stimulus_sha256"}:
        raise AutoConfigError("SCIntegrator AL bulk marker incomplete")
    spec = inspect_manifest(bulk["batch_id"])
    if (runtime.circuit_id not in spec["circuit_ids"]
            or bulk["candidate_manifest_sha256"] != spec["candidate_manifest_sha256"]):
        raise AutoConfigError("SCIntegrator AL circuit/Manifest identity changed")
    directory = Path(spec["preparation_root"]) / runtime.circuit_id
    if Path(bulk["receipt_path"]).resolve() != (directory / "result.json").resolve():
        raise AutoConfigError("SCIntegrator AL receipt path changed")
    saved = _receipt(spec, runtime.circuit_id)
    if saved.get("status") != "PREPARATION_READY":
        raise AutoConfigError("SCIntegrator AL requires PREPARATION_READY")
    config_path = Path(config_path).resolve(strict=True)
    d0_path = Path(d0_path).resolve(strict=True)
    plan = derive_plan(runtime.circuit_id)
    if (config_path != (directory / saved["config_path"]).resolve(strict=True)
            or d0_path != (directory / saved["d0_path"]).resolve(strict=True)
            or _sha(config_path) != saved["artifacts"][saved["config_path"]]
            or _sha(d0_path) != saved["artifacts"][saved["d0_path"]]
            or plan["fixture_sha256"] != bulk["fixture_sha256"]
            or plan["stimulus_sha256"] != bulk["stimulus_sha256"]
            or canonical_sha256(config.get("target_specs")) != saved["target_spec_sha256"]):
        raise AutoConfigError("SCIntegrator AL source/target/D0 identity changed")
    settings, data = config.get("active_learning") or {}, config.get("data") or {}
    if (settings.get("max_iterations") != 5 or settings.get("stop_on_meets_spec") is not False
            or settings.get("prediction_prompt_version") != "v1"
            or data.get("d0_mode") != "independent" or data.get("seed") != 42
            or data.get("n_samples") != 20 or data.get("sampling_mode") != "v4"):
        raise AutoConfigError("SCIntegrator AL iteration/D0 policy changed")
    native_root = OUTPUT_ROOT / bulk["batch_id"] / "native" / runtime.circuit_id
    if Path(bulk["al_native_root"]).resolve() != native_root.resolve():
        raise AutoConfigError("SCIntegrator AL physical ledger root changed")
    executor = SCIntegratorPhysicalExecutor(
        runtime, native_root, stage="al", limit=5,
        extra_identity={"batch_id": bulk["batch_id"],
                        "config_sha256": _sha(config_path), "d0_sha256": _sha(d0_path)})
    executor.verify_existing()
    return executor
