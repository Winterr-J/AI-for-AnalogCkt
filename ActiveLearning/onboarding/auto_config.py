"""Convert frozen Stage A/B results into an executable AL runtime config."""

from __future__ import annotations

import importlib.util
import inspect
import math
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from param_extraction.stage_a import (
    parse_pyspice_file,
    resolve_bindings,
    validate_candidates,
)
from param_extraction.stage_a.integration.pipeline import build_stage_a_output_rows
from param_extraction.stage_b import construct_admissible_search_space
from param_extraction.stage_b.core import (
    CircuitContext,
    OperatingContext,
    TechnologyContext,
)
from param_extraction.stage_b.providers import IdealRangeProvider
from param_extraction.stage_b.validation.integration_validation import (
    ValidationCircuit,
    build_context as build_stage_b_nominal_context,
)

from .errors import AutoConfigError, BindingValidationError, SemanticMappingError
from .generic_adapter import GenericPySpiceAdapter, GenericPySpiceBuilder
from .models import (
    ActiveLearningCircuitConfig,
    CurrentMirrorSemanticMapping,
    RuntimeBinding,
    RuntimeParameter,
    SemanticCurrentMeasurement,
    SemanticExcitationCandidate,
    SemanticReferenceExcitation,
)
from .opamp_semantics import resolve_opamp_semantics
from .runtime_provider import resolve_runtime_provider


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FIELD_MAP = {
    "width": ("W", "width"),
    "w": ("W", "width"),
    "length": ("L", "length"),
    "l": ("L", "length"),
    "resistance": ("resistance", "resistance"),
    "resistor": ("resistance", "resistance"),
    "r": ("resistance", "resistance"),
    "capacitance": ("capacitance", "capacitance"),
    "capacitor": ("capacitance", "capacitance"),
    "c": ("capacitance", "capacitance"),
    "inductance": ("inductance", "inductance"),
    "inductor": ("inductance", "inductance"),
    "multiplicity": ("m", "multiplier"),
    "m": ("m", "multiplier"),
    "area": ("area", "area"),
    "current": ("current", "dc_value"),
    "bias_current": ("current", "dc_value"),
    "voltage": ("voltage", "dc_value"),
    "bias_voltage": ("voltage", "dc_value"),
}


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            payload = yaml.safe_load(stream)
    except OSError as error:
        raise AutoConfigError(f"Cannot load onboarding input {path}: {error}") from error
    if not isinstance(payload, dict):
        raise AutoConfigError(f"Onboarding input {path} is not a YAML mapping")
    return payload


def _load_topology_factory(path: Path, circuit_id: str):
    module_name = f"_active_learning_autoconfig_{circuit_id}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AutoConfigError(f"Cannot import PySpice topology {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise AutoConfigError(f"Cannot import PySpice topology {path}: {error}") from error
    factory = getattr(module, "build_circuit", None)
    if not callable(factory):
        raise AutoConfigError(f"Topology {path} does not expose build_circuit()")
    return factory


def _numeric_nominal(value: Any) -> float | int | None:
    """Return a finite SI scalar, including native PySpice UnitValue objects."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _add_row_nominal(
    result: dict[str, float | int],
    row: Any,
    value: Any,
) -> bool:
    numeric = _numeric_nominal(value)
    if numeric is None:
        return False
    result.setdefault(row.parameter_id, numeric)
    if row.parameter_name:
        result.setdefault(row.parameter_name, numeric)
    return True


def _topology_bound_nominals(
    rows: Sequence[Any],
    result: dict[str, float | int],
    topology_factory: Any | None,
) -> None:
    """Read remaining direct bindings from a topology whose args are resolvable."""

    if topology_factory is None:
        return
    signature = inspect.signature(topology_factory)
    kwargs: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            continue
        if name in result:
            kwargs[name] = result[name]
        elif parameter.default is not inspect.Parameter.empty:
            # Preserve native unit objects while building; float conversion is
            # applied only when the bound element value is transported to AL.
            kwargs[name] = parameter.default
        else:
            return
    try:
        circuit = topology_factory(**kwargs)
    except Exception:
        return
    for row in rows:
        if not row.optimization_eligible or row.parameter_id in result:
            continue
        field_pair = _FIELD_MAP.get(row.parameter_kind.strip().lower())
        if field_pair is None or not row.device_id:
            continue
        try:
            element = circuit[row.device_id]
            value = getattr(element, field_pair[1])
        except Exception:
            continue
        _add_row_nominal(result, row, value)


def resolve_nominal_values(
    rows: Sequence[Any],
    parameter_path: str | Path,
    *,
    circuit_id: str,
    circuit_type: str,
    circuit_ir: Any | None = None,
    topology_factory: Any | None = None,
    stage_b_nominals: Mapping[str, Any] | None = None,
) -> dict[str, float | int]:
    """Resolve optimization nominals using existing Stage B SI transport.

    Precedence is explicit Stage B nominal, the existing Stage B real-circuit
    nominal context (operating/source/device records), build-function defaults,
    then a directly readable topology-bound element value.
    """

    result: dict[str, float | int] = {}
    explicit = stage_b_nominals or {}
    for row in rows:
        for key in (row.parameter_name, row.parameter_id):
            if key is not None and key in explicit:
                if _add_row_nominal(result, row, explicit[key]):
                    break

    # Nominal transport is also needed for bound, non-optimization arguments
    # (for example a fixed degeneration resistor).  Stage B intentionally
    # populates only eligible rows, so present an eligibility-only view here;
    # this does not change the real Stage A rows or the optimization space.
    nominal_rows = tuple(
        replace(row, optimization_eligible=True)
        if row.binding_status == "BOUND"
        else row
        for row in rows
    )
    context, _operating, _sources, _missing = build_stage_b_nominal_context(
        ValidationCircuit(circuit_id, circuit_type, "AutoConfig nominal transport"),
        nominal_rows,
        Path(parameter_path),
    )
    for row in rows:
        if row.parameter_id in result:
            continue
        for key in (row.parameter_id, row.parameter_name):
            if key is not None and key in context.nominal_values:
                if _add_row_nominal(result, row, context.nominal_values[key]):
                    break

    if topology_factory is not None:
        signature = inspect.signature(topology_factory)
        for row in rows:
            if row.parameter_id in result or not row.parameter_name:
                continue
            parameter = signature.parameters.get(row.parameter_name)
            if parameter is not None and parameter.default is not inspect.Parameter.empty:
                _add_row_nominal(result, row, parameter.default)

    # A Stage A literal is relevant only when a future extension deliberately
    # marks that target eligible.  Numeric literals are safe to transport;
    # arbitrary expressions are never evaluated here.
    if circuit_ir is not None:
        for row in rows:
            if row.parameter_id in result:
                continue
            try:
                expression = circuit_ir.target(row.parameter_id).source_expression
            except KeyError:
                continue
            if expression.expression_type == "literal":
                _add_row_nominal(
                    result,
                    row,
                    expression.resolved_text or expression.source_text,
                )

    _topology_bound_nominals(rows, result, topology_factory)
    missing = sorted(
        row.parameter_name or row.parameter_id
        for row in rows
        if row.optimization_eligible and row.parameter_id not in result
    )
    if missing:
        raise AutoConfigError(
            "Cannot resolve nominal values for optimization parameters: "
            + ", ".join(missing)
        )
    return result


def _normalize_model_parameters(payload: Mapping[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for name, value in payload.items():
        if name == "name":
            continue
        if name == "KP_uA_per_V2":
            parameters["KP"] = float(value) * 1.0e-6
        elif name.endswith("_per_V"):
            parameters[name[:-6].upper()] = value
        elif name.endswith("_V"):
            parameters[name[:-2].upper()] = value
        else:
            parameters[str(name).upper()] = value
    return parameters


def _model_definitions(ideal_payload: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    definitions = []
    type_names = {
        "nmos": "NMOS",
        "pmos": "PMOS",
        "npn": "NPN",
        "pnp": "PNP",
    }
    for family, payload in (ideal_payload.get("models") or {}).items():
        if not isinstance(payload, Mapping) or payload.get("name") is None:
            continue
        model_type = type_names.get(str(family).casefold())
        if model_type is None:
            continue
        definitions.append(
            {
                "family": str(family).casefold(),
                "name": str(payload["name"]),
                "model_type": model_type,
                "parameters": _normalize_model_parameters(payload),
            }
        )
    for model_name, payload in (ideal_payload.get("model_defaults") or {}).items():
        if not isinstance(payload, Mapping):
            continue
        folded_name = str(model_name).casefold()
        family = next((name for name in type_names if name in folded_name), None)
        if family is None:
            continue
        definitions.append(
            {
                "family": family,
                "name": str(model_name),
                "model_type": type_names[family],
                "parameters": _normalize_model_parameters(payload),
            }
        )
    for model_name, payload in (ideal_payload.get("model_parameters") or {}).items():
        if not isinstance(payload, Mapping):
            continue
        folded_name = str(model_name).casefold()
        family = next((name for name in type_names if name in folded_name), None)
        if family is None:
            continue
        parameters = {
            str(name).upper(): value
            for name, value in payload.items()
            if name != "model_type" and not str(name).casefold().endswith("_unit")
        }
        kp_unit = str(payload.get("KP_unit", "")).casefold()
        if "KP" in parameters and "ua" in kp_unit:
            parameters["KP"] = float(parameters["KP"]) * 1.0e-6
        definitions.append(
            {
                "family": family,
                "name": str(model_name),
                "model_type": type_names[family],
                "parameters": parameters,
            }
        )
    return tuple(definitions)


def _resolve_model_argument(
    argument: str,
    definitions: Sequence[Mapping[str, Any]],
) -> str:
    matches = [
        item for item in definitions if str(item["family"]).upper() in argument.upper()
    ]
    if len(matches) == 1:
        return str(matches[0]["name"])
    if len(definitions) == 1:
        return str(definitions[0]["name"])
    raise AutoConfigError(
        f"Cannot resolve model build argument {argument!r}; available model families="
        f"{[item['family'] for item in definitions]!r}"
    )


def _source_candidates(source: Any) -> tuple[str, ...]:
    prefix = "I" if source.source_type == "current_source" else "V"
    values = (
        source.source_id,
        source.original_source_id,
        f"{prefix}{source.source_id}",
        f"{prefix}{source.original_source_id}",
    )
    return tuple(dict.fromkeys(str(value) for value in values if value))


def _resolve_element_name(circuit: Any, candidates: Sequence[str], role: str) -> str:
    actual = {str(name).casefold(): str(name) for name in circuit.element_names}
    matches = [actual[item.casefold()] for item in candidates if item.casefold() in actual]
    unique = tuple(dict.fromkeys(matches))
    if len(unique) != 1:
        raise BindingValidationError(
            f"Cannot resolve {role} element; candidates={list(candidates)!r}; "
            f"matches={list(unique)!r}"
        )
    return unique[0]


def _diode_connected_nodes(circuit_ir: Any) -> set[str]:
    result = set()
    for device in circuit_ir.devices:
        terminals = {key.casefold(): value for key, value in device.terminals.items()}
        drain = terminals.get("drain")
        gate = terminals.get("gate")
        collector = terminals.get("collector")
        base = terminals.get("base")
        if drain is not None and gate is not None and drain.casefold() == gate.casefold():
            result.add(drain.casefold())
        if (
            collector is not None
            and base is not None
            and collector.casefold() == base.casefold()
        ):
            result.add(collector.casefold())
    return result


def _non_ground_node(source: Any) -> str | None:
    nodes = [str(value) for value in source.terminals.values()]
    non_ground = [value for value in nodes if value.casefold() not in {"0", "gnd", "vss"}]
    return non_ground[0] if len(non_ground) == 1 else None


def _transistor_roles(device: Any) -> tuple[str, str, str, str, str] | None:
    """Return family, current terminal, control terminal, return terminal, id key."""

    terminals = {key.casefold(): str(value) for key, value in device.terminals.items()}
    device_type = str(device.device_type).casefold()
    if {"drain", "gate", "source"}.issubset(terminals):
        return device_type, terminals["drain"], terminals["gate"], terminals["source"], "id"
    if {"collector", "base", "emitter"}.issubset(terminals):
        return (
            device_type,
            terminals["collector"],
            terminals["base"],
            terminals["emitter"],
            "ic",
        )
    return None


def resolve_reference_current_role(
    circuit_ir: Any,
    output_source: Any,
) -> tuple[str, Any, tuple[str, ...]]:
    """Resolve a measurable CurrentMirror reference path from topology semantics."""

    sources = [item for item in circuit_ir.sources if item.source_type == "current_source"]
    if sources:
        diode_nodes = _diode_connected_nodes(circuit_ir)
        structural = [
            item
            for item in sources
            if _non_ground_node(item) is not None
            and _non_ground_node(item).casefold() in diode_nodes
        ]
        if len(structural) == 1:
            return "source_current", structural[0], (
                "explicit current source feeds a diode-connected reference node",
            )
        if len(sources) == 1:
            return "source_current", sources[0], (
                "only explicit current source in topology",
            )
        named = [
            item
            for item in sources
            if any(token in item.physical_target.upper() for token in ("REF", "IREF", "IIN"))
        ]
        if len(named) == 1:
            return "source_current", named[0], (
                "explicit current source has a unique REF/IREF/IIN role",
            )
        raise SemanticMappingError(
            "Cannot determine a unique CurrentMirror reference current source; candidates="
            + ", ".join(item.physical_target for item in sources)
        )

    output_node = _non_ground_node(output_source)
    if output_node is None:
        raise SemanticMappingError(
            "Cannot determine output node while resolving CurrentMirror reference branch"
        )
    output_node_folded = output_node.casefold()
    devices_with_roles = [
        (device, roles)
        for device in circuit_ir.devices
        if (roles := _transistor_roles(device)) is not None
    ]
    output_devices = [
        (device, roles)
        for device, roles in devices_with_roles
        if roles[1].casefold() == output_node_folded
    ]
    diode_devices = [
        (device, roles)
        for device, roles in devices_with_roles
        if roles[1].casefold() == roles[2].casefold()
    ]
    matched: dict[str, tuple[Any, tuple[str, str, str, str, str], set[str]]] = {}
    for reference_device, reference_roles in diode_devices:
        family, _branch, control, return_node, _current_key = reference_roles
        peers = {
            output_device.device_id
            for output_device, output_roles in output_devices
            if output_device.device_id != reference_device.device_id
            and output_roles[0] == family
            and output_roles[2].casefold() == control.casefold()
            and output_roles[3].casefold() == return_node.casefold()
        }
        if peers:
            matched[reference_device.device_id] = (
                reference_device,
                reference_roles,
                peers,
            )
    if len(matched) == 1:
        reference_device, _roles, peers = next(iter(matched.values()))
        return "device_current", reference_device, (
            "reference transistor is diode-connected (control terminal equals current terminal)",
            "reference transistor shares device family, control node, and return rail "
            "with output branch device(s): " + ", ".join(sorted(peers)),
        )
    if len(matched) > 1:
        raise SemanticMappingError(
            "Multiple diode-connected CurrentMirror reference branches match the output "
            "gate network: " + ", ".join(sorted(matched))
        )
    diode_names = ", ".join(sorted(device.device_id for device, _roles in diode_devices))
    output_names = ", ".join(sorted(device.device_id for device, _roles in output_devices))
    raise SemanticMappingError(
        "No diode-connected reference branch uniquely drives the output mirror gate; "
        f"diode_candidates=[{diode_names}], output_branch_devices=[{output_names}]"
    )


def resolve_reference_excitation_role(
    circuit_ir: Any,
    reference_type: str,
    reference_role: Any,
    output_source: Any,
) -> dict[str, Any]:
    """Resolve an external source that can excite the measured reference branch.

    Candidate ranking is based only on electrical connectivity.  A source that
    directly drives the diode-connected branch/control node outranks a source
    that only drives the reference device return rail.  Multiple candidates at
    the best evidence tier remain ambiguous.
    """

    if reference_type == "source_current":
        return {
            "status": "resolved",
            "source": reference_role,
            "candidates": (
                {
                    "source": reference_role,
                    "priority": 0,
                    "topology_role": "explicit_reference_source",
                    "evidence": (
                        "reference current is measured on an independent current source",
                        "the same independent source is directly controllable",
                    ),
                },
            ),
            "evidence": (
                "explicit reference source is both measurement branch and excitation",
            ),
            "reason": None,
        }

    roles = _transistor_roles(reference_role)
    if reference_type != "device_current" or roles is None:
        return {
            "status": "unresolved",
            "source": None,
            "candidates": (),
            "evidence": (),
            "reason": "reference measurement has no topology-resolvable source role",
        }

    _family, branch_node, control_node, return_node, _current_key = roles
    direct_nodes = {branch_node.casefold(), control_node.casefold()}
    return_node_folded = return_node.casefold()
    candidates = []
    for source in circuit_ir.sources:
        if source.physical_target == output_source.physical_target:
            continue
        source_nodes = {
            str(value).casefold()
            for value in source.terminals.values()
            if str(value).casefold() not in {"0", "gnd", "vss"}
        }
        direct_match = sorted(source_nodes & direct_nodes)
        if direct_match:
            evidence = (
                "independent source directly drives reference branch/control node(s): "
                + ", ".join(direct_match),
                "candidate is connected to the diode-connected reference device",
            )
            priority = 0
            topology_role = "reference_branch_or_control_driver"
        elif return_node_folded in source_nodes:
            evidence = (
                "independent source directly drives reference return node "
                f"{return_node!r}",
                "supply/return excitation is considered only because no direct "
                "reference branch/control driver was found",
            )
            priority = 1
            topology_role = "reference_return_driver"
        else:
            continue
        candidates.append(
            {
                "source": source,
                "priority": priority,
                "topology_role": topology_role,
                "evidence": evidence,
            }
        )

    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item["priority"],
                str(item["source"].physical_target),
            ),
        )
    )
    if not ordered:
        return {
            "status": "unresolved",
            "source": None,
            "candidates": (),
            "evidence": (),
            "reason": (
                "no independent source directly drives the reference branch, "
                "control node, or return node"
            ),
        }
    best_priority = ordered[0]["priority"]
    best = tuple(item for item in ordered if item["priority"] == best_priority)
    if len(best) != 1:
        return {
            "status": "ambiguous",
            "source": None,
            "candidates": ordered,
            "evidence": (),
            "reason": (
                "multiple independent sources have equal topology evidence for "
                "reference excitation"
            ),
        }
    selected = best[0]
    return {
        "status": "resolved",
        "source": selected["source"],
        "candidates": ordered,
        "evidence": selected["evidence"]
        + ("only source at the strongest available topology-evidence tier",),
        "reason": None,
    }


def _resolve_output_source(circuit_ir: Any, rows_by_id: Mapping[str, Any]) -> Any:
    voltage_sources = [
        item for item in circuit_ir.sources if item.source_type == "voltage_source"
    ]
    operating = [
        item
        for item in voltage_sources
        if rows_by_id.get(item.physical_target) is not None
        and rows_by_id[item.physical_target].semantic_category == "operating_condition"
    ]
    candidates = operating or voltage_sources
    if len(candidates) == 1:
        return candidates[0]
    primary = [
        item
        for item in candidates
        if item.physical_target.upper() in {"IOUT1", "VOUT1"}
    ]
    if len(primary) == 1:
        return primary[0]
    raise SemanticMappingError(
        "Cannot determine a unique CurrentMirror output-compliance source; candidates="
        + ", ".join(item.physical_target for item in candidates)
    )


def _vout_nominal(output_parameter: str, ideal_payload: Mapping[str, Any]) -> float:
    operating = _operating_conditions(ideal_payload)
    if output_parameter in operating:
        return float(operating[output_parameter])
    vdd = operating.get("VDD")
    vss = operating.get("VSS", 0.0)
    if vdd is not None:
        return 0.5 * (float(vdd) + float(vss))
    raise SemanticMappingError(
        f"Cannot determine nominal output compliance for {output_parameter!r}; "
        "provide it in operating_parameters or provide VDD/VSS rails"
    )


def _topology_arguments(
    factory: Any,
    *,
    optimization_nominals: Mapping[str, float | int],
    all_nominals: Mapping[str, float | int],
    output_parameter: str | None,
    vout_nominal: float | None,
    model_definitions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    signature = inspect.signature(factory)
    nominal_arguments: dict[str, Any] = {}
    fixed_arguments: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            continue
        if name in optimization_nominals:
            value = optimization_nominals[name]
        elif output_parameter is not None and name == output_parameter:
            assert vout_nominal is not None
            value = vout_nominal
            fixed_arguments[name] = value
        elif name in all_nominals:
            value = all_nominals[name]
            fixed_arguments[name] = value
        elif name.upper().endswith("_MODEL"):
            value = _resolve_model_argument(name, model_definitions)
            fixed_arguments[name] = value
        elif parameter.default is not inspect.Parameter.empty:
            value = parameter.default
            fixed_arguments[name] = value
        else:
            raise AutoConfigError(
                f"Cannot resolve required topology argument {name!r} from Stage A/B "
                "bindings or ideal working conditions"
            )
        nominal_arguments[name] = value
    return nominal_arguments, fixed_arguments


def _runtime_parameters(
    search_space: Any,
    circuit_ir: Any,
    source_elements: Mapping[str, str],
) -> tuple[RuntimeParameter, ...]:
    targets = {item.parameter_id: item for item in circuit_ir.physical_targets}
    result = []
    unresolved = []
    for variable in search_space.variables:
        if variable.domain_type.value != "continuous":
            unresolved.append(
                f"{variable.variable_id}: domain {variable.domain_type.value!r} is not "
                "supported by the current AL sampler"
            )
            continue
        if (
            variable.range.lower is None
            or variable.range.upper is None
            or variable.nominal_value is None
        ):
            unresolved.append(
                f"{variable.variable_id}: Stage B did not provide nominal/lower/upper"
            )
            continue
        bindings = []
        for parameter_id in variable.members:
            target = targets.get(parameter_id)
            if target is None or target.binding is None:
                unresolved.append(f"{variable.variable_id}: missing Stage A target {parameter_id}")
                continue
            kind = target.parameter_kind.strip().lower()
            field_pair = _FIELD_MAP.get(kind)
            if field_pair is None:
                unresolved.append(
                    f"{variable.variable_id}: unsupported binding field {target.parameter_kind!r}"
                )
                continue
            if target.source_kind == "source_interface":
                element_name = source_elements.get(target.parameter_id)
            else:
                element_name = target.device_id
            if not element_name:
                unresolved.append(
                    f"{variable.variable_id}: no executable instance for {parameter_id}"
                )
                continue
            expression = (target.binding.binding_expression or "").strip()
            direct_value = expression == variable.variable_id
            bindings.append(
                RuntimeBinding(
                    parameter_id=parameter_id,
                    instance=str(target.device_id or element_name),
                    field=field_pair[0],
                    pyspice_attribute=field_pair[1],
                    element_name=str(element_name),
                    source_kind=target.source_kind,
                    direct_value=direct_value,
                )
            )
        result.append(
            RuntimeParameter(
                name=variable.variable_id,
                semantic_category=variable.semantic_category,
                nominal=variable.nominal_value,
                lower=variable.range.lower,
                upper=variable.range.upper,
                variable_type=variable.domain_type.value,
                unit=variable.unit,
                scale=variable.range.scale,
                transform=variable.range.transform,
                fixed_sign=variable.range.fixed_sign,
                bindings=tuple(bindings),
            )
        )
    if unresolved:
        raise BindingValidationError("; ".join(unresolved))
    return tuple(result)


def _build_param_template(
    *,
    circuit_id: str,
    circuit_type: str,
    ideal_payload: Mapping[str, Any],
    runtime_parameters: Sequence[RuntimeParameter],
    output_source: Any,
    vout_nominal: float,
    model_definitions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    parameters = []
    bindings = []
    for parameter in runtime_parameters:
        first = parameter.bindings[0]
        parameters.append(
            {
                "name": parameter.name,
                "parameter_id": (
                    first.parameter_id
                    if len(parameter.bindings) == 1
                    else parameter.name
                ),
                "declared_name": parameter.name,
                "semantic_category": parameter.semantic_category,
                "source_kind": first.source_kind,
                "device_instance": first.instance,
                "topology_element": first.element_name,
                "parameter_kind": first.field,
                "validity_status": "VALID",
                "binding_status": "BOUND",
                "optimization_eligible": True,
                "binding": {
                    "status": "BOUND",
                    "targets": [
                        {
                            "parameter_id": item.parameter_id,
                            "instance": item.instance,
                            "field": item.field,
                            "element": item.element_name,
                        }
                        for item in parameter.bindings
                    ],
                },
                "nominal_value": parameter.nominal,
                "unit": parameter.unit,
                "variable_type": parameter.variable_type,
                "admissible_range": {
                    "lower": parameter.lower,
                    "upper": parameter.upper,
                    "scale": parameter.scale,
                    "transform": parameter.transform,
                    "fixed_sign": parameter.fixed_sign,
                },
                "discrete_options": [],
                "range_status": "VALID",
                "range_source": "STAGE_B_IDEAL",
            }
        )
        bindings.append(
            {
                "binding_id": f"bind_{parameter.name}",
                "relation_type": (
                    "identity" if len(parameter.bindings) == 1 else "shared_parameter"
                ),
                "members": [item.parameter_id for item in parameter.bindings],
                "expression": f"Stage A root argument: {parameter.name}",
                "independent_variable": parameter.name,
                "dependent_variables": [],
            }
        )

    parameters.append(
        {
            "name": output_source.physical_target,
            "parameter_id": output_source.physical_target,
            "declared_name": output_source.physical_target,
            "semantic_category": "operating_condition",
            "source_kind": "source_interface",
            "device_instance": output_source.source_id,
            "topology_element": output_source.source_id,
            "device_type": "voltage_source",
            "parameter_kind": "voltage",
            "validity_status": "VALID",
            "binding_status": "BOUND",
            "optimization_eligible": False,
            "nominal_value": vout_nominal,
            "unit": "V",
            "variable_type": "fixed",
            "admissible_range": {
                "lower": None,
                "upper": None,
                "scale": None,
                "transform": None,
                "fixed_sign": None,
            },
            "discrete_options": [],
            "range_status": "NOT_APPLICABLE",
            "range_source": None,
        }
    )
    for model in model_definitions:
        parameters.append(
            {
                "name": f"{str(model['family']).upper()}_MODEL",
                "parameter_id": f"{str(model['family']).upper()}_MODEL",
                "declared_name": f"{str(model['family']).upper()}_MODEL",
                "semantic_category": "constraint_or_process",
                "source_kind": "model",
                "device_instance": None,
                "topology_element": None,
                "device_type": None,
                "parameter_kind": "model_family",
                "validity_status": "VALID",
                "binding_status": "BOUND",
                "optimization_eligible": False,
                "nominal_value": model["name"],
                "unit": "unitless",
                "variable_type": "categorical",
                "admissible_range": {
                    "lower": None,
                    "upper": None,
                    "scale": None,
                    "transform": None,
                    "fixed_sign": None,
                },
                "discrete_options": [model["name"]],
                "range_status": "NOT_APPLICABLE",
                "range_source": None,
            }
        )
    subtype = (
        (ideal_payload.get("circuit") or {}).get("actual_circuit_type")
        or (ideal_payload.get("circuit") or {}).get("topology_name")
    )
    variables = [item.name for item in runtime_parameters]
    return {
        "schema_version": "parameter_template_v1",
        "circuit": {
            "circuit_id": circuit_id,
            "circuit_type": circuit_type,
            "circuit_subtype": subtype,
            "simulation_environment": "ideal",
        },
        "parameters": parameters,
        "bindings": bindings,
        "constraints": [
            {
                "constraint_id": "constraint_output_compliance_not_in_X",
                "type": "operating_condition_exclusion",
                "expression": f"{output_source.physical_target} not in optimization_space.variables",
                "source": "Stage A semantic_category=operating_condition",
                "applies_to": [output_source.physical_target],
            }
        ],
        "optimization_space": {
            "variables": variables,
            "dimension_before_binding": len(variables),
            "effective_dimension_after_binding": len(variables),
        },
        "validation": {
            "all_optimization_parameters_have_ranges": True,
            "all_units_resolved": True,
            "all_bindings_explicit": True,
            "operating_conditions_excluded_from_X": True,
            "unresolved_items": [],
        },
        "provenance": {
            "stage_a": "param_extraction.stage_a frozen runtime pipeline",
            "stage_b": "param_extraction.stage_b IdealRangeProvider v1 frozen runtime pipeline",
            "generated_by": "ActiveLearning.onboarding.AutoConfigBuilder",
        },
    }


def _operating_conditions(
    ideal_payload: Mapping[str, Any],
) -> dict[str, float | int]:
    """Transport explicit dataset operating values without synthesizing VCM."""

    result: dict[str, float | int] = {}
    raw = ideal_payload.get("operating_parameters") or {}
    if not isinstance(raw, Mapping):
        return result
    for name, record in raw.items():
        value = record.get("value") if isinstance(record, Mapping) else record
        numeric = _numeric_nominal(value)
        if numeric is not None:
            result[str(name).removesuffix("_DC")] = numeric
    return result


def _build_opamp_param_template(
    *,
    circuit_id: str,
    circuit_type: str,
    ideal_payload: Mapping[str, Any],
    runtime_parameters: Sequence[RuntimeParameter],
    fixed_arguments: Mapping[str, Any],
    rows_by_id: Mapping[str, Any],
    source_elements: Mapping[str, str],
    model_definitions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the generated OpAmp template solely from Stage A/B structure."""

    parameters = []
    bindings = []
    for parameter in runtime_parameters:
        first = parameter.bindings[0]
        parameters.append(
            {
                "name": parameter.name,
                "parameter_id": (
                    first.parameter_id
                    if len(parameter.bindings) == 1
                    else parameter.name
                ),
                "declared_name": parameter.name,
                "semantic_category": parameter.semantic_category,
                "source_kind": first.source_kind,
                "device_instance": first.instance,
                "topology_element": first.element_name,
                "parameter_kind": first.field,
                "validity_status": "VALID",
                "binding_status": "BOUND",
                "optimization_eligible": True,
                "binding": {
                    "status": "BOUND",
                    "targets": [
                        {
                            "parameter_id": item.parameter_id,
                            "instance": item.instance,
                            "field": item.field,
                            "element": item.element_name,
                        }
                        for item in parameter.bindings
                    ],
                },
                "nominal_value": parameter.nominal,
                "unit": parameter.unit,
                "variable_type": parameter.variable_type,
                "admissible_range": {
                    "lower": parameter.lower,
                    "upper": parameter.upper,
                    "scale": parameter.scale,
                    "transform": parameter.transform,
                    "fixed_sign": parameter.fixed_sign,
                },
                "discrete_options": [],
                "range_status": "VALID",
                "range_source": "STAGE_B_IDEAL",
            }
        )
        bindings.append(
            {
                "binding_id": f"bind_{parameter.name}",
                "relation_type": (
                    "identity" if len(parameter.bindings) == 1 else "shared_parameter"
                ),
                "members": [item.parameter_id for item in parameter.bindings],
                "expression": f"Stage A root argument: {parameter.name}",
                "independent_variable": parameter.name,
                "dependent_variables": [],
            }
        )

    rows_by_name = {
        row.parameter_name: row
        for row in rows_by_id.values()
        if row.parameter_name is not None
    }
    for name, value in fixed_arguments.items():
        if name.upper().endswith("_MODEL"):
            continue
        row = rows_by_name.get(name)
        parameters.append(
            {
                "name": name,
                "parameter_id": name if row is None else row.parameter_id,
                "declared_name": name,
                "semantic_category": (
                    "fixed_topology_argument"
                    if row is None
                    else row.semantic_category
                ),
                "source_kind": None if row is None else row.source_kind,
                "device_instance": None if row is None else row.device_id,
                "topology_element": source_elements.get(name),
                "parameter_kind": None if row is None else row.parameter_kind,
                "validity_status": "VALID",
                "binding_status": "BOUND",
                "optimization_eligible": False,
                "nominal_value": _numeric_nominal(value),
                "unit": (
                    None
                    if row is None
                    else {
                        "voltage": "V",
                        "bias_voltage": "V",
                        "current": "A",
                        "bias_current": "A",
                    }.get(str(row.parameter_kind).casefold())
                ),
                "variable_type": "fixed",
                "admissible_range": {
                    "lower": None,
                    "upper": None,
                    "scale": None,
                    "transform": None,
                    "fixed_sign": None,
                },
                "discrete_options": [],
                "range_status": "NOT_APPLICABLE",
                "range_source": None,
            }
        )
    for model in model_definitions:
        parameters.append(
            {
                "name": f"{str(model['family']).upper()}_MODEL",
                "parameter_id": f"{str(model['family']).upper()}_MODEL",
                "declared_name": f"{str(model['family']).upper()}_MODEL",
                "semantic_category": "constraint_or_process",
                "source_kind": "model",
                "device_instance": None,
                "topology_element": None,
                "parameter_kind": "model_family",
                "validity_status": "VALID",
                "binding_status": "BOUND",
                "optimization_eligible": False,
                "nominal_value": model["name"],
                "unit": "unitless",
                "variable_type": "categorical",
                "admissible_range": {
                    "lower": None,
                    "upper": None,
                    "scale": None,
                    "transform": None,
                    "fixed_sign": None,
                },
                "discrete_options": [model["name"]],
                "range_status": "NOT_APPLICABLE",
                "range_source": None,
            }
        )
    subtype = (
        (ideal_payload.get("circuit") or {}).get("actual_circuit_type")
        or (ideal_payload.get("circuit") or {}).get("topology_name")
        or ideal_payload.get("circuit_type")
    )
    variables = [item.name for item in runtime_parameters]
    fixed_names = [
        item["name"]
        for item in parameters
        if item["optimization_eligible"] is False
        and item["source_kind"] != "model"
    ]
    return {
        "schema_version": "parameter_template_v1",
        "circuit": {
            "circuit_id": circuit_id,
            "circuit_type": circuit_type,
            "circuit_subtype": subtype,
            "simulation_environment": "ideal",
        },
        "parameters": parameters,
        "bindings": bindings,
        "constraints": [
            {
                "constraint_id": "constraint_operating_conditions_not_in_X",
                "type": "operating_condition_exclusion",
                "expression": "fixed Stage A operating conditions not in optimization_space.variables",
                "source": "Stage A/B semantic and eligibility classification",
                "applies_to": fixed_names,
            }
        ],
        "optimization_space": {
            "variables": variables,
            "dimension_before_binding": len(variables),
            "effective_dimension_after_binding": len(variables),
        },
        "validation": {
            "all_optimization_parameters_have_ranges": True,
            "all_units_resolved": True,
            "all_bindings_explicit": True,
            "operating_conditions_excluded_from_X": True,
            "unresolved_items": [],
        },
        "provenance": {
            "stage_a": "param_extraction.stage_a frozen runtime pipeline",
            "stage_b": "param_extraction.stage_b IdealRangeProvider v1 frozen runtime pipeline",
            "generated_by": "ActiveLearning.onboarding.AutoConfigBuilder",
        },
    }


def _anchored_topology_factory(
    factory: Any,
    *,
    reference_voltage_v: float | None,
    resistance_ohm: float | None,
) -> Any:
    """Decorate numerical floating nodes using explicit operating conditions."""

    if reference_voltage_v is None or resistance_ohm is None:
        return factory
    reference = float(reference_voltage_v)
    resistance = float(resistance_ohm)

    def build(**kwargs):
        circuit = factory(**kwargs)
        circuit.V("AL_REF_ANCHOR", "AL_REF_ANCHOR", circuit.gnd, reference)
        fixed_nodes = {"0", "gnd", "vss", "al_ref_anchor"}
        for element_name in list(circuit.element_names):
            if str(element_name)[:1].casefold() in {"v", "i"}:
                fixed_nodes.update(
                    str(node).casefold() for node in circuit[element_name].node_names
                )
        anchor_nodes = [
            str(node)
            for node in list(circuit.node_names)
            if str(node).casefold() not in fixed_nodes
        ]
        for index, node in enumerate(sorted(anchor_nodes, key=str.casefold)):
            circuit.R(f"AL_ANCHOR{index}", node, "AL_REF_ANCHOR", resistance)
        return circuit

    return build


def _build_current_mirror_circuit_config(
    circuit_type: str,
    circuit_id: str,
    *,
    repository_root: str | Path | None = None,
    runtime_provider: Any,
) -> ActiveLearningCircuitConfig:
    """Run frozen Stage A/B and build an executable CurrentMirror config."""

    root = Path(repository_root).resolve() if repository_root else _REPOSITORY_ROOT
    circuit_root = root / "dataset" / circuit_id
    topology_path = circuit_root / "topology" / f"{circuit_id}.py"
    ideal_parameter_path = circuit_root / "simulation" / "parameter" / "ideal_param.yaml"
    if not topology_path.is_file():
        raise AutoConfigError(f"PySpice topology not found: {topology_path}")
    if not ideal_parameter_path.is_file():
        raise AutoConfigError(f"Ideal working conditions not found: {ideal_parameter_path}")

    ideal_payload = _load_yaml(ideal_parameter_path)
    factory = _load_topology_factory(topology_path, circuit_id)
    circuit_ir = resolve_bindings(
        parse_pyspice_file(
            topology_path,
            circuit_id=circuit_id,
            circuit_type=circuit_type,
        )
    )
    stage_a_rows = build_stage_a_output_rows(
        circuit_ir,
        validity=validate_candidates(circuit_ir),
    )
    nominal_map = resolve_nominal_values(
        stage_a_rows,
        ideal_parameter_path,
        circuit_id=circuit_id,
        circuit_type=circuit_type,
        circuit_ir=circuit_ir,
        topology_factory=factory,
    )
    operating = ideal_payload.get("operating_parameters") or {}
    search_space = construct_admissible_search_space(
        stage_a_rows,
        circuit_ir=circuit_ir,
        circuit_context=CircuitContext(
            circuit_id,
            circuit_type,
            nominal_values=nominal_map,
        ),
        operating_context=OperatingContext(
            vdd=operating.get("VDD"),
            vss=operating.get("VSS"),
            temperature=25.0,
            values=operating,
        ),
        technology_context=TechnologyContext("ideal"),
        range_provider=IdealRangeProvider(),
    )

    rows_by_id = {row.parameter_id: row for row in stage_a_rows}
    output_source = _resolve_output_source(circuit_ir, rows_by_id)
    reference_type, reference_role, reference_evidence = resolve_reference_current_role(
        circuit_ir,
        output_source,
    )
    output_parameter = output_source.physical_target
    nominal_vout = _vout_nominal(output_parameter, ideal_payload)
    models = _model_definitions(ideal_payload)
    optimization_nominals = {
        variable.variable_id: variable.nominal_value for variable in search_space.variables
    }
    nominal_arguments, fixed_arguments = _topology_arguments(
        factory,
        optimization_nominals=optimization_nominals,
        all_nominals=nominal_map,
        output_parameter=output_parameter,
        vout_nominal=nominal_vout,
        model_definitions=models,
    )
    try:
        nominal_circuit = factory(**nominal_arguments)
    except Exception as error:
        raise BindingValidationError(
            f"{circuit_id}: nominal topology build failed during AutoConfig: {error}"
        ) from error

    source_elements = {
        source.physical_target: _resolve_element_name(
            nominal_circuit,
            _source_candidates(source),
            f"Stage A source {source.physical_target}",
        )
        for source in circuit_ir.sources
    }
    excitation_resolution = resolve_reference_excitation_role(
        circuit_ir,
        reference_type,
        reference_role,
        output_source,
    )
    excitation_candidates = []
    for candidate in excitation_resolution["candidates"]:
        source = candidate["source"]
        element = source_elements[source.physical_target]
        row = rows_by_id.get(source.physical_target)
        try:
            nominal_excitation = float(nominal_circuit[element].dc_value)
        except Exception as error:
            raise BindingValidationError(
                f"{circuit_id}: cannot read nominal excitation source {element!r}"
            ) from error
        excitation_candidates.append(
            SemanticExcitationCandidate(
                excitation_type=(
                    "current"
                    if source.source_type == "current_source"
                    else "voltage"
                ),
                element=element,
                parameter=(None if row is None else row.parameter_name),
                nominal=nominal_excitation,
                topology_role=candidate["topology_role"],
                evidence=tuple(candidate["evidence"]),
            )
        )
    resolved_source = excitation_resolution["source"]
    resolved_candidate = next(
        (
            item
            for item in excitation_candidates
            if resolved_source is not None
            and item.element == source_elements[resolved_source.physical_target]
        ),
        None,
    )
    reference_excitation = SemanticReferenceExcitation(
        status=excitation_resolution["status"],
        excitation_type=(
            None if resolved_candidate is None else resolved_candidate.excitation_type
        ),
        element=None if resolved_candidate is None else resolved_candidate.element,
        parameter=None if resolved_candidate is None else resolved_candidate.parameter,
        nominal=None if resolved_candidate is None else resolved_candidate.nominal,
        evidence=tuple(excitation_resolution["evidence"]),
        candidates=tuple(excitation_candidates),
        reason=excitation_resolution["reason"],
    )
    runtime_parameters = _runtime_parameters(search_space, circuit_ir, source_elements)
    if reference_type == "source_current":
        reference_row = rows_by_id.get(reference_role.physical_target)
        if reference_row is None or reference_row.parameter_name is None:
            raise SemanticMappingError(
                f"Reference source {reference_role.physical_target} has no Stage A binding"
            )
        reference_parameter = reference_row.parameter_name
        reference_element = source_elements[reference_role.physical_target]
        reference_measurement_name = f"@{reference_element}[current]"
        scale_names = tuple(
            item.name
            for item in runtime_parameters
            if item.semantic_category == "bias_parameter"
            and any(binding.source_kind == "source_interface" for binding in item.bindings)
        )
        reference_is_optimized = reference_parameter in {
            item.name for item in runtime_parameters
        }
        reference_status_evidence = (
            "reference source is an optimization variable"
            if reference_is_optimized
            else "reference source is a fixed Stage A operating condition"
        )
    else:
        reference_parameter = None
        reference_element = reference_role.device_id
        roles = _transistor_roles(reference_role)
        assert roles is not None
        reference_measurement_name = f"@{reference_element}[{roles[4]}]"
        scale_names = ()
        reference_status_evidence = (
            "reference current is measured on a topology-derived transistor branch"
        )
    output_element = source_elements[output_source.physical_target]
    reference_measurement = SemanticCurrentMeasurement(
        measurement_type=reference_type,
        element=reference_element,
        op_section="internal_parameters",
        op_name=reference_measurement_name,
        evidence=reference_evidence,
    )
    output_measurement = SemanticCurrentMeasurement(
        measurement_type="branch_current",
        element=output_element,
        op_section="internal_parameters",
        op_name=f"@{output_element}[i]",
        dc_section="branches",
        dc_name=output_element.casefold(),
        evidence=(
            "output current is sensed through the selected output-compliance source",
        ),
    )
    semantic_mapping = CurrentMirrorSemanticMapping(
        reference_parameter=reference_parameter,
        reference_current_measurement=reference_measurement,
        output_parameter=output_parameter,
        output_current_measurement=output_measurement,
        sweep_element=output_element,
        output_node=_non_ground_node(output_source) or output_parameter,
        vout_nominal=nominal_vout,
        mirror_ratio=1.0,
        scale_parameter_names=scale_names,
        evidence=reference_evidence + (
            reference_status_evidence,
            "output source selected from Stage A operating_condition voltage source",
            "mirror_ratio=1.0 is the CurrentMirror V1 type-level protocol",
        ),
        reference_excitation=reference_excitation,
    )
    parameter_template = _build_param_template(
        circuit_id=circuit_id,
        circuit_type=circuit_type,
        ideal_payload=ideal_payload,
        runtime_parameters=runtime_parameters,
        output_source=output_source,
        vout_nominal=nominal_vout,
        model_definitions=models,
    )
    current_source_elements = tuple(
        source_elements[item.physical_target]
        for item in circuit_ir.sources
        if item.source_type == "current_source"
    )
    adapter = GenericPySpiceAdapter(
        circuit_id=circuit_id,
        topology_factory=factory,
        parameter_template=parameter_template,
        runtime_parameters=runtime_parameters,
        fixed_arguments=fixed_arguments,
        model_definitions=models,
        current_source_elements=current_source_elements,
        semantic_mapping=semantic_mapping,
        temperature=25.0,
    )
    adapter.validate_nominal_bindings()
    return ActiveLearningCircuitConfig(
        circuit_id=circuit_id,
        circuit_type=circuit_type,
        topology_path=topology_path,
        ideal_parameter_path=ideal_parameter_path,
        stage_a_rows=tuple(stage_a_rows),
        stage_b_search_space=search_space,
        runtime_parameters=runtime_parameters,
        param_template=parameter_template,
        semantic_mapping=semantic_mapping,
        adapter=adapter,
        candidate_builder=adapter.candidate_builder,
        runtime_provider=runtime_provider,
        operating_conditions=_operating_conditions(ideal_payload),
        testbench_config=deepcopy(dict(runtime_provider.testbench_defaults)),
        performance_template_path=runtime_provider.performance_template_path(
            Path(__file__).resolve().parents[1]
        ),
        auto_bound=len(runtime_parameters),
        unresolved=(),
    )


def _build_opamp_circuit_config(
    circuit_type: str,
    circuit_id: str,
    *,
    repository_root: str | Path | None = None,
    runtime_provider: Any,
) -> ActiveLearningCircuitConfig:
    """Run the same frozen Stage A/B pipeline and build an OpAmp runtime."""

    root = Path(repository_root).resolve() if repository_root else _REPOSITORY_ROOT
    circuit_root = root / "dataset" / circuit_id
    topology_path = circuit_root / "topology" / f"{circuit_id}.py"
    ideal_parameter_path = circuit_root / "simulation" / "parameter" / "ideal_param.yaml"
    op_result_path = circuit_root / "simulation" / "results" / "ideal" / "op_results.yaml"
    if not topology_path.is_file():
        raise AutoConfigError(f"PySpice topology not found: {topology_path}")
    if not ideal_parameter_path.is_file():
        raise AutoConfigError(f"Ideal working conditions not found: {ideal_parameter_path}")

    ideal_payload = _load_yaml(ideal_parameter_path)
    factory = _load_topology_factory(topology_path, circuit_id)
    circuit_ir = resolve_bindings(
        parse_pyspice_file(
            topology_path,
            circuit_id=circuit_id,
            circuit_type=circuit_type,
        )
    )
    stage_a_rows = build_stage_a_output_rows(
        circuit_ir,
        validity=validate_candidates(circuit_ir),
    )
    nominal_map = resolve_nominal_values(
        stage_a_rows,
        ideal_parameter_path,
        circuit_id=circuit_id,
        circuit_type=circuit_type,
        circuit_ir=circuit_ir,
        topology_factory=factory,
    )
    raw_operating = _operating_conditions(ideal_payload)
    search_space = construct_admissible_search_space(
        stage_a_rows,
        circuit_ir=circuit_ir,
        circuit_context=CircuitContext(
            circuit_id,
            circuit_type,
            nominal_values=nominal_map,
        ),
        operating_context=OperatingContext(
            vdd=raw_operating.get("VDD"),
            vss=raw_operating.get("VSS"),
            temperature=25.0,
            values=raw_operating,
        ),
        technology_context=TechnologyContext("ideal"),
        range_provider=IdealRangeProvider(),
    )
    models = _model_definitions(ideal_payload)
    optimization_nominals = {
        variable.variable_id: variable.nominal_value for variable in search_space.variables
    }
    topology_nominals = dict(nominal_map)
    topology_nominals.update(raw_operating)
    nominal_arguments, fixed_arguments = _topology_arguments(
        factory,
        optimization_nominals=optimization_nominals,
        all_nominals=topology_nominals,
        output_parameter=None,
        vout_nominal=None,
        model_definitions=models,
    )
    try:
        nominal_circuit = factory(**nominal_arguments)
    except Exception as error:
        raise BindingValidationError(
            f"{circuit_id}: nominal topology build failed during AutoConfig: {error}"
        ) from error
    source_elements = {
        source.physical_target: _resolve_element_name(
            nominal_circuit,
            _source_candidates(source),
            f"Stage A source {source.physical_target}",
        )
        for source in circuit_ir.sources
    }
    runtime_parameters = _runtime_parameters(search_space, circuit_ir, source_elements)
    rows_by_id = {row.parameter_id: row for row in stage_a_rows}

    op_payload = _load_yaml(op_result_path) if op_result_path.is_file() else None
    semantic_mapping = resolve_opamp_semantics(
        circuit_ir,
        operating_conditions=ideal_payload,
        operating_point=op_payload,
        operating_provenance=str(ideal_parameter_path),
        operating_point_provenance=(
            str(op_result_path) if op_payload is not None else None
        ),
    )
    parameter_template = _build_opamp_param_template(
        circuit_id=circuit_id,
        circuit_type=circuit_type,
        ideal_payload=ideal_payload,
        runtime_parameters=runtime_parameters,
        fixed_arguments=fixed_arguments,
        rows_by_id=rows_by_id,
        source_elements=source_elements,
        model_definitions=models,
    )

    testbench = deepcopy(dict(runtime_provider.testbench_defaults))
    testbench["load"] = {
        "preserve_native_elements": semantic_mapping.load_definition.preserve_native,
        "capacitance_f": semantic_mapping.load_definition.capacitance_f,
        "resistance_ohm": semantic_mapping.load_definition.resistance_ohm,
    }
    dataset_testbench = ideal_payload.get("operational_amplifier_testbench") or {}
    anchor = (
        dataset_testbench.get("floating_node_anchor")
        if isinstance(dataset_testbench, Mapping)
        else None
    )
    anchor = anchor if isinstance(anchor, Mapping) else {}
    reference_voltage = _numeric_nominal(anchor.get("reference_voltage_v"))
    reference_source = None
    if reference_voltage is not None:
        reference_source = "dataset operational_amplifier_testbench"
    elif semantic_mapping.common_mode_condition.input_common_mode_v is not None:
        reference_voltage = float(
            semantic_mapping.common_mode_condition.input_common_mode_v
        )
        reference_source = (
            semantic_mapping.common_mode_condition.input_value_source
            or "explicit input common mode"
        )
    anchor_resistance = _numeric_nominal(anchor.get("resistance_ohm"))
    if anchor_resistance is None:
        anchor_resistance = _numeric_nominal(
            testbench["stabilization"]["floating_node_anchor_resistance_ohm"]
        )
    testbench["stabilization"] = {
        "floating_node_anchor_resistance_ohm": anchor_resistance,
        "reference_voltage_v": reference_voltage,
        "reference_source": reference_source,
    }

    decorated_factory = _anchored_topology_factory(
        factory,
        reference_voltage_v=reference_voltage,
        resistance_ohm=anchor_resistance,
    )
    current_source_elements = tuple(
        source_elements[item.physical_target]
        for item in circuit_ir.sources
        if item.source_type == "current_source"
    )
    candidate_builder = GenericPySpiceBuilder(
        name=circuit_id,
        topology_factory=decorated_factory,
        parameter_template=parameter_template,
        runtime_parameters=runtime_parameters,
        fixed_arguments=fixed_arguments,
        model_definitions=models,
        normalize_ground_current_sources=current_source_elements,
        raw_spice_options=".options noacct",
    )
    candidate_builder.validate_nominal_bindings()

    operating_conditions = _operating_conditions(ideal_payload)
    operating_conditions["input_common_mode_v"] = (
        semantic_mapping.common_mode_condition.input_common_mode_v
    )
    performance_template_path = runtime_provider.performance_template_path(
        Path(__file__).resolve().parents[1]
    )
    if not performance_template_path.is_file():
        raise AutoConfigError(
            f"Performance template not found: {performance_template_path}"
        )
    return ActiveLearningCircuitConfig(
        circuit_id=circuit_id,
        circuit_type=circuit_type,
        topology_path=topology_path,
        ideal_parameter_path=ideal_parameter_path,
        stage_a_rows=tuple(stage_a_rows),
        stage_b_search_space=search_space,
        runtime_parameters=runtime_parameters,
        param_template=parameter_template,
        semantic_mapping=semantic_mapping,
        adapter=None,
        candidate_builder=candidate_builder,
        runtime_provider=runtime_provider,
        operating_conditions=operating_conditions,
        testbench_config=testbench,
        performance_template_path=performance_template_path,
        auto_bound=len(runtime_parameters),
        unresolved=(),
    )


def build_active_learning_circuit_config(
    circuit_type: str,
    circuit_id: str,
    *,
    repository_root: str | Path | None = None,
) -> ActiveLearningCircuitConfig:
    """Resolve a registered circuit type and build its Stage A/B runtime."""

    provider = resolve_runtime_provider(circuit_type)
    return provider.build_config(circuit_id, repository_root=repository_root)
