"""Small runtime models shared by the onboarding builder and adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


SemanticResolutionState = Literal["resolved", "ambiguous", "unavailable"]


def _validate_resolution_state(value: str, *, field_name: str) -> None:
    if value not in {"resolved", "ambiguous", "unavailable"}:
        raise ValueError(
            f"{field_name} must be resolved, ambiguous, or unavailable; got {value!r}"
        )


def _jsonable(value: Any) -> Any:
    """Recursively convert frozen semantic records to JSON/YAML-ready values."""

    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class RuntimeBinding:
    """One Stage A physical target controlled by an optimizer variable."""

    parameter_id: str
    instance: str
    field: str
    pyspice_attribute: str
    element_name: str
    source_kind: str
    direct_value: bool


@dataclass(frozen=True)
class RuntimeParameter:
    """Stage A binding plus the Stage B admissible domain."""

    name: str
    semantic_category: str | None
    nominal: float | int
    lower: float | int
    upper: float | int
    variable_type: str
    unit: str | None
    scale: str | None
    transform: str | None
    fixed_sign: str | None
    bindings: tuple[RuntimeBinding, ...]


@dataclass(frozen=True)
class SemanticCurrentMeasurement:
    """One topology current role plus its simulator vector locations."""

    measurement_type: Literal["source_current", "device_current", "branch_current"]
    element: str
    op_section: Literal["branches", "internal_parameters"]
    op_name: str
    dc_section: Literal["branches", "internal_parameters"] | None = None
    dc_name: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticExcitationCandidate:
    """One topology-supported external source that may excite a reference branch."""

    excitation_type: Literal["current", "voltage"]
    element: str
    parameter: str | None
    nominal: float
    topology_role: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticReferenceExcitation:
    """External controllable stimulus used by the reference-response test."""

    status: Literal["resolved", "unresolved", "ambiguous"]
    excitation_type: Literal["current", "voltage"] | None = None
    element: str | None = None
    parameter: str | None = None
    nominal: float | None = None
    evidence: tuple[str, ...] = ()
    candidates: tuple[SemanticExcitationCandidate, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class CurrentMirrorSemanticMapping:
    """Topology roles consumed by the generic CurrentMirror simulator."""

    reference_parameter: str | None
    reference_current_measurement: SemanticCurrentMeasurement
    output_parameter: str
    output_current_measurement: SemanticCurrentMeasurement
    sweep_element: str
    output_node: str
    vout_nominal: float
    mirror_ratio: float
    scale_parameter_names: tuple[str, ...]
    evidence: tuple[str, ...]
    reference_excitation: SemanticReferenceExcitation | None = None

    @property
    def reference_element(self) -> str:
        """Compatibility view for callers that only need the physical element."""

        return self.reference_current_measurement.element

    @property
    def output_element(self) -> str:
        """Compatibility view for callers that only need the physical element."""

        return self.output_current_measurement.element


@dataclass(frozen=True)
class SemanticTerminalBinding:
    """A topology terminal that can be redirected by a testbench decorator."""

    element: str
    terminal: str
    original_node: str


@dataclass(frozen=True)
class OpAmpInputEndpoint:
    """One resolved or unresolved OpAmp signal-input endpoint."""

    node: str | None
    source: str | None
    terminal_binding: SemanticTerminalBinding | None
    status: SemanticResolutionState
    evidence: tuple[str, ...] = ()
    confidence: float | None = None

    def __post_init__(self) -> None:
        _validate_resolution_state(self.status, field_name="OpAmp input status")


@dataclass(frozen=True)
class OpAmpInputs:
    positive: OpAmpInputEndpoint
    negative: OpAmpInputEndpoint
    exposure: Literal[
        "single_external_plus_internal_feedback",
        "dual_external_differential",
        "dual_external_differential_with_common_mode_control",
        "ambiguous",
        "unavailable",
    ]
    polarity_status: SemanticResolutionState
    candidates: tuple[OpAmpInputEndpoint, ...] = ()

    def __post_init__(self) -> None:
        _validate_resolution_state(
            self.polarity_status, field_name="OpAmp input polarity_status"
        )


@dataclass(frozen=True)
class OpAmpOutputs:
    mode: Literal["single_ended", "differential", "ambiguous", "unavailable"]
    positive_node: str | None
    negative_node: str | None
    status: SemanticResolutionState
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_resolution_state(self.status, field_name="OpAmp output status")


@dataclass(frozen=True)
class OpAmpSupplies:
    positive: str | None
    negative: str | None
    status: SemanticResolutionState
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_resolution_state(self.status, field_name="OpAmp supply status")


@dataclass(frozen=True)
class OpAmpBiasSource:
    element: str
    quantity: Literal["current", "voltage"]
    parameter: str | None
    node: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpAmpCommonModeCondition:
    input_common_mode_v: float | None
    input_value_source: str | None
    output_reference_source: str | None
    output_reference_v: float | None
    status: SemanticResolutionState
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_resolution_state(
            self.status, field_name="OpAmp common-mode status"
        )


@dataclass(frozen=True)
class OpAmpFeedbackEdge:
    from_node: str
    to_node: str | None
    to_terminal: SemanticTerminalBinding | None
    factor: float
    status: SemanticResolutionState
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_resolution_state(self.status, field_name="OpAmp feedback edge status")


@dataclass(frozen=True)
class OpAmpFeedback:
    exposure: Literal[
        "explicit_closed_loop_direct",
        "open_loop_no_global_feedback",
        "open_loop_differential_with_internal_common_mode_control",
        "ambiguous",
        "unavailable",
    ]
    edges: tuple[OpAmpFeedbackEdge, ...]
    unity_feedback_confirmed: bool
    status: SemanticResolutionState
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_resolution_state(self.status, field_name="OpAmp feedback status")


@dataclass(frozen=True)
class OpAmpLoadDefinition:
    mode: Literal["external", "native", "native_plus_external"]
    preserve_native: bool
    resistance_ohm: float | None
    capacitance_f: float | None
    differential: bool
    provenance: str


@dataclass(frozen=True)
class OpAmpSemanticMapping:
    """Topology semantics and frozen V1 testbench contract for an OpAmp."""

    inputs: OpAmpInputs
    outputs: OpAmpOutputs
    supplies: OpAmpSupplies
    bias_sources: tuple[OpAmpBiasSource, ...]
    common_mode_condition: OpAmpCommonModeCondition
    feedback: OpAmpFeedback
    load_definition: OpAmpLoadDefinition
    measurement_modes: tuple[str, ...]
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a stable, JSON/YAML-ready representation."""

        return _jsonable(asdict(self))


@dataclass(frozen=True)
class ActiveLearningCircuitConfig:
    """Executable configuration produced from topology plus Stage A/B."""

    circuit_id: str
    circuit_type: str
    topology_path: Path
    ideal_parameter_path: Path
    stage_a_rows: tuple[Any, ...]
    stage_b_search_space: Any
    runtime_parameters: tuple[RuntimeParameter, ...]
    param_template: dict[str, Any]
    semantic_mapping: CurrentMirrorSemanticMapping | OpAmpSemanticMapping
    adapter: Any
    candidate_builder: Any
    runtime_provider: Any
    operating_conditions: dict[str, Any]
    testbench_config: dict[str, Any]
    performance_template_path: Path
    auto_bound: int
    unresolved: tuple[str, ...]

    def create_evaluator(
        self,
        *,
        testbench_overrides: dict[str, Any] | None = None,
    ) -> Any:
        return self.runtime_provider.create_evaluator(
            self,
            testbench_overrides=testbench_overrides,
        )

    def create_functional_validator(self) -> Any:
        return self.runtime_provider.create_functional_validator(self)

    def evaluate_nominal(
        self,
        *,
        target_specs: dict[str, Any] | None = None,
        testbench_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.runtime_provider.evaluate_nominal(
            self,
            target_specs=target_specs,
            testbench_overrides=testbench_overrides,
        )

    def summary(self) -> dict[str, Any]:
        if isinstance(self.semantic_mapping, OpAmpSemanticMapping):
            semantic_mapping = self.semantic_mapping.to_dict()
        else:
            semantic_mapping = {
                "reference_parameter": self.semantic_mapping.reference_parameter,
                "reference_element": self.semantic_mapping.reference_element,
                "reference_current_measurement": {
                    "type": self.semantic_mapping.reference_current_measurement.measurement_type,
                    "element": self.semantic_mapping.reference_current_measurement.element,
                    "section": self.semantic_mapping.reference_current_measurement.op_section,
                    "name": self.semantic_mapping.reference_current_measurement.op_name,
                    "evidence": list(
                        self.semantic_mapping.reference_current_measurement.evidence
                    ),
                },
                "reference_excitation": (
                    None
                    if self.semantic_mapping.reference_excitation is None
                    else {
                        "status": self.semantic_mapping.reference_excitation.status,
                        "type": self.semantic_mapping.reference_excitation.excitation_type,
                        "element": self.semantic_mapping.reference_excitation.element,
                        "parameter": self.semantic_mapping.reference_excitation.parameter,
                        "nominal": self.semantic_mapping.reference_excitation.nominal,
                        "evidence": list(
                            self.semantic_mapping.reference_excitation.evidence
                        ),
                        "reason": self.semantic_mapping.reference_excitation.reason,
                        "candidates": [
                            {
                                "type": item.excitation_type,
                                "element": item.element,
                                "parameter": item.parameter,
                                "nominal": item.nominal,
                                "topology_role": item.topology_role,
                                "evidence": list(item.evidence),
                            }
                            for item in self.semantic_mapping.reference_excitation.candidates
                        ],
                    }
                ),
                "output_parameter": self.semantic_mapping.output_parameter,
                "output_element": self.semantic_mapping.output_element,
                "output_current_measurement": {
                    "type": self.semantic_mapping.output_current_measurement.measurement_type,
                    "element": self.semantic_mapping.output_current_measurement.element,
                    "op_section": self.semantic_mapping.output_current_measurement.op_section,
                    "op_name": self.semantic_mapping.output_current_measurement.op_name,
                    "dc_section": self.semantic_mapping.output_current_measurement.dc_section,
                    "dc_name": self.semantic_mapping.output_current_measurement.dc_name,
                    "evidence": list(
                        self.semantic_mapping.output_current_measurement.evidence
                    ),
                },
                "sweep_element": self.semantic_mapping.sweep_element,
                "output_node": self.semantic_mapping.output_node,
            }
        return {
            "circuit_id": self.circuit_id,
            "circuit_type": self.circuit_type,
            "parameters": len(self.runtime_parameters),
            "auto_bound": self.auto_bound,
            "unresolved": len(self.unresolved),
            "simulator": self.circuit_type,
            "semantic_resolver": self.runtime_provider.semantic_resolver_name,
            "evaluator": self.runtime_provider.evaluator_name,
            "functional_validator": self.runtime_provider.functional_validator_name,
            "performance_template": self.runtime_provider.performance_template_name,
            "performance_template_path": str(self.performance_template_path),
            "parameter_source": "Stage A/B AutoConfig",
            "parameter_nominals": {
                item.name: item.nominal for item in self.runtime_parameters
            },
            "operating_conditions": _jsonable(self.operating_conditions),
            "testbench": _jsonable(self.testbench_config),
            "semantic_mapping": semantic_mapping,
        }
