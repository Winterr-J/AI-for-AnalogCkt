"""Extract and safely parse only explicit topology relations.

No expression is evaluated.  The accepted grammar is numeric constants, names,
unary signs, and arithmetic operators.  Function calls, attributes, indexing,
and dynamic expressions remain explicit uncertainty.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Iterable

from ..core.models import RelationEvidence
from ..core.types import RelationStatus


@dataclass(frozen=True)
class ParsedExplicitRelation:
    status: RelationStatus
    relation_type: str
    roots: tuple[str, ...]
    expression: str | None
    reason: str | None = None


def extract_relation_evidence(
    circuit_ir: object,
    selected_parameter_ids: Iterable[str] | None = None,
) -> tuple[RelationEvidence, ...]:
    """Adapt Stage A CircuitIR binding/expression facts without reparsing source."""

    selected = set(selected_parameter_ids) if selected_parameter_ids is not None else None
    result: list[RelationEvidence] = []
    for target in getattr(circuit_ir, "physical_targets", ()):
        parameter_id = str(target.parameter_id)
        if selected is not None and parameter_id not in selected:
            continue
        binding = getattr(target, "binding", None)
        source = getattr(target, "source_expression", None)
        source_text = getattr(source, "source_text", None)
        resolved_text = getattr(source, "resolved_text", None)
        direct_symbol = getattr(source, "direct_symbol", None)
        alias_chain = tuple(getattr(source, "alias_chain", ()) or ())
        roots = tuple(getattr(binding, "root_symbol_dependencies", ()) or ())
        normalized = getattr(binding, "normalized_expression", None)
        binding_expression = getattr(binding, "binding_expression", None)
        owner_symbol = alias_chain[-1] if alias_chain else None
        result.append(
            RelationEvidence(
                parameter_id=parameter_id,
                source_expression=source_text,
                resolved_expression=resolved_text or binding_expression or source_text,
                normalized_expression=normalized,
                root_symbols=roots,
                alias_chain=alias_chain,
                direct_symbol=direct_symbol,
                owner_symbol=owner_symbol,
                evidence=tuple(getattr(binding, "evidence", ()) or ()),
            )
        )
    return tuple(sorted(result, key=lambda item: item.parameter_id))


def analyze_explicit_relation(evidence: RelationEvidence) -> ParsedExplicitRelation:
    expression = evidence.resolved_expression or evidence.source_expression
    if not expression:
        return ParsedExplicitRelation(
            status=RelationStatus.UNCERTAIN,
            relation_type="UNAVAILABLE",
            roots=(),
            expression=None,
            reason="RELATION_EXPRESSION_MISSING",
        )
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return ParsedExplicitRelation(
            status=RelationStatus.UNCERTAIN,
            relation_type="UNSUPPORTED",
            roots=tuple(sorted(set(evidence.root_symbols))),
            expression=expression,
            reason="RELATION_EXPRESSION_SYNTAX_ERROR",
        )

    if not _is_safe_arithmetic(node):
        return ParsedExplicitRelation(
            status=RelationStatus.UNCERTAIN,
            relation_type="UNSUPPORTED",
            roots=tuple(sorted(_names(node))),
            expression=_unparse(node),
            reason="UNSUPPORTED_RELATION_EXPRESSION",
        )

    roots = tuple(sorted(_names(node)))
    if len(roots) > 1:
        return ParsedExplicitRelation(
            status=RelationStatus.UNCERTAIN,
            relation_type="MULTI_ROOT",
            roots=roots,
            expression=_unparse(node),
            reason="MULTIPLE_INDEPENDENT_ROOTS",
        )
    if not roots:
        return ParsedExplicitRelation(
            status=RelationStatus.UNCERTAIN,
            relation_type="CONSTANT",
            roots=(),
            expression=_unparse(node),
            reason="NO_INDEPENDENT_ROOT",
        )

    root = roots[0]
    original = _parse_name(evidence.source_expression)
    explicit_alias = bool(evidence.alias_chain) and original is not None and original != root
    if isinstance(node, ast.Name):
        relation_type = "EXPLICIT_EQUALITY" if explicit_alias else "IDENTITY"
    elif _is_scaled_root(node, root):
        relation_type = "EXPLICIT_RATIO"
    elif _is_reciprocal_root(node, root):
        relation_type = "EXPLICIT_RECIPROCAL"
    else:
        relation_type = "EXPLICIT_DERIVED"
    return ParsedExplicitRelation(
        status=RelationStatus.VALID,
        relation_type=relation_type,
        roots=(root,),
        expression=_unparse(node),
    )


def _parse_name(text: str | None) -> str | None:
    if not text:
        return None
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        return None
    return node.id if isinstance(node, ast.Name) else None


def _names(node: ast.AST) -> set[str]:
    return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}


def _is_numeric(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _is_safe_arithmetic(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) or _is_numeric(node):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_safe_arithmetic(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
    ):
        return _is_safe_arithmetic(node.left) and _is_safe_arithmetic(node.right)
    return False


def _is_root(node: ast.AST, root: str) -> bool:
    return isinstance(node, ast.Name) and node.id == root


def _is_scaled_root(node: ast.AST, root: str) -> bool:
    if not isinstance(node, ast.BinOp):
        return False
    if isinstance(node.op, ast.Mult):
        return (_is_numeric(node.left) and _is_root(node.right, root)) or (
            _is_root(node.left, root) and _is_numeric(node.right)
        )
    if isinstance(node.op, ast.Div):
        return _is_root(node.left, root) and _is_numeric(node.right)
    return False


def _is_reciprocal_root(node: ast.AST, root: str) -> bool:
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and _is_numeric(node.left)
        and _is_root(node.right, root)
    )


def _unparse(node: ast.AST) -> str:
    return ast.unparse(node) if hasattr(ast, "unparse") else ast.dump(node)
