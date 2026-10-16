"""Map target-local Ideal constraints back to independent roots."""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
import math
from typing import Iterable

from ..core.models import (
    CircuitContext,
    Diagnostic,
    NumericRange,
    OperatingContext,
    RangeDecision,
    SearchVariable,
    StageATargetRecord,
    TargetRangeRecord,
    TargetRangeRequest,
    TechnologyContext,
)
from ..core.types import RangeStatus, RelationStatus, Severity
from ..generic.domain_classifier import classify_domain
from ..providers.base import RangeProvider
from .interval import (
    IntervalDecision,
    contains_zero,
    intersect_intervals,
    ordered_range,
    validate_interval,
)


@dataclass(frozen=True)
class PropagationResult:
    variables: tuple[SearchVariable, ...]
    diagnostics: tuple[Diagnostic, ...]
    status: RelationStatus


@dataclass(frozen=True)
class _WorkItem:
    target: StageATargetRecord
    nominal: float | int | None
    relation: str
    expression: str
    local: RangeDecision
    relation_evidence: tuple[str, ...]


@dataclass(frozen=True)
class _MappingDecision:
    range: NumericRange
    status: RangeStatus
    reason: str
    evidence: tuple[str, ...] = ()


def construct_and_propagate_ranges(
    variables: Iterable[SearchVariable],
    targets: Iterable[StageATargetRecord],
    *,
    provider: RangeProvider,
    circuit_context: CircuitContext,
    operating_context: OperatingContext,
    technology_context: TechnologyContext,
) -> PropagationResult:
    target_by_id = {target.parameter_id: target for target in targets}
    updated: list[SearchVariable] = []
    diagnostics: list[Diagnostic] = []
    aggregate = RelationStatus.VALID
    for variable in variables:
        result, variable_diagnostics = _construct_variable_range(
            variable,
            target_by_id,
            provider,
            circuit_context,
            operating_context,
            technology_context,
        )
        updated.append(result)
        diagnostics.extend(variable_diagnostics)
        if result.range_status == RangeStatus.CONFLICT:
            aggregate = RelationStatus.CONFLICT
        elif result.range_status == RangeStatus.UNCERTAIN and aggregate != RelationStatus.CONFLICT:
            aggregate = RelationStatus.UNCERTAIN
    return PropagationResult(tuple(updated), tuple(diagnostics), aggregate)


def _construct_variable_range(
    variable: SearchVariable,
    target_by_id: dict[str, StageATargetRecord],
    provider: RangeProvider,
    circuit_context: CircuitContext,
    operating_context: OperatingContext,
    technology_context: TechnologyContext,
) -> tuple[SearchVariable, tuple[Diagnostic, ...]]:
    diagnostics: list[Diagnostic] = list(variable.diagnostics)
    dependent_by_id = {item.parameter_id: item for item in variable.dependent_targets}
    work: list[_WorkItem] = []
    missing = [
        parameter_id
        for parameter_id in (*variable.members, *dependent_by_id)
        if parameter_id not in target_by_id
    ]
    if missing:
        diagnostic = Diagnostic(
            code="MISSING_TARGET_METADATA",
            message=f"Missing Stage A metadata for targets {sorted(missing)}.",
            severity=Severity.ERROR,
            parameter_ids=tuple(sorted(missing)),
        )
        diagnostics.append(diagnostic)
        return (
            replace(
                variable,
                range=NumericRange(),
                target_ranges=(),
                range_status=RangeStatus.CONFLICT,
                range_reason="MISSING_TARGET_METADATA",
                diagnostics=tuple(diagnostics),
            ),
            tuple(diagnostics),
        )

    for parameter_id in variable.members:
        target = target_by_id[parameter_id]
        work.append(
            _make_work_item(
                target,
                variable,
                relation="identity",
                expression=variable.variable_id,
                relation_evidence=(f"direct member of root {variable.variable_id}",),
                provider=provider,
                circuit_context=circuit_context,
                operating_context=operating_context,
                technology_context=technology_context,
                dependent=False,
            )
        )
    for dependent in variable.dependent_targets:
        target = target_by_id[dependent.parameter_id]
        work.append(
            _make_work_item(
                target,
                variable,
                relation=dependent.relation,
                expression=dependent.expression,
                relation_evidence=dependent.evidence,
                provider=provider,
                circuit_context=circuit_context,
                operating_context=operating_context,
                technology_context=technology_context,
                dependent=True,
            )
        )

    for item in work:
        diagnostics.extend(item.local.diagnostics)

    mapped: dict[str, _MappingDecision] = {}
    current: NumericRange | None = None
    conflict_reason: str | None = None
    uncertain_reason: str | None = None

    direct = [item for item in work if item.relation == "identity" and item.target.parameter_id in variable.members]
    nonreciprocal = [
        item for item in work if item not in direct and not _is_reciprocal(item.expression, variable.variable_id)
    ]
    reciprocal = [
        item for item in work if item not in direct and _is_reciprocal(item.expression, variable.variable_id)
    ]

    for item in (*direct, *nonreciprocal, *reciprocal):
        if item.local.status == RangeStatus.CONFLICT:
            mapping = _MappingDecision(NumericRange(), RangeStatus.CONFLICT, item.local.reason)
        elif item.local.status == RangeStatus.UNCERTAIN:
            mapping = _MappingDecision(NumericRange(), RangeStatus.UNCERTAIN, item.local.reason)
        elif item in direct:
            checked = validate_interval(item.local.range)
            mapping = _MappingDecision(checked.range, checked.status, checked.reason)
        else:
            mapping = _invert_to_root(
                item.expression,
                variable.variable_id,
                item.local.range,
                current,
            )
        mapped[item.target.parameter_id] = mapping

        if mapping.status == RangeStatus.CONFLICT:
            conflict_reason = conflict_reason or mapping.reason
            diagnostics.append(
                _range_diagnostic(mapping.reason, item.target.parameter_id, conflict=True)
            )
            continue
        if mapping.status == RangeStatus.UNCERTAIN:
            uncertain_reason = uncertain_reason or mapping.reason
            diagnostics.append(
                _range_diagnostic(mapping.reason, item.target.parameter_id, conflict=False)
            )
            continue
        if current is None:
            current = mapping.range
        else:
            intersection = intersect_intervals(current, mapping.range)
            if intersection.status == RangeStatus.CONFLICT:
                conflict_reason = conflict_reason or intersection.reason
                diagnostics.append(
                    _range_diagnostic(
                        intersection.reason,
                        item.target.parameter_id,
                        conflict=True,
                    )
                )
            elif intersection.status == RangeStatus.UNCERTAIN:
                uncertain_reason = uncertain_reason or intersection.reason
                diagnostics.append(
                    _range_diagnostic(
                        intersection.reason,
                        item.target.parameter_id,
                        conflict=False,
                    )
                )
            else:
                current = intersection.range

    if conflict_reason is None and current is not None:
        physical = _validate_physical_domain(variable, current)
        if physical.status == RangeStatus.CONFLICT:
            conflict_reason = physical.reason
            diagnostics.append(
                _range_diagnostic(physical.reason, variable.variable_id, conflict=True)
            )
        elif physical.status == RangeStatus.UNCERTAIN:
            uncertain_reason = uncertain_reason or physical.reason
            diagnostics.append(
                _range_diagnostic(physical.reason, variable.variable_id, conflict=False)
            )
        else:
            current = physical.range

    target_ranges = tuple(
        _target_record(item, mapped[item.target.parameter_id])
        for item in sorted(work, key=lambda value: value.target.parameter_id)
    )
    if conflict_reason is not None:
        status = RangeStatus.CONFLICT
        reason = conflict_reason
    elif uncertain_reason is not None or current is None:
        status = RangeStatus.UNCERTAIN
        reason = uncertain_reason or "NO_VALID_TARGET_LOCAL_RANGE"
    else:
        status = RangeStatus.VALID
        reason = "ADMISSIBLE_RANGE_CONSTRUCTED"

    provider_names = {item.local.provider for item in work if item.local.provider}
    provider_name = next(iter(provider_names)) if len(provider_names) == 1 else None
    final_range = current if current is not None else NumericRange()
    return (
        replace(
            variable,
            range=final_range,
            target_ranges=target_ranges,
            range_status=status,
            range_reason=reason,
            provider=provider_name,
            evidence=tuple(
                dict.fromkeys(
                    (
                        *variable.evidence,
                        "target-local ranges propagated to independent root",
                        "final range is the intersection of all valid mapped constraints",
                    )
                )
            ),
            diagnostics=tuple(diagnostics),
        ),
        tuple(diagnostics),
    )


def _make_work_item(
    target: StageATargetRecord,
    variable: SearchVariable,
    *,
    relation: str,
    expression: str,
    relation_evidence: tuple[str, ...],
    provider: RangeProvider,
    circuit_context: CircuitContext,
    operating_context: OperatingContext,
    technology_context: TechnologyContext,
    dependent: bool,
) -> _WorkItem:
    classification = classify_domain(target)
    nominal = _nominal_for_target(target, variable, circuit_context, dependent=dependent)
    request = TargetRangeRequest(
        parameter_id=target.parameter_id,
        parameter_name=target.parameter_name,
        device_id=target.device_id,
        device_type=target.device_type,
        parameter_kind=target.parameter_kind,
        semantic_category=target.semantic_category,
        domain_type=classification.domain_type,
        unit=classification.unit,
        nominal_value=nominal,
    )
    local = provider.construct_target_range(
        request,
        circuit_context,
        operating_context,
        technology_context,
    )
    return _WorkItem(target, nominal, relation, expression, local, relation_evidence)


def _nominal_for_target(
    target: StageATargetRecord,
    variable: SearchVariable,
    context: CircuitContext,
    *,
    dependent: bool,
) -> float | int | None:
    values = context.nominal_values
    if target.parameter_id in values:
        return values[target.parameter_id]
    if not dependent and target.parameter_name and target.parameter_name in values:
        return values[target.parameter_name]
    if not dependent and variable.variable_id in values:
        return values[variable.variable_id]
    return None


def _invert_to_root(
    expression: str,
    root: str,
    local: NumericRange,
    current_root: NumericRange | None,
) -> _MappingDecision:
    checked = validate_interval(local)
    if checked.status != RangeStatus.VALID:
        return _MappingDecision(checked.range, checked.status, checked.reason)
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return _MappingDecision(NumericRange(), RangeStatus.UNCERTAIN, "UNSUPPORTED_RELATION_RANGE_PROPAGATION")
    assert local.lower is not None and local.upper is not None
    lower = float(local.lower)
    upper = float(local.upper)
    if isinstance(node, ast.Name) and node.id == root:
        return _MappingDecision(local, RangeStatus.VALID, "IDENTITY_RANGE_PROPAGATION")

    linear = _linear_coefficient(node, root)
    if linear is not None:
        if not math.isfinite(linear) or linear == 0:
            return _MappingDecision(NumericRange(), RangeStatus.UNCERTAIN, "NONINVERTIBLE_EXPLICIT_RELATION")
        fixed_sign = _mapped_fixed_sign(local.fixed_sign, linear)
        decision = ordered_range(
            lower / linear,
            upper / linear,
            scale=local.scale,
            transform=local.transform,
            fixed_sign=fixed_sign,
        )
        return _MappingDecision(
            decision.range,
            decision.status,
            "LINEAR_RELATION_INVERSE_PROPAGATION" if decision.status == RangeStatus.VALID else decision.reason,
            (f"inverse coefficient: 1/{linear}",),
        )

    reciprocal = _reciprocal_coefficient(node, root)
    if reciprocal is not None:
        if not math.isfinite(reciprocal) or reciprocal == 0:
            return _MappingDecision(NumericRange(), RangeStatus.UNCERTAIN, "UNSAFE_RECIPROCAL_RANGE_PROPAGATION")
        if contains_zero(local) or current_root is None or contains_zero(current_root):
            return _MappingDecision(NumericRange(), RangeStatus.UNCERTAIN, "UNSAFE_RECIPROCAL_RANGE_PROPAGATION")
        fixed_sign = _reciprocal_fixed_sign(local.fixed_sign, reciprocal)
        decision = ordered_range(
            reciprocal / lower,
            reciprocal / upper,
            scale=local.scale,
            transform=local.transform,
            fixed_sign=fixed_sign,
        )
        return _MappingDecision(
            decision.range,
            decision.status,
            "RECIPROCAL_RELATION_INVERSE_PROPAGATION" if decision.status == RangeStatus.VALID else decision.reason,
            (f"safe reciprocal coefficient: {reciprocal}",),
        )
    return _MappingDecision(NumericRange(), RangeStatus.UNCERTAIN, "UNSUPPORTED_RELATION_RANGE_PROPAGATION")


def _linear_coefficient(node: ast.AST, root: str) -> float | None:
    if isinstance(node, ast.Name) and node.id == root:
        return 1.0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        if isinstance(node.operand, ast.Name) and node.operand.id == root:
            return 1.0 if isinstance(node.op, ast.UAdd) else -1.0
    if not isinstance(node, ast.BinOp):
        return None
    left_number = _constant_value(node.left)
    right_number = _constant_value(node.right)
    left_root = isinstance(node.left, ast.Name) and node.left.id == root
    right_root = isinstance(node.right, ast.Name) and node.right.id == root
    if isinstance(node.op, ast.Mult):
        if left_number is not None and right_root:
            return left_number
        if left_root and right_number is not None:
            return right_number
    if isinstance(node.op, ast.Div) and left_root and right_number is not None and right_number != 0:
        return 1.0 / right_number
    return None


def _reciprocal_coefficient(node: ast.AST, root: str) -> float | None:
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
        return None
    numerator = _constant_value(node.left)
    denominator_is_root = isinstance(node.right, ast.Name) and node.right.id == root
    return numerator if numerator is not None and denominator_is_root else None


def _is_reciprocal(expression: str, root: str) -> bool:
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return False
    return _reciprocal_coefficient(node, root) is not None


def _constant_value(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _constant_value(node.operand)
        if value is not None:
            return value if isinstance(node.op, ast.UAdd) else -value
    return None


def _mapped_fixed_sign(sign: str | None, coefficient: float) -> str | None:
    if sign is None or coefficient > 0:
        return sign
    return "negative" if sign == "positive" else "positive" if sign == "negative" else sign


def _reciprocal_fixed_sign(sign: str | None, coefficient: float) -> str | None:
    return _mapped_fixed_sign(sign, coefficient)


def _validate_physical_domain(variable: SearchVariable, value: NumericRange) -> IntervalDecision:
    checked = validate_interval(value)
    if checked.status != RangeStatus.VALID:
        return checked
    assert value.lower is not None and value.upper is not None
    kind = (variable.parameter_kind or "").strip().lower()
    positive_only = kind in {
        "width",
        "w",
        "length",
        "l",
        "resistance",
        "resistor",
        "r",
        "capacitance",
        "capacitor",
        "c",
        "inductance",
        "inductor",
    }
    if positive_only and (float(value.lower) <= 0 or float(value.upper) <= 0):
        return IntervalDecision(value, RangeStatus.CONFLICT, "PHYSICAL_DOMAIN_VIOLATION")
    if value.scale == "log" and (float(value.lower) <= 0 or float(value.upper) <= 0):
        return IntervalDecision(value, RangeStatus.CONFLICT, "PHYSICAL_DOMAIN_VIOLATION")
    return IntervalDecision(value, RangeStatus.VALID, "PHYSICAL_DOMAIN_VALID")


def _target_record(item: _WorkItem, mapping: _MappingDecision) -> TargetRangeRecord:
    return TargetRangeRecord(
        parameter_id=item.target.parameter_id,
        nominal_value=item.nominal,
        relation=item.relation,
        expression=item.expression,
        local_range=item.local.range,
        local_status=item.local.status,
        local_reason=item.local.reason,
        mapped_to_root=mapping.range,
        mapping_status=mapping.status,
        mapping_reason=mapping.reason,
        provider=item.local.provider,
        evidence=tuple(
            dict.fromkeys((*item.local.evidence, *item.relation_evidence, *mapping.evidence))
        ),
        diagnostics=item.local.diagnostics,
    )


def _range_diagnostic(code: str, parameter_id: str, *, conflict: bool) -> Diagnostic:
    return Diagnostic(
        code=code,
        message=f"Range construction for {parameter_id}: {code}.",
        severity=Severity.ERROR if conflict else Severity.WARNING,
        parameter_ids=(parameter_id,),
    )
