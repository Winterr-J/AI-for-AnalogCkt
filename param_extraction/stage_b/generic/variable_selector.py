"""Select Stage A-approved optimizer targets without reclassifying them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Any

from ..core.models import Diagnostic, ExcludedTarget, StageATargetRecord
from ..core.types import Severity


@dataclass(frozen=True)
class SelectionResult:
    selected: tuple[StageATargetRecord, ...]
    excluded: tuple[ExcludedTarget, ...]
    diagnostics: tuple[Diagnostic, ...]


def select_optimization_targets(
    rows: Iterable[object | Mapping[str, Any]],
) -> SelectionResult:
    """Use the frozen Stage A eligibility decision as the only selection gate."""

    records = tuple(StageATargetRecord.from_row(row) for row in rows)
    seen: set[str] = set()
    selected: list[StageATargetRecord] = []
    excluded: list[ExcludedTarget] = []
    diagnostics: list[Diagnostic] = []
    circuit_keys = {(row.circuit_id, row.circuit_type) for row in records}
    if len(circuit_keys) > 1:
        diagnostics.append(
            Diagnostic(
                code="MIXED_CIRCUIT_INPUT",
                message="Stage B input rows must belong to exactly one circuit.",
                severity=Severity.ERROR,
                parameter_ids=tuple(sorted(row.parameter_id for row in records)),
            )
        )

    for row in records:
        if row.parameter_id in seen:
            diagnostics.append(
                Diagnostic(
                    code="DUPLICATE_STAGE_A_TARGET",
                    message=f"Duplicate Stage A row for {row.parameter_id}.",
                    severity=Severity.ERROR,
                    parameter_ids=(row.parameter_id,),
                )
            )
            continue
        seen.add(row.parameter_id)
        if row.optimization_eligible:
            selected.append(row)
        else:
            excluded.append(
                ExcludedTarget(
                    parameter_id=row.parameter_id,
                    reason="STAGE_A_OPTIMIZATION_ELIGIBLE_FALSE",
                )
            )

    return SelectionResult(
        selected=tuple(sorted(selected, key=lambda item: item.parameter_id)),
        excluded=tuple(sorted(excluded, key=lambda item: item.parameter_id)),
        diagnostics=tuple(diagnostics),
    )
