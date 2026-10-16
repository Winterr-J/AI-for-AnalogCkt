"""Technology-neutral Stage B v1 input and output models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .types import DomainType, RangeStatus, RelationStatus, Severity


def _get(row: object | Mapping[str, Any], name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (DomainType, RangeStatus, RelationStatus, Severity)):
        return value.value
    return value


@dataclass(frozen=True)
class StageATargetRecord:
    """The frozen Stage A fields consumed by Stage B."""

    circuit_id: str
    circuit_type: str
    parameter_id: str
    parameter_name: str | None
    device_id: str | None
    device_type: str | None
    parameter_kind: str
    validity_status: str
    semantic_category: str | None
    binding_status: str
    optimization_eligible: bool
    evidence: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: object | Mapping[str, Any]) -> "StageATargetRecord":
        return cls(
            circuit_id=str(_get(row, "circuit_id")),
            circuit_type=str(_get(row, "circuit_type")),
            parameter_id=str(_get(row, "parameter_id")),
            parameter_name=_get(row, "parameter_name"),
            device_id=_get(row, "device_id"),
            device_type=_get(row, "device_type"),
            parameter_kind=str(_get(row, "parameter_kind")),
            validity_status=str(_get(row, "validity_status")),
            semantic_category=_get(row, "semantic_category"),
            binding_status=str(_get(row, "binding_status")),
            optimization_eligible=bool(_get(row, "optimization_eligible", False)),
            evidence=tuple(_get(row, "evidence", ()) or ()),
        )


@dataclass(frozen=True)
class CircuitContext:
    circuit_id: str
    circuit_type: str
    nominal_values: Mapping[str, float | int] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperatingContext:
    """Fixed conditions; these are context, not automatically optimizer dimensions."""

    vdd: float | None = None
    vss: float | None = None
    temperature: float | None = None
    values: Mapping[str, float | int | str | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class TechnologyContext:
    technology: str
    model_parameters: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationEvidence:
    """A topology-backed expression for one physical Stage A target."""

    parameter_id: str
    source_expression: str | None
    resolved_expression: str | None
    normalized_expression: str | None = None
    root_symbols: tuple[str, ...] = ()
    alias_chain: tuple[str, ...] = ()
    direct_symbol: str | None = None
    owner_symbol: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: Severity = Severity.WARNING
    parameter_ids: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class NumericRange:
    lower: float | int | None = None
    upper: float | int | None = None
    scale: str | None = None
    transform: str | None = None
    fixed_sign: str | None = None


@dataclass(frozen=True)
class RangeDecision:
    range: NumericRange = NumericRange()
    status: RangeStatus = RangeStatus.UNCERTAIN
    reason: str = "RANGE_PROVIDER_NOT_APPLIED"
    provider: str | None = None
    evidence: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class TargetRangeRequest:
    """One physical target presented to a technology range provider."""

    parameter_id: str
    parameter_name: str | None
    device_id: str | None
    device_type: str | None
    parameter_kind: str
    semantic_category: str | None
    domain_type: DomainType
    unit: str | None
    nominal_value: float | int | None


@dataclass(frozen=True)
class TargetRangeRecord:
    """Auditable local and inverse-mapped range for one physical target."""

    parameter_id: str
    nominal_value: float | int | None
    relation: str
    expression: str
    local_range: NumericRange
    local_status: RangeStatus
    local_reason: str
    mapped_to_root: NumericRange
    mapping_status: RangeStatus
    mapping_reason: str
    provider: str | None
    evidence: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class ConstraintRecord:
    type: str
    expression: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class DependentTarget:
    parameter_id: str
    relation: str
    expression: str
    relation_status: RelationStatus = RelationStatus.VALID
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchVariable:
    variable_id: str
    members: tuple[str, ...]
    semantic_category: str | None
    parameter_kind: str | None
    domain_type: DomainType
    independent: bool
    nominal_value: float | int | None
    unit: str | None
    range: NumericRange
    constraints: tuple[ConstraintRecord, ...]
    dependent_targets: tuple[DependentTarget, ...]
    target_ranges: tuple[TargetRangeRecord, ...]
    range_status: RangeStatus
    range_reason: str
    provider: str | None
    relation_status: RelationStatus
    evidence: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class UnresolvedTarget:
    parameter_id: str
    relation_status: RelationStatus
    reason: str
    expression: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExcludedTarget:
    parameter_id: str
    reason: str


@dataclass(frozen=True)
class SearchSpace:
    schema_version: str
    circuit_id: str
    circuit_type: str
    technology: str
    status: RelationStatus
    operating_context: OperatingContext
    variables: tuple[SearchVariable, ...]
    excluded_targets: tuple[ExcludedTarget, ...] = ()
    unresolved_targets: tuple[UnresolvedTarget, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))
