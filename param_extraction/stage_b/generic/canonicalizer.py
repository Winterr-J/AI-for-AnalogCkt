"""Canonicalize physical Stage A targets into independent optimizer dimensions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from ..constraints.explicit_relations import (
    ParsedExplicitRelation,
    analyze_explicit_relation,
)
from ..core.models import (
    CircuitContext,
    ConstraintRecord,
    DependentTarget,
    Diagnostic,
    NumericRange,
    RelationEvidence,
    SearchVariable,
    StageATargetRecord,
    UnresolvedTarget,
)
from ..core.types import DomainType, RangeStatus, RelationStatus, Severity
from .domain_classifier import classify_domain


@dataclass(frozen=True)
class CanonicalizationResult:
    variables: tuple[SearchVariable, ...]
    unresolved_targets: tuple[UnresolvedTarget, ...]
    diagnostics: tuple[Diagnostic, ...]
    status: RelationStatus


@dataclass(frozen=True)
class _ResolvedTarget:
    target: StageATargetRecord
    evidence: RelationEvidence
    relation: ParsedExplicitRelation
    owner_symbol: str
    root_symbol: str


def canonicalize_variables(
    targets: Iterable[StageATargetRecord],
    relation_evidence: Iterable[RelationEvidence],
    circuit_context: CircuitContext,
) -> CanonicalizationResult:
    targets = tuple(targets)
    evidence_rows = tuple(relation_evidence)
    diagnostics: list[Diagnostic] = []
    unresolved: list[UnresolvedTarget] = []

    by_evidence: dict[str, list[RelationEvidence]] = defaultdict(list)
    for item in evidence_rows:
        by_evidence[item.parameter_id].append(item)

    resolved: list[_ResolvedTarget] = []
    for target in targets:
        candidates = by_evidence.get(target.parameter_id, [])
        if len(candidates) > 1 and _has_conflicting_evidence(candidates):
            diagnostics.append(
                Diagnostic(
                    code="CONFLICTING_EXPLICIT_RELATIONS",
                    message=f"Conflicting relation evidence for {target.parameter_id}.",
                    severity=Severity.ERROR,
                    parameter_ids=(target.parameter_id,),
                    evidence=tuple(
                        sorted(
                            str((item.source_expression, item.resolved_expression))
                            for item in candidates
                        )
                    ),
                )
            )
            unresolved.append(
                UnresolvedTarget(
                    parameter_id=target.parameter_id,
                    relation_status=RelationStatus.CONFLICT,
                    reason="CONFLICTING_EXPLICIT_RELATIONS",
                )
            )
            continue

        evidence = candidates[0] if candidates else _identity_evidence(target)
        relation = analyze_explicit_relation(evidence)
        if relation.status != RelationStatus.VALID or len(relation.roots) != 1:
            code = relation.reason or "RELATION_UNCERTAIN"
            diagnostics.append(
                Diagnostic(
                    code=code,
                    message=(
                        f"Cannot canonicalize {target.parameter_id}: "
                        f"{relation.reason or relation.relation_type}."
                    ),
                    severity=Severity.WARNING,
                    parameter_ids=(target.parameter_id,),
                    evidence=evidence.evidence,
                )
            )
            unresolved.append(
                UnresolvedTarget(
                    parameter_id=target.parameter_id,
                    relation_status=relation.status,
                    reason=code,
                    expression=relation.expression,
                    evidence=evidence.evidence,
                )
            )
            continue
        root = relation.roots[0]
        owner = (
            evidence.owner_symbol
            or (evidence.alias_chain[-1] if evidence.alias_chain else None)
            or target.parameter_name
            or root
        )
        resolved.append(
            _ResolvedTarget(
                target=target,
                evidence=evidence,
                relation=relation,
                owner_symbol=owner,
                root_symbol=root,
            )
        )

    alias_graph: dict[str, str] = {}
    for item in resolved:
        if item.relation.relation_type in {"IDENTITY", "EXPLICIT_EQUALITY"}:
            if item.owner_symbol != item.root_symbol:
                prior = alias_graph.get(item.owner_symbol)
                if prior is not None and prior != item.root_symbol:
                    diagnostics.append(
                        Diagnostic(
                            code="CONFLICTING_ALIAS_ROOTS",
                            message=(
                                f"Alias {item.owner_symbol} points to both {prior} "
                                f"and {item.root_symbol}."
                            ),
                            severity=Severity.ERROR,
                            parameter_ids=(item.target.parameter_id,),
                        )
                    )
                alias_graph[item.owner_symbol] = item.root_symbol

    cyclic_symbols = _cyclic_symbols(alias_graph)
    if cyclic_symbols:
        cycle_parameters = sorted(
            {
                item.target.parameter_id
                for item in resolved
                if item.owner_symbol in cyclic_symbols or item.root_symbol in cyclic_symbols
            }
        )
        diagnostics.append(
            Diagnostic(
                code="DEPENDENCY_CYCLE",
                message=f"Explicit dependency cycle among {sorted(cyclic_symbols)}.",
                severity=Severity.ERROR,
                parameter_ids=tuple(cycle_parameters),
            )
        )
        unresolved.extend(
            UnresolvedTarget(
                parameter_id=parameter_id,
                relation_status=RelationStatus.CONFLICT,
                reason="DEPENDENCY_CYCLE",
            )
            for parameter_id in cycle_parameters
            if parameter_id not in {item.parameter_id for item in unresolved}
        )
        resolved = [
            item
            for item in resolved
            if item.target.parameter_id not in set(cycle_parameters)
        ]

    groups: dict[str, list[_ResolvedTarget]] = defaultdict(list)
    for item in resolved:
        root = _resolve_alias_root(item.root_symbol, alias_graph)
        groups[root].append(replace(item, root_symbol=root))

    variables: list[SearchVariable] = []
    for root, items in sorted(groups.items()):
        variable, group_diagnostics = _build_variable(root, items, circuit_context)
        variables.append(variable)
        diagnostics.extend(group_diagnostics)

    status = RelationStatus.VALID
    if any(item.relation_status == RelationStatus.CONFLICT for item in unresolved) or any(
        diagnostic.severity == Severity.ERROR for diagnostic in diagnostics
    ):
        status = RelationStatus.CONFLICT
    elif unresolved:
        status = RelationStatus.UNCERTAIN
    return CanonicalizationResult(
        variables=tuple(variables),
        unresolved_targets=tuple(sorted(unresolved, key=lambda item: item.parameter_id)),
        diagnostics=tuple(diagnostics),
        status=status,
    )


def _identity_evidence(target: StageATargetRecord) -> RelationEvidence:
    root = target.parameter_name or target.parameter_id
    return RelationEvidence(
        parameter_id=target.parameter_id,
        source_expression=root,
        resolved_expression=root,
        root_symbols=(root,),
        direct_symbol=root,
        owner_symbol=root,
        evidence=("no derived relation; target treated as direct Stage A binding",),
    )


def _has_conflicting_evidence(rows: list[RelationEvidence]) -> bool:
    signatures = {
        (
            item.source_expression,
            item.resolved_expression,
            item.normalized_expression,
            item.root_symbols,
            item.alias_chain,
            item.owner_symbol,
        )
        for item in rows
    }
    return len(signatures) > 1


def _cyclic_symbols(graph: Mapping[str, str]) -> set[str]:
    cyclic: set[str] = set()
    for start in graph:
        path: list[str] = []
        positions: dict[str, int] = {}
        node = start
        while node in graph:
            if node in positions:
                cyclic.update(path[positions[node] :])
                break
            positions[node] = len(path)
            path.append(node)
            node = graph[node]
    return cyclic


def _resolve_alias_root(root: str, graph: Mapping[str, str]) -> str:
    seen: set[str] = set()
    while root in graph and root not in seen:
        seen.add(root)
        root = graph[root]
    return root


def _build_variable(
    root: str,
    items: list[_ResolvedTarget],
    context: CircuitContext,
) -> tuple[SearchVariable, tuple[Diagnostic, ...]]:
    group_diagnostics: list[Diagnostic] = []
    classifications = [classify_domain(item.target) for item in items]
    domain_types = {item.domain_type for item in classifications}
    units = {item.unit for item in classifications}
    kinds = {item.target.parameter_kind for item in items}
    semantics = {item.target.semantic_category for item in items}
    relation_status = RelationStatus.VALID
    if len(domain_types) != 1 or len(kinds) != 1:
        relation_status = RelationStatus.CONFLICT
        group_diagnostics.append(
            Diagnostic(
                code="SHARED_ROOT_DOMAIN_CONFLICT",
                message=f"Root {root} controls incompatible parameter domains.",
                severity=Severity.ERROR,
                parameter_ids=tuple(sorted(item.target.parameter_id for item in items)),
            )
        )

    classification = classifications[0]
    domain = classification.domain_type if len(domain_types) == 1 else DomainType.UNSUPPORTED
    unit = classification.unit if len(units) == 1 else None
    members: list[str] = []
    dependents: list[DependentTarget] = []
    constraints: list[ConstraintRecord] = []
    all_evidence: list[str] = []
    for item in sorted(items, key=lambda value: value.target.parameter_id):
        all_evidence.extend(item.evidence.evidence)
        is_alias = item.owner_symbol != item.root_symbol
        if item.relation.relation_type == "IDENTITY" and not is_alias:
            members.append(item.target.parameter_id)
            continue
        relation_name = (
            "identity"
            if item.relation.relation_type in {"IDENTITY", "EXPLICIT_EQUALITY"}
            else "derived"
        )
        expression = item.relation.expression or item.root_symbol
        dependents.append(
            DependentTarget(
                parameter_id=item.target.parameter_id,
                relation=relation_name,
                expression=expression,
                evidence=item.evidence.evidence,
            )
        )
        constraints.append(
            ConstraintRecord(
                type=(
                    "EXPLICIT_EQUALITY"
                    if relation_name == "identity"
                    else item.relation.relation_type
                ),
                expression=f"{item.target.parameter_id} = {expression}",
                evidence=item.evidence.evidence,
            )
        )

    if len(members) > 1:
        constraints.insert(
            0,
            ConstraintRecord(
                type="SHARED_BINDING",
                expression=" = ".join((*members, root)),
                evidence=(f"shared explicit root symbol: {root}",),
            ),
        )

    nominal = context.nominal_values.get(root)
    if nominal is None:
        for item in items:
            if item.target.parameter_id in context.nominal_values:
                nominal = context.nominal_values[item.target.parameter_id]
                break

    range_status = classification.range_status
    range_reason = classification.range_reason
    if relation_status == RelationStatus.CONFLICT:
        range_status = RangeStatus.CONFLICT
        range_reason = "SHARED_ROOT_DOMAIN_CONFLICT"

    return (
        SearchVariable(
            variable_id=root,
            members=tuple(members),
            semantic_category=next(iter(semantics)) if len(semantics) == 1 else None,
            parameter_kind=next(iter(kinds)) if len(kinds) == 1 else None,
            domain_type=domain,
            independent=True,
            nominal_value=nominal,
            unit=unit,
            range=NumericRange(),
            constraints=tuple(constraints),
            dependent_targets=tuple(dependents),
            target_ranges=(),
            range_status=range_status,
            range_reason=range_reason,
            provider=None,
            relation_status=relation_status,
            evidence=tuple(dict.fromkeys(all_evidence)),
            diagnostics=tuple(group_diagnostics),
        ),
        tuple(group_diagnostics),
    )
