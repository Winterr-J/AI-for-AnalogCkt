"""Stage A/B-backed, simulator-free SampleHold parameter binding.

The historical one-attempt canary is deliberately not a D0 execution backend.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

from ActiveLearning.InitializeD0.validation import validate_parameters

from .errors import AutoConfigError
from .models import ActiveLearningCircuitConfig, RuntimeBinding, RuntimeParameter


_ASSETS = {
    "circuit_0259": "SC_0259_TRANSIENT_FIXTURE_V1_SC4B3C.json",
    "circuit_0605": "SC_0605_CANARY_FIXTURE_CANDIDATE_SC4C4.json",
    "circuit_0263": "SC_0263_CANARY_FIXTURE_CANDIDATE_SC4C4.json",
}
_METRICS = ("track_end_error_v", "sampling_error_v",
            "hold_isolation_error_v", "hold_droop_v_per_s")


@dataclass(frozen=True)
class SampleHoldSemanticMapping:
    mapping: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.mapping)


class SampleHoldCandidateBuilder:
    """Bind only explicitly ranged physical constructor arguments."""

    def __init__(self, template: dict, fixed: Mapping[str, Any], scales: Mapping[str, float]):
        self.template = template
        self.fixed = dict(fixed)
        self.scales = dict(scales)

    def expand_candidate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        for name, value in params.items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite numeric SampleHold value")
        result = validate_parameters(params, self.template)
        if not result["parameter_valid"]:
            raise ValueError("; ".join(result["violations"]))
        return {**self.fixed, **{
            name: float(params[name]) * self.scales[name] for name in self.scales
        }}


class SampleHoldEvaluatorAdapter:
    """Existing D0/AL evaluator contract; no implicit native execution."""

    def __init__(self, runtime: ActiveLearningCircuitConfig, executor=None):
        self.runtime = runtime
        self.executor = executor

    def evaluate(self, params: Mapping[str, Any]) -> Any:
        arguments = self.runtime.candidate_builder.expand_candidate(params)
        if self.executor is None:
            raise AutoConfigError(
                "SampleHold parameterized native batch executor and independent "
                "attempt budget are not authorized/configured"
            )
        return self.executor(arguments)


class SampleHoldFunctionalValidator:
    """Translate the unchanged V1 evaluator result into the shared D0 schema."""

    def validate(self, params, evaluation, *, target_specs=None):
        from ActiveLearning.InitializeD0.validation import check_meets_spec

        result = evaluation.to_dict() if hasattr(evaluation, "to_dict") else dict(evaluation)
        metrics = dict(result["metrics"])
        if set(metrics) != set(_METRICS):
            raise ValueError("SampleHold V1 requires all four measured metric fields")
        parameter_valid = bool(result["parameter_valid"])
        simulation_valid = bool(result["simulation_valid"])
        measurement_valid = bool(result["measurement_valid"])
        functional_valid = bool(result["functional_valid"])
        meets_spec = (check_meets_spec(metrics, target_specs)["meets_spec"]
                      if parameter_valid and simulation_valid and measurement_valid
                      and functional_valid else None)
        return {
            "metrics": metrics,
            "status": {"parameter_valid": parameter_valid,
                       "simulation_valid": simulation_valid,
                       "measurement_valid": measurement_valid,
                       "functional_valid": functional_valid,
                       "functional_status": "valid" if functional_valid else "invalid",
                       "meets_spec": meets_spec},
            "failure_reasons": list(result.get("failure_reasons") or []),
            "diagnostics": dict(result.get("diagnostics") or {}),
            "functional_checks": {"measurement_valid": measurement_valid},
        }


def _verified_asset(root: Path, entry: Mapping[str, str]) -> Path:
    path = (root / entry["path"]).resolve(strict=True)
    if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise AutoConfigError("SampleHold source asset hash/path mismatch")
    return path


def _constructor_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "build_circuit"]
    if len(functions) != 1:
        raise AutoConfigError("SampleHold converted topology constructor is ambiguous")
    return {item.arg for item in functions[0].args.kwonlyargs}


def build_sample_hold_circuit_config(circuit_type: str, circuit_id: str, *,
                                     repository_root: str | Path | None = None,
                                     runtime_provider: Any) -> ActiveLearningCircuitConfig:
    if circuit_id not in _ASSETS:
        raise AutoConfigError(f"No reviewed SampleHold mapping for {circuit_id}")
    root = (Path(repository_root) if repository_root else Path(__file__).resolve().parents[2]).resolve()
    dataset = root / "dataset" / circuit_id
    docs = root / "ActiveLearning" / "docs" / "reference_audit"
    evidence = json.loads((docs / _ASSETS[circuit_id]).read_text(encoding="utf-8"))
    assets = evidence["source_assets"]
    topology = _verified_asset(root, assets["converted"] if circuit_id != "circuit_0259"
                               else assets["converted_pyspice"])
    ideal = _verified_asset(root, assets["ideal_parameters"] if circuit_id != "circuit_0259"
                            else assets["ideal_parameter_archive"])
    _verified_asset(root, assets["topology"])
    stage_a_path = dataset / "parameters" / "tunable_parameters.yaml"
    stage_b_path = dataset / "parameters" / "ideal_design_space.yaml"
    stage_a, stage_b = yaml.safe_load(stage_a_path.read_text(encoding="utf-8")), yaml.safe_load(stage_b_path.read_text(encoding="utf-8"))
    if stage_a.get("circuit_id") != circuit_id or stage_b.get("circuit_id") != circuit_id:
        raise AutoConfigError("SampleHold Stage A/B circuit identity mismatch")
    if circuit_id == "circuit_0259":
        fixture = evidence["fixture_inputs"]
        mapping, conditions = fixture["mapping"], fixture["conditions"]
        fixed = {"NMOS_MODEL": "NMOS_IDEAL", "C0": evidence["fixed_parameters"]["C0_f"]}
        nominals = {"W_M0": evidence["fixed_parameters"]["W_M0_m"],
                    "L_M0": evidence["fixed_parameters"]["L_M0_m"],
                    "C0": evidence["fixed_parameters"]["C0_f"]}
        archive = yaml.safe_load(ideal.read_text(encoding="utf-8"))
        if (archive["devices"]["MOS"]["M0"]["model"] != fixed["NMOS_MODEL"]
                or not math.isclose(archive["devices"]["MOS"]["M0"]["W_um"] * 1e-6,
                                    nominals["W_M0"], rel_tol=1e-12)
                or not math.isclose(archive["devices"]["MOS"]["M0"]["L_um"] * 1e-6,
                                    nominals["L_M0"], rel_tol=1e-12)
                or not math.isclose(archive["devices"]["C"]["C0"]["C_f"], fixed["C0"],
                                    rel_tol=1e-12)):
            raise AutoConfigError("circuit_0259 archive and frozen canary nominal disagree")
    else:
        if os.name == "posix" and circuit_id == "circuit_0263":
            from .sample_hold_ubuntu import load_al_plan
            load_al_plan(circuit_id)
        else:
            from .sample_hold_phase_a import verify_static_candidate
            verify_static_candidate(docs / _ASSETS[circuit_id])
        mapping, conditions = evidence["mapping"], evidence["conditions"]
        fixed = {key: spec["value"] for key, spec in evidence["assembly"]["constructor_arguments"].items()
                 if spec["kind"] == "fixed"}
        nominals = dict(fixed)
    constructors = _constructor_names(topology)
    ranged, scales, runtime_parameters = [], {}, []
    for instance, fields in stage_a["device_parameters"].items():
        design = (stage_b.get("device_design_space") or {}).get(instance, {}).get("parameters") or {}
        for field, stage_a_item in fields["tunable_parameters"].items():
            if field not in {"W", "L", "R", "C"} or not stage_a_item.get("optimizable"):
                continue
            argument = f"{field}_{instance}" if field in {"W", "L"} else instance
            bounds = design.get(field)
            if argument not in constructors or argument not in nominals or not bounds:
                continue  # A declared geometry without a physical ideal constructor is not X.
            if bounds.get("data_type") != "continuous":
                continue
            scale = 1e-6 if field in {"W", "L"} else 1.0
            nominal = round(float(nominals[argument]) / scale, 12) if scale != 1 else float(nominals[argument])
            low, high = float(bounds["minimum"]), float(bounds["maximum"])
            if not (math.isfinite(low) and math.isfinite(high) and low <= nominal <= high and low < high):
                raise AutoConfigError(f"{argument}: Stage B does not enclose frozen nominal")
            if not math.isclose(nominal, float(bounds["default"]), rel_tol=1e-10):
                raise AutoConfigError(f"{argument}: Stage B default differs from canary nominal")
            scales[argument] = scale
            ranged.append({"name": argument, "parameter_id": f"{instance}.{field}",
                           "device_instance": instance, "parameter_kind": field,
                           "nominal_value": nominal, "unit": bounds["unit"],
                           "variable_type": "continuous", "optimization_eligible": True,
                           "admissible_range": {"lower": low, "upper": high,
                                                "scale": bounds.get("scale"), "transform": None,
                                                "fixed_sign": None}, "discrete_options": []})
            binding = RuntimeBinding(f"{instance}.{field}", instance, field, argument,
                                     instance, "device_instance", True)
            runtime_parameters.append(RuntimeParameter(argument, "device_parameter", nominal,
                                                      low, high, "continuous", bounds["unit"],
                                                      bounds.get("scale"), None, None, (binding,)))
    if not ranged:
        raise AutoConfigError("No physically bound, continuous SampleHold parameters")
    fixed = {name: value for name, value in fixed.items() if name not in scales}
    if not set(scales) | set(fixed) <= constructors:
        raise AutoConfigError("SampleHold constructor binding mismatch")
    template = {"schema_version": "parameter_template_v1",
                "circuit": {"circuit_id": circuit_id, "circuit_type": circuit_type,
                            "simulation_environment": "ideal"},
                "parameters": ranged,
                "optimization_space": {"variables": list(scales),
                                       "effective_dimension_after_binding": len(scales)},
                "bindings": [], "constraints": [],
                "provenance": {"stage_a": str(stage_a_path), "stage_b": str(stage_b_path),
                               "nominal_fixture": str(docs / _ASSETS[circuit_id])}}
    performance = runtime_provider.performance_template_path(root / "ActiveLearning")
    if not performance.is_file():
        raise AutoConfigError("SampleHold performance template missing")
    testbench = {"mapping": mapping, "conditions": conditions,
                 "metric_selection": {name: name != "hold_droop_v_per_s" for name in _METRICS},
                 "target_status": "not_frozen",
                 "independent_d0_plan": {"sampling_mode": "v3", "seed": 42,
                                         "n_samples": 20, "n_local": 10, "n_global": 9},
                 "result_format": "existing_D0_JSONL_CSV_and_AL_run_logging",
                  "native_execution": "requires_configured_parameterized_batch_worker"}
    return ActiveLearningCircuitConfig(
        circuit_id=circuit_id, circuit_type=circuit_type, topology_path=topology,
        ideal_parameter_path=ideal, stage_a_rows=tuple(stage_a["device_parameters"].items()),
        stage_b_search_space=stage_b, runtime_parameters=tuple(runtime_parameters),
        param_template=template, semantic_mapping=SampleHoldSemanticMapping(mapping),
        adapter=None, candidate_builder=SampleHoldCandidateBuilder(template, fixed, scales),
        runtime_provider=runtime_provider, operating_conditions=dict(conditions),
        testbench_config=testbench, performance_template_path=performance,
        auto_bound=len(runtime_parameters), unresolved=(),
    )
