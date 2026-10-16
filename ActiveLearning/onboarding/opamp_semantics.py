"""Topology-derived OpAmp semantics without performance-metric evaluation."""

from __future__ import annotations

import cmath
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from param_extraction.stage_a import parse_pyspice_file, resolve_bindings

from .errors import SemanticMappingError
from .models import (
    OpAmpBiasSource,
    OpAmpCommonModeCondition,
    OpAmpFeedback,
    OpAmpFeedbackEdge,
    OpAmpInputEndpoint,
    OpAmpInputs,
    OpAmpLoadDefinition,
    OpAmpOutputs,
    OpAmpSemanticMapping,
    OpAmpSupplies,
    SemanticTerminalBinding,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_GROUND_NAMES = {"0", "gnd", "vss"}


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SemanticMappingError(f"Cannot load OpAmp semantic input {path}: {error}") from error
    if not isinstance(payload, dict):
        raise SemanticMappingError(f"OpAmp semantic input is not a mapping: {path}")
    return payload


def _numeric(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _operating_values(payload: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    raw = payload.get("operating_parameters") or {}
    if not isinstance(raw, Mapping):
        return result
    for name, value in raw.items():
        numeric = _numeric(value)
        if numeric is not None:
            result[str(name).removesuffix("_DC")] = numeric
    return result


def _casefold_lookup(mapping: Mapping[str, Any], name: str) -> float | None:
    target = name.casefold()
    for key, value in mapping.items():
        if str(key).casefold() == target:
            return _numeric(value)
    return None


def _op_output_value(payload: Mapping[str, Any], output_node: str) -> float | None:
    for field in ("node_voltages", "node_voltages_v"):
        values = payload.get(field)
        if isinstance(values, Mapping):
            result = _casefold_lookup(values, output_node)
            if result is not None:
                return result
    output = payload.get("output")
    if isinstance(output, Mapping):
        return _numeric(output.get("observed_value"))
    key_output = payload.get("key_output")
    if isinstance(key_output, Mapping):
        return _numeric(key_output.get("value"))
    return None


def _source_element(source: Any) -> str:
    prefix = "V" if source.source_type == "voltage_source" else "I"
    original = str(source.original_source_id)
    return prefix + original


def _signal_input_sources(circuit_ir: Any) -> list[Any]:
    return sorted(
        (
            source
            for source in circuit_ir.sources
            if source.source_type == "voltage_source"
            and str(source.physical_target).upper().startswith("VIN")
        ),
        key=lambda source: str(source.physical_target),
    )


def _output_nodes(circuit_ir: Any) -> list[str]:
    return sorted(
        (str(node) for node in circuit_ir.nodes if str(node).upper().startswith("VOUT")),
        key=str.casefold,
    )


def _paired_output_feedback_candidates(
    circuit_ir: Any,
    input_node: str,
    output_node: str,
) -> list[tuple[Any, Any]]:
    """Find differential-pair mates whose second gate is tied to the output.

    This deliberately requires a shared source and device type.  Merely seeing
    the output in an internal transistor, mirror, or compensation network is
    not sufficient evidence of global feedback.
    """

    external_devices = [
        device
        for device in circuit_ir.devices
        if device.device_type in {"nmos", "pmos"}
        and device.terminals.get("gate") == input_node
    ]
    feedback_devices = [
        device
        for device in circuit_ir.devices
        if device.device_type in {"nmos", "pmos"}
        and device.terminals.get("gate") == output_node
    ]
    return [
        (external, feedback)
        for external in external_devices
        for feedback in feedback_devices
        if external.device_type == feedback.device_type
        and external.terminals.get("source") == feedback.terminals.get("source")
        and external.device_id != feedback.device_id
    ]


def _unresolved_endpoint(
    status: str,
    evidence: Sequence[str],
) -> OpAmpInputEndpoint:
    return OpAmpInputEndpoint(
        node=None,
        source=None,
        terminal_binding=None,
        status=status,  # type: ignore[arg-type]
        evidence=tuple(evidence),
        confidence=None,
    )


def resolve_opamp_semantics(
    circuit_ir: Any,
    *,
    operating_conditions: Mapping[str, Any] | None = None,
    operating_point: Mapping[str, Any] | None = None,
    operating_provenance: str | None = None,
    operating_point_provenance: str | None = None,
) -> OpAmpSemanticMapping:
    """Resolve OpAmp roles from Stage A topology plus explicit artifacts.

    No circuit identifier participates in the rules.  Numerical common-mode
    values are transported only from the supplied operating artifacts.
    """

    inputs = _signal_input_sources(circuit_ir)
    outputs = _output_nodes(circuit_ir)
    vcm_sources = [
        source
        for source in circuit_ir.sources
        if str(source.physical_target).upper().startswith("VCM")
    ]
    evidence: list[str] = [
        "semantic roles derived from Stage A source/device connectivity",
    ]

    if len(outputs) == 1:
        output_node = outputs[0]
        output_mapping = OpAmpOutputs(
            mode="single_ended",
            positive_node=output_node,
            negative_node=None,
            status="resolved",
            evidence=(f"only topology output node is {output_node}",),
        )
    elif len(outputs) == 2:
        output_node = outputs[0]
        output_mapping = OpAmpOutputs(
            mode="differential",
            positive_node=None,
            negative_node=None,
            status="ambiguous",
            evidence=(
                f"two topology output nodes found: {outputs}; polarity is not name-derived",
            ),
        )
    elif outputs:
        output_node = outputs[0]
        output_mapping = OpAmpOutputs(
            mode="ambiguous",
            positive_node=None,
            negative_node=None,
            status="ambiguous",
            evidence=(f"multiple topology output nodes found: {outputs}",),
        )
    else:
        output_node = ""
        output_mapping = OpAmpOutputs(
            mode="unavailable",
            positive_node=None,
            negative_node=None,
            status="unavailable",
            evidence=("no VOUT topology node was found",),
        )

    feedback_edges: tuple[OpAmpFeedbackEdge, ...] = ()
    if len(inputs) == 1 and output_mapping.status == "resolved":
        external = inputs[0]
        input_node = str(external.terminals.get("p") or external.physical_target)
        pairs = _paired_output_feedback_candidates(circuit_ir, input_node, output_node)
        if len(pairs) == 1:
            external_device, feedback_device = pairs[0]
            binding = SemanticTerminalBinding(
                element=str(feedback_device.device_id),
                terminal="gate",
                original_node=output_node,
            )
            positive = OpAmpInputEndpoint(
                node=input_node,
                source=_source_element(external),
                terminal_binding=SemanticTerminalBinding(
                    element=str(external_device.device_id),
                    terminal="gate",
                    original_node=input_node,
                ),
                status="resolved",
                evidence=(
                    f"{input_node} is the only external signal input",
                    f"{external_device.device_id} and {feedback_device.device_id} share "
                    "device type and source node",
                ),
                confidence=1.0,
            )
            negative = OpAmpInputEndpoint(
                node=output_node,
                source=None,
                terminal_binding=binding,
                status="resolved",
                evidence=(
                    f"{output_node} directly drives {feedback_device.device_id}.gate",
                    "paired input transistor identifies the internal feedback endpoint",
                ),
                confidence=1.0,
            )
            input_mapping = OpAmpInputs(
                positive=positive,
                negative=negative,
                exposure="single_external_plus_internal_feedback",
                polarity_status="resolved",
                candidates=(positive, negative),
            )
            feedback_edges = (
                OpAmpFeedbackEdge(
                    from_node=output_node,
                    to_node=output_node,
                    to_terminal=binding,
                    factor=1.0,
                    status="resolved",
                    evidence=(
                        f"direct topology edge {output_node} -> {feedback_device.device_id}.gate",
                    ),
                ),
            )
            feedback = OpAmpFeedback(
                exposure="explicit_closed_loop_direct",
                edges=feedback_edges,
                unity_feedback_confirmed=True,
                status="resolved",
                evidence=("direct output-to-input terminal connection has factor 1",),
            )
            measurement_modes = ("existing_feedback_return_ratio",)
        else:
            reason = (
                "no paired output-driven input terminal found"
                if not pairs
                else "multiple paired output-driven input terminals found"
            )
            endpoint_status = "unavailable" if not pairs else "ambiguous"
            input_mapping = OpAmpInputs(
                positive=_unresolved_endpoint(endpoint_status, (reason,)),
                negative=_unresolved_endpoint(endpoint_status, (reason,)),
                exposure=endpoint_status,
                polarity_status=endpoint_status,
                candidates=(),
            )
            feedback = OpAmpFeedback(
                exposure=endpoint_status,
                edges=(),
                unity_feedback_confirmed=False,
                status=endpoint_status,
                evidence=(reason,),
            )
            measurement_modes = ()
    elif len(inputs) == 2:
        candidates = tuple(
            OpAmpInputEndpoint(
                node=str(source.terminals.get("p") or source.physical_target),
                source=_source_element(source),
                terminal_binding=None,
                status="resolved",
                evidence=("external voltage source drives a topology input node",),
                confidence=1.0,
            )
            for source in inputs
        )
        polarity_evidence = (
            "two external input nodes are resolved; positive/negative polarity requires "
            "a deterministic sign probe and is not inferred from VIN names",
        )
        exposure = (
            "dual_external_differential_with_common_mode_control"
            if vcm_sources
            else "dual_external_differential"
        )
        input_mapping = OpAmpInputs(
            positive=_unresolved_endpoint("ambiguous", polarity_evidence),
            negative=_unresolved_endpoint("ambiguous", polarity_evidence),
            exposure=exposure,
            polarity_status="ambiguous",
            candidates=candidates,
        )
        feedback_exposure = (
            "open_loop_differential_with_internal_common_mode_control"
            if vcm_sources
            else "open_loop_no_global_feedback"
        )
        feedback = OpAmpFeedback(
            exposure=feedback_exposure,
            edges=(),
            unity_feedback_confirmed=False,
            status="resolved",
            evidence=(
                "dual external signal inputs are independently driven",
                "no output-to-input-pair edge exists; internal mirrors and compensation are excluded",
            ),
        )
        measurement_modes = ("synthesized_unity_feedback_return_ratio",)
    elif inputs:
        reason = f"unsupported number of external signal inputs: {len(inputs)}"
        input_mapping = OpAmpInputs(
            positive=_unresolved_endpoint("ambiguous", (reason,)),
            negative=_unresolved_endpoint("ambiguous", (reason,)),
            exposure="ambiguous",
            polarity_status="ambiguous",
            candidates=(),
        )
        feedback = OpAmpFeedback(
            exposure="ambiguous",
            edges=(),
            unity_feedback_confirmed=False,
            status="ambiguous",
            evidence=(reason,),
        )
        measurement_modes = ()
    else:
        reason = "no external VIN source was found"
        input_mapping = OpAmpInputs(
            positive=_unresolved_endpoint("unavailable", (reason,)),
            negative=_unresolved_endpoint("unavailable", (reason,)),
            exposure="unavailable",
            polarity_status="unavailable",
            candidates=(),
        )
        feedback = OpAmpFeedback(
            exposure="unavailable",
            edges=(),
            unity_feedback_confirmed=False,
            status="unavailable",
            evidence=(reason,),
        )
        measurement_modes = ()

    source_names = {str(source.physical_target).upper() for source in circuit_ir.sources}
    positive_supply = "VDD" if "VDD" in source_names else None
    negative_supply = "VSS" if "0" in {str(node).casefold() for node in circuit_ir.nodes} else None
    supply_status = "resolved" if positive_supply and negative_supply else "unavailable"
    supplies = OpAmpSupplies(
        positive=positive_supply,
        negative=negative_supply,
        status=supply_status,
        evidence=("Stage A supply source and canonical ground connectivity",),
    )

    bias_sources = tuple(
        OpAmpBiasSource(
            element=_source_element(source),
            quantity=("current" if source.source_type == "current_source" else "voltage"),
            parameter=str(source.physical_target),
            node=str(source.terminals.get("n") or source.terminals.get("p") or ""),
            evidence=("Stage A external non-signal bias source",),
        )
        for source in circuit_ir.sources
        if str(source.physical_target).upper().startswith(("IB", "VB"))
    )

    operating = _operating_values(operating_conditions or {})
    input_values = [
        _casefold_lookup(operating, str(source.physical_target)) for source in inputs
    ]
    if input_values and all(value is not None for value in input_values):
        input_common_mode = sum(float(value) for value in input_values if value is not None) / len(input_values)
        common_status = "resolved"
        common_evidence = (
            "input common mode transported from explicit operating_parameters",
        )
    else:
        input_common_mode = None
        common_status = "unavailable"
        common_evidence = (
            "input common mode is unavailable; it was not guessed from supply rails",
        )
    explicit_output_reference = (
        _casefold_lookup(operating, output_node) if output_node else None
    )
    if explicit_output_reference is not None:
        output_reference = explicit_output_reference
        output_reference_source = operating_provenance
    elif operating_point is not None and output_node:
        output_reference = _op_output_value(operating_point, output_node)
        output_reference_source = (
            operating_point_provenance if output_reference is not None else None
        )
    else:
        output_reference = None
        output_reference_source = None
    common_mode = OpAmpCommonModeCondition(
        input_common_mode_v=input_common_mode,
        input_value_source=operating_provenance if input_common_mode is not None else None,
        output_reference_source=output_reference_source,
        output_reference_v=output_reference,
        status=common_status,
        evidence=common_evidence,
    )

    return OpAmpSemanticMapping(
        inputs=input_mapping,
        outputs=output_mapping,
        supplies=supplies,
        bias_sources=bias_sources,
        common_mode_condition=common_mode,
        feedback=feedback,
        load_definition=OpAmpLoadDefinition(
            mode="native_plus_external",
            preserve_native=True,
            resistance_ohm=None,
            capacitance_f=1.0e-12,
            differential=output_mapping.mode == "differential",
            provenance="opamp_v1_testbench_contract",
        ),
        measurement_modes=measurement_modes,
        evidence=tuple(evidence),
    )


def build_opamp_semantic_mapping(
    circuit_id: str,
    *,
    repository_root: str | Path | None = None,
) -> OpAmpSemanticMapping:
    """Load one dataset circuit and resolve its OpAmp semantic provider record."""

    root = Path(repository_root).resolve() if repository_root else _REPOSITORY_ROOT
    circuit_root = root / "dataset" / circuit_id
    topology_path = circuit_root / "topology" / f"{circuit_id}.py"
    operating_path = circuit_root / "simulation" / "parameter" / "ideal_param.yaml"
    op_path = circuit_root / "simulation" / "results" / "ideal" / "op_results.yaml"
    if not topology_path.is_file():
        raise SemanticMappingError(f"OpAmp PySpice topology not found: {topology_path}")
    if not operating_path.is_file():
        raise SemanticMappingError(f"OpAmp operating artifact not found: {operating_path}")
    circuit_ir = resolve_bindings(
        parse_pyspice_file(
            topology_path,
            circuit_id=circuit_id,
            circuit_type="OpAmp",
        )
    )
    op_payload = _load_mapping(op_path) if op_path.is_file() else None
    return resolve_opamp_semantics(
        circuit_ir,
        operating_conditions=_load_mapping(operating_path),
        operating_point=op_payload,
        operating_provenance=str(operating_path),
        operating_point_provenance=str(op_path) if op_payload is not None else None,
    )


def resolve_differential_input_polarity(
    input_a: str,
    input_b: str,
    low_frequency_transfer: complex,
) -> dict[str, Any]:
    """Resolve input sign from a caller-supplied low-frequency AC sign probe.

    This helper performs no simulation and computes no performance metric.  It
    simply isolates the deterministic sign-validation rule used by the prior
    calibration so a future evaluator can opt in without naming conventions.
    """

    value = complex(low_frequency_transfer)
    magnitude = abs(value)
    phase_deg = math.degrees(cmath.phase(value))
    phase_distance = min(abs(phase_deg), abs(abs(phase_deg) - 180.0))
    if not (math.isfinite(value.real) and math.isfinite(value.imag)) or magnitude <= 1.0e-18:
        return {
            "status": "unavailable",
            "positive": None,
            "negative": None,
            "reason": "non_finite_or_numerically_zero_low_frequency_transfer",
        }
    if phase_distance > 45.0:
        return {
            "status": "ambiguous",
            "positive": None,
            "negative": None,
            "reason": "low_frequency_transfer_is_not_predominantly_real",
        }
    return {
        "status": "resolved",
        "positive": input_a if value.real > 0.0 else input_b,
        "negative": input_b if value.real > 0.0 else input_a,
        "reason": None,
    }


# Descriptive alias for callers that prefer provider terminology.
load_opamp_semantic_mapping = build_opamp_semantic_mapping
