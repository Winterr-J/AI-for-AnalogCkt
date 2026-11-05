"""Source-bound SampleHold preparation for the two bulk manifests.

All conditions here are hypotheses until a real transient passes the shared
SampleHold functional validator.  Importing this module never loads ngspice.
"""

from __future__ import annotations

import ast
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re

import yaml

from ActiveLearning.sample_hold_offline import CircuitMapping, OperatingConditions, build_fixture
from ActiveLearning.sc_sample_hold_generic import build_stimulus, canonical_sha256
from .errors import AutoConfigError
from .models import ActiveLearningCircuitConfig, RuntimeBinding, RuntimeParameter


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("sample_hold_bulk_fixtures.json")
BULK_IDS = frozenset("circuit_" + n for n in (
    "0260", "0262", "0605", "0606", "0607", "0608", "0609", "0610",
    "0612", "0614", "0615", "0617", "0618", "0619", "0658",
))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _constructor(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == "build_circuit"]
    if len(methods) != 1:
        raise AutoConfigError("converted topology lacks a unique build_circuit")
    return {arg.arg for arg in methods[0].args.kwonlyargs}


def _device_rows(archive: dict) -> dict:
    groups = archive.get("devices")
    if isinstance(groups, dict):
        return {name: row for group in groups.values() if isinstance(group, dict)
                for name, row in group.items()}
    return archive.get("device_parameters") or {}


def _model_definitions(archive: dict) -> dict:
    defaults = archive.get("model_defaults")
    if not isinstance(defaults, dict):
        defaults = {row["name"]: row for row in (archive.get("models") or {}).values()
                    if isinstance(row, dict) and "name" in row}
    result = {}
    for name, row in defaults.items():
        if name in {"NMOS_IDEAL", "PMOS_IDEAL"}:
            result[name] = {"type": "NMOS" if name.startswith("N") else "PMOS",
                            "parameters": {"LEVEL": int(row.get("level", 1)),
                                           "VTO": float(row.get("VTO_V", row.get("VTO"))),
                                           "KP": float(row["KP_uA_per_V2"]) * 1e-6,
                                           "LAMBDA": float(row.get("LAMBDA_per_V", row.get("LAMBDA")))}}
        elif name in {"NPN_IDEAL", "PNP_IDEAL"}:
            result[name] = {"type": "NPN" if name.startswith("N") else "PNP",
                            "parameters": {key: float(row[key]) for key in
                                           ("IS", "BF", "VAF", "EG", "XTI") if key in row}}
        elif name == "DIODE_IDEAL":
            result[name] = {"type": "D", "parameters": dict(row)}
    # The migrated ideal archives name DIODE_IDEAL on device rows but do not
    # contain a .model card. Register that exact ideal-diode class explicitly;
    # never reinterpret a diode as a BJT/MOS or omit the physical device.
    if any(row.get("model") == "DIODE_IDEAL" for row in _device_rows(archive).values()
           if isinstance(row, dict)) and "DIODE_IDEAL" not in result:
        result["DIODE_IDEAL"] = {"type": "D", "parameters": {"IS": 1e-14, "N": 1.0}}
    return result


def derive_plan(circuit_id: str, *, root: Path = ROOT) -> dict:
    """Bind real topology nodes and a conservative physical clock hypothesis."""
    if circuit_id not in BULK_IDS:
        raise AutoConfigError("circuit is not in SampleHold bulk scope")
    root = Path(root).resolve()
    base = root / "dataset" / circuit_id
    topology = base / "topology" / f"{circuit_id}.py"
    netlist = base / "topology" / f"{circuit_id}.cir"
    ideal = base / "simulation/parameter/ideal_param.yaml"
    if not all(path.is_file() and not path.is_symlink() for path in (topology, netlist, ideal)):
        raise AutoConfigError("SampleHold converted topology/netlist/ideal archive missing")
    archive = yaml.safe_load(ideal.read_text(encoding="utf-8"))
    if archive.get("circuit", {}).get("id") != circuit_id:
        raise AutoConfigError("SampleHold ideal archive circuit identity mismatch")
    configs = json.loads(FIXTURES.read_text(encoding="utf-8"))
    selected = configs[circuit_id]
    args = _constructor(topology)
    netlist_text = netlist.read_text(encoding="utf-8")
    physical_nodes = set(re.findall(r"\b(?:VIN|VOUT|VCLK|VTRACK|VHOLD)\d+\b", netlist_text))
    inputs, outputs = tuple(selected["inputs"]), tuple(selected["outputs"])
    clock, complements = selected["track_clock"], tuple(selected["complement_clocks"])
    if (not set((*inputs, *outputs, clock, *complements)) <= physical_nodes
            or not set((*inputs, clock, *complements)) <= args
            or len(inputs) not in (1, 2) or len(outputs) != len(inputs)
            or len({*inputs, *outputs, clock, *complements}) !=
               len(inputs) + len(outputs) + 1 + len(complements)):
        raise AutoConfigError("SampleHold fixture ports do not bind physical topology")
    controls = {name for name in args if re.fullmatch(r"(?:VCLK|VTRACK|VHOLD)\d+", name)}
    if controls != {clock, *complements}:
        raise AutoConfigError("unresolved SampleHold multi-clock phase or control source")
    if len(complements) > 1:
        raise AutoConfigError("multi-clock phase relation is not automatically resolvable")
    operating = archive.get("operating_parameters") or {}
    unresolved_currents = {name for name in operating
                           if re.fullmatch(r"(?:IB|IREF|IIN)\d+", name)
                           and name not in args and re.search(rf"\b{re.escape(name)}\b", netlist_text)}
    if unresolved_currents:
        raise AutoConfigError("current-source orientation not encoded by converted topology: "
                              + ", ".join(sorted(unresolved_currents)))
    supply = float(operating["VDD"])
    vin = float(operating[inputs[0]])
    frequency = float(operating.get("clock_frequency", 1e6))
    if not all(map(math.isfinite, (supply, vin, frequency))) or not (0 < vin < supply and frequency > 0):
        raise AutoConfigError("invalid archived SampleHold operating condition")
    period = 1.0 / frequency
    span = min(0.4, supply / 4, vin, supply - vin)
    vin_range = (-span / 2, span / 2) if len(inputs) == 2 else (vin - span, vin + span)
    mapping = CircuitMapping(circuit_id, inputs, outputs, clock, selected["track_level"], sha(netlist))
    conditions = OperatingConditions(
        vin_range, period, 0.0, supply, 0.5, period * .01, period * .01,
        {"supply": ideal.relative_to(root).as_posix(),
         "input": ideal.relative_to(root).as_posix(),
         "clock_frequency": (ideal.relative_to(root).as_posix()
                             if "clock_frequency" in operating
                             else "preparation_hypothesis_1MHz")},
        fixed_common_mode_v=vin if len(inputs) == 2 else None,
        vin_edge_s=period * .01,
    )
    fixture = build_fixture(mapping, conditions, guard_time_s=period * .005)
    stimulus = build_stimulus(
        fixture, startup_input_v=0.0 if len(inputs) == 2 else vin,
        physical_input_bounds_v={name: [0.0, supply] for name in inputs},
    )
    models = _model_definitions(archive)
    required_models = {name.removesuffix("_MODEL") + "_IDEAL" for name in args
                       if name.endswith("_MODEL")}
    if not required_models <= models.keys():
        raise AutoConfigError("ideal model definition missing: "
                              + ", ".join(sorted(required_models - models.keys())))
    source = {path.relative_to(root).as_posix(): sha(path)
              for path in (topology, netlist, ideal,
                           base / "parameters/tunable_parameters.yaml",
                           base / "parameters/ideal_design_space.yaml")}
    source["ActiveLearning/onboarding/sample_hold_bulk_fixtures.json"] = sha(FIXTURES)
    return {"fixture": fixture, "stimulus": stimulus, "archive": archive,
            "models": {name: models[name] for name in sorted(required_models)},
            "complement_clocks": complements, "constructor_names": args,
            "source_sha256": source,
            "fixture_sha256": fixture.fixture_hash,
            "stimulus_sha256": canonical_sha256(stimulus)}


def _nominal_values(plan: dict) -> dict:
    archive = plan["archive"]
    result = {}
    for name, row in _device_rows(archive).items():
        if not isinstance(row, dict):
            continue
        for key, value in (("W_um", f"W_{name}"), ("L_um", f"L_{name}")):
            if key in row:
                result[value] = float(row[key]) * 1e-6
        for key in ("R_ohm", "C_f", "C_F"):
            if key in row:
                result[name] = float(row[key])
    operating = archive.get("operating_parameters") or {}
    for name in plan["constructor_names"]:
        if name.endswith("_MODEL"):
            result[name] = name.removesuffix("_MODEL") + "_IDEAL"
        elif name in operating:
            result[name] = operating[name]
    dynamic = set(plan["fixture"].mapping.inputs) | {plan["fixture"].mapping.clock,
                                                    *plan["complement_clocks"]}
    if plan["constructor_names"] - result.keys() - dynamic:
        raise AutoConfigError("unbound converted constructor argument: "
                              + ", ".join(sorted(plan["constructor_names"] - result.keys() - dynamic)))
    return result


def build_bulk_runtime(circuit_type: str, circuit_id: str, *, repository_root: Path,
                       runtime_provider) -> ActiveLearningCircuitConfig:
    from .sample_hold import SampleHoldCandidateBuilder, SampleHoldSemanticMapping, _METRICS

    root = Path(repository_root).resolve()
    plan = derive_plan(circuit_id, root=root)
    base = root / "dataset" / circuit_id
    stage_a_path = base / "parameters/tunable_parameters.yaml"
    stage_b_path = base / "parameters/ideal_design_space.yaml"
    stage_a = yaml.safe_load(stage_a_path.read_text(encoding="utf-8"))
    stage_b = yaml.safe_load(stage_b_path.read_text(encoding="utf-8"))
    if stage_a.get("circuit_id") != circuit_id or stage_b.get("circuit_id") != circuit_id:
        raise AutoConfigError("SampleHold Stage A/B circuit identity mismatch")
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
        raise AutoConfigError("no physically bound continuous SampleHold parameters")
    dynamic = set(plan["fixture"].mapping.inputs) | {plan["fixture"].mapping.clock,
                                                    *plan["complement_clocks"]}
    fixed = {key: value for key, value in nominal.items()
             if key not in scales and key not in dynamic}
    template = {"schema_version": "parameter_template_v1",
                "circuit": {"circuit_id": circuit_id, "circuit_type": circuit_type,
                            "simulation_environment": "ideal"},
                "parameters": ranged,
                "optimization_space": {"variables": list(scales),
                                       "effective_dimension_after_binding": len(scales)},
                "bindings": [], "constraints": [],
                "provenance": {"stage_a_sha256": sha(stage_a_path),
                               "stage_b_sha256": sha(stage_b_path),
                               "fixture_hypothesis_sha256": plan["fixture_sha256"]}}
    performance = runtime_provider.performance_template_path(root / "ActiveLearning")
    if not performance.is_file():
        raise AutoConfigError("SampleHold performance template missing")
    mapping = asdict(plan["fixture"].mapping)
    conditions = asdict(plan["fixture"].conditions)
    return ActiveLearningCircuitConfig(
        circuit_id=circuit_id, circuit_type=circuit_type,
        topology_path=base / "topology" / f"{circuit_id}.py",
        ideal_parameter_path=base / "simulation/parameter/ideal_param.yaml",
        stage_a_rows=tuple(stage_a["device_parameters"].items()),
        stage_b_search_space=stage_b, runtime_parameters=tuple(parameters),
        param_template=template, semantic_mapping=SampleHoldSemanticMapping(mapping),
        adapter=None, candidate_builder=SampleHoldCandidateBuilder(template, fixed, scales),
        runtime_provider=runtime_provider, operating_conditions=conditions,
        testbench_config={"mapping": mapping, "conditions": conditions,
                          "metric_selection": {name: name != "hold_droop_v_per_s" for name in _METRICS},
                          "fixture_status": "pending_real_preparation"},
        performance_template_path=performance, auto_bound=len(parameters), unresolved=(),
    )


def validate_bulk_al(runtime, config: dict, config_path: Path, d0_path: Path):
    """Automatic READY receipt + physical environment guard for the AL loop."""
    from ActiveLearning.batch_runner_v2.sample_hold_bulk_production import (
        EXPECTED, _receipt, inspect_manifest,
    )
    from ActiveLearning.batch_runner_v2.schema import canonical_sha256
    from .sample_hold_bulk_native import BulkPhysicalExecutor

    bulk = config.get("sample_hold_bulk") or {}
    if set(bulk) != {"batch_id", "manifest_path", "receipt_path", "al_native_root",
                     "fixture_sha256", "stimulus_sha256"}:
        raise AutoConfigError("SampleHold bulk AL marker incomplete")
    batch = bulk["batch_id"]
    if batch not in EXPECTED or runtime.circuit_id[-4:] not in EXPECTED[batch]:
        raise AutoConfigError("SampleHold circuit not in selected bulk Batch")
    manifest_path = Path(bulk["manifest_path"]).resolve(strict=True)
    if manifest_path != (ROOT / "ActiveLearning/batch_runner_v2" / f"{batch}.json").resolve():
        raise AutoConfigError("SampleHold bulk manifest path changed")
    spec = inspect_manifest(manifest_path)
    item = next(row for row in spec["jobs"] if row["circuit_id"] == runtime.circuit_id)
    receipt_path = Path(spec["preparation_root"]) / runtime.circuit_id / "result.json"
    if Path(bulk["receipt_path"]).resolve() != receipt_path.resolve():
        raise AutoConfigError("SampleHold preparation receipt path changed")
    saved = _receipt(spec, item)
    if saved.get("status") != "PREPARATION_READY":
        raise AutoConfigError("SampleHold circuit is not PREPARATION_READY")
    config_path = Path(config_path).resolve(strict=True)
    d0_path = Path(d0_path).resolve(strict=True)
    directory = receipt_path.parent
    plan = derive_plan(runtime.circuit_id)
    if (config_path != (directory / saved["config_path"]).resolve(strict=True)
            or d0_path != (directory / saved["d0_path"]).resolve(strict=True)
            or sha(config_path) != saved["artifacts"][saved["config_path"]]
            or sha(d0_path) != saved["artifacts"][saved["d0_path"]]
            or plan["fixture_sha256"] != bulk["fixture_sha256"]
            or plan["stimulus_sha256"] != bulk["stimulus_sha256"]
            or canonical_sha256(config.get("target_specs")) != saved["target_spec_sha256"]):
        raise AutoConfigError("SampleHold bulk AL source/target/D0 identity changed")
    settings = config.get("active_learning") or {}
    data = config.get("data") or {}
    if (settings.get("max_iterations") != 5
            or settings.get("stop_on_meets_spec") is not False
            or settings.get("prediction_prompt_version") != "compact_v1"
            or data.get("d0_mode") != "independent"
            or data.get("seed") != 42 or data.get("n_samples") != 20
            or data.get("sampling_mode") != "v4"):
        raise AutoConfigError("SampleHold bulk AL iteration/D0 policy changed")
    native_root = Path(spec["output_root"]) / batch / "native" / item["job_id"]
    if Path(bulk["al_native_root"]).resolve() != native_root.resolve():
        raise AutoConfigError("SampleHold bulk AL physical ledger root changed")
    executor = BulkPhysicalExecutor(
        runtime, native_root, stage="al", limit=5,
        extra_identity={"batch_id": batch, "config_sha256": sha(config_path),
                        "d0_sha256": sha(d0_path)},
    )
    executor.verify_existing()
    return executor
