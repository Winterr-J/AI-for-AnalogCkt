"""Dataclasses for the Stage A Circuit IR.

The IR records construction and binding facts. It deliberately has no
validity, semantic-category, or optimization-eligibility decisions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExpressionIR:
    """A statically recovered value expression or an omitted slot."""

    expression_type: str
    source_text: str | None = None
    resolved_text: str | None = None
    direct_symbol: str | None = None
    symbol_references: tuple[str, ...] = ()
    alias_chain: tuple[str, ...] = ()
    ast_dump: str | None = None
    explicit: bool = True

    @classmethod
    def omitted(cls) -> "ExpressionIR":
        return cls(expression_type="omitted", explicit=False)


@dataclass
class BindingIR:
    """Internal binding result for one physical target."""

    parameter_id: str
    parameter_name: str | None
    binding_status: str
    binding_root_argument: str | None
    binding_reason: str
    binding_expression: str | None
    normalized_expression: str | None
    root_symbol_dependencies: list[str] = field(default_factory=list)
    alias_chain: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    review_status: str = "AUTO"
    confidence: str = "HIGH"


@dataclass
class PhysicalTargetIR:
    """One technology-independent physical parameter target."""

    parameter_id: str
    device_id: str | None
    device_type: str | None
    parameter_kind: str
    source_kind: str
    parameter_name: str | None
    source_expression: ExpressionIR
    binding: BindingIR | None = None


@dataclass
class DeviceIR:
    device_id: str
    original_device_id: str
    device_type: str
    terminals: dict[str, str]
    model_reference: ExpressionIR | None = None
    explicit_parameter_expressions: dict[str, ExpressionIR] = field(default_factory=dict)
    canonical_parameter_targets: list[str] = field(default_factory=list)
    source_location: str | None = None


@dataclass
class SourceIR:
    source_id: str
    original_source_id: str
    source_type: str
    terminals: dict[str, str]
    value_expression: ExpressionIR
    physical_target: str
    source_location: str | None = None


@dataclass
class ModelIR:
    model_id: str
    model_type: str | None
    reference_expression: ExpressionIR
    declared_parameters: dict[str, ExpressionIR] = field(default_factory=dict)
    physical_target: str | None = None


@dataclass
class SubcircuitIR:
    instance_id: str
    original_instance_id: str
    subcircuit_reference: ExpressionIR
    nodes: list[str]
    exposed_parameters: dict[str, ExpressionIR] = field(default_factory=dict)
    canonical_parameter_targets: list[str] = field(default_factory=list)
    source_location: str | None = None


@dataclass
class ParameterRelationIR:
    """A safe relation recovered from the current implementation interface."""

    relation_id: str
    relation_type: str
    members: list[str]
    root_symbol: str | None
    source_expression: str | None
    evidence: list[str] = field(default_factory=list)


@dataclass
class DiagnosticIR:
    code: str
    message: str
    severity: str = "warning"
    line: int | None = None
    column: int | None = None


@dataclass
class CircuitIR:
    circuit_id: str
    circuit_type: str
    devices: list[DeviceIR] = field(default_factory=list)
    sources: list[SourceIR] = field(default_factory=list)
    nodes: set[str] = field(default_factory=set)
    models: list[ModelIR] = field(default_factory=list)
    subcircuits: list[SubcircuitIR] = field(default_factory=list)
    physical_targets: list[PhysicalTargetIR] = field(default_factory=list)
    parameter_relations: list[ParameterRelationIR] = field(default_factory=list)
    diagnostics: list[DiagnosticIR] = field(default_factory=list)
    python_arguments: list[str] = field(default_factory=list)
    local_aliases: dict[str, ExpressionIR] = field(default_factory=dict)

    def add_nodes(self, *nodes: str) -> None:
        self.nodes.update(node for node in nodes if node)

    def target(self, parameter_id: str) -> PhysicalTargetIR:
        matches = [item for item in self.physical_targets if item.parameter_id == parameter_id]
        if len(matches) != 1:
            raise KeyError(f"Expected one target {parameter_id!r}; found {len(matches)}")
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["nodes"] = sorted(self.nodes)
        return result

    def dump_json(self, path: str | Path) -> None:
        """Explicit debug-only IR dump; parsing never writes automatically."""

        output = Path(path)
        output.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
