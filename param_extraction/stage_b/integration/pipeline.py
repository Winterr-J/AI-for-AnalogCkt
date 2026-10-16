"""Compose the generic Stage B v1 admissible-search-space document."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..constraints import extract_relation_evidence
from ..core.models import (
    CircuitContext,
    Diagnostic,
    OperatingContext,
    RelationEvidence,
    SearchSpace,
    TechnologyContext,
)
from ..core.types import RelationStatus, Severity
from ..generic import canonicalize_variables, select_optimization_targets
from ..providers.base import RangeProvider
from ..ranges import construct_and_propagate_ranges


def construct_admissible_search_space(
    stage_a_rows: Iterable[object | Mapping[str, Any]],
    *,
    circuit_ir: object | None,
    circuit_context: CircuitContext,
    operating_context: OperatingContext,
    technology_context: TechnologyContext,
    relation_evidence: Iterable[RelationEvidence] | None = None,
    range_provider: RangeProvider | None = None,
) -> SearchSpace:
    """Build independent dimensions from frozen Stage A decisions and IR facts."""

    selection = select_optimization_targets(stage_a_rows)
    selected_ids = {item.parameter_id for item in selection.selected}
    if relation_evidence is None:
        evidence = (
            extract_relation_evidence(circuit_ir, selected_ids)
            if circuit_ir is not None
            else ()
        )
    else:
        evidence = tuple(relation_evidence)

    diagnostics = list(selection.diagnostics)
    if any(item.circuit_id != circuit_context.circuit_id for item in selection.selected):
        diagnostics.append(
            Diagnostic(
                code="CIRCUIT_CONTEXT_MISMATCH",
                message="Circuit context does not match selected Stage A rows.",
                severity=Severity.ERROR,
                parameter_ids=tuple(sorted(selected_ids)),
            )
        )

    canonical = canonicalize_variables(selection.selected, evidence, circuit_context)
    diagnostics.extend(canonical.diagnostics)
    variables = list(canonical.variables)
    status = canonical.status
    if range_provider is not None:
        propagation = construct_and_propagate_ranges(
            variables,
            selection.selected,
            provider=range_provider,
            circuit_context=circuit_context,
            operating_context=operating_context,
            technology_context=technology_context,
        )
        variables = list(propagation.variables)
        diagnostics.extend(propagation.diagnostics)
        status = _combine_status(status, propagation.status)
    elif variables and status != RelationStatus.CONFLICT:
        status = RelationStatus.UNCERTAIN

    if not selection.selected:
        diagnostics.append(
            Diagnostic(
                code="NO_ELIGIBLE_OPTIMIZATION_VARIABLES",
                message="Stage A supplied no optimization_eligible=true targets.",
                severity=Severity.WARNING,
            )
        )
        if status != RelationStatus.CONFLICT:
            status = RelationStatus.UNCERTAIN

    if any(item.severity == Severity.ERROR for item in diagnostics):
        status = RelationStatus.CONFLICT
    return SearchSpace(
        schema_version="stage_b_search_space_v1",
        circuit_id=circuit_context.circuit_id,
        circuit_type=circuit_context.circuit_type,
        technology=technology_context.technology,
        status=status,
        operating_context=operating_context,
        variables=tuple(variables),
        excluded_targets=selection.excluded,
        unresolved_targets=canonical.unresolved_targets,
        diagnostics=tuple(diagnostics),
    )


def construct_gf180_conditional_search_space(
    stage_a_rows: Iterable[object | Mapping[str, Any]],
    *,
    circuit_ir: object | None,
    circuit_context: CircuitContext,
    operating_context: OperatingContext,
    technology_context: TechnologyContext,
    relation_evidence: Iterable[RelationEvidence] | None = None,
    provider: object | None = None,
):
    """Run frozen Generic construction, then GF180 P3 branch construction.

    The import is local so the technology-neutral pipeline has no import-time
    dependency on GF180.  Existing Ideal construction and serialization are
    unchanged.
    """

    from ..providers.gf180.provider import GF180RangeProvider

    rows = tuple(stage_a_rows)
    generic = construct_admissible_search_space(
        rows,
        circuit_ir=circuit_ir,
        circuit_context=circuit_context,
        operating_context=operating_context,
        technology_context=technology_context,
        relation_evidence=relation_evidence,
        range_provider=None,
    )
    active_provider = provider or GF180RangeProvider()
    if not isinstance(active_provider, GF180RangeProvider):
        raise TypeError("GF180 conditional integration requires GF180RangeProvider")
    return active_provider.construct_conditional_space(
        generic,
        rows,
        circuit_context=circuit_context,
        operating_context=operating_context,
        technology_context=technology_context,
    )


def _combine_status(left: RelationStatus, right: RelationStatus) -> RelationStatus:
    if RelationStatus.CONFLICT in {left, right}:
        return RelationStatus.CONFLICT
    if RelationStatus.UNCERTAIN in {left, right}:
        return RelationStatus.UNCERTAIN
    return RelationStatus.VALID
