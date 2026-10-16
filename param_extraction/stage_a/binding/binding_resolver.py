"""Resolve build-argument bindings from Circuit IR without reparsing Python."""

from __future__ import annotations

from ..ir.circuit_ir import BindingIR, CircuitIR, DiagnosticIR, PhysicalTargetIR
from .relation_resolver import ParameterRelationResolver


class BindingResolver:
    """Attach deterministic binding facts to canonical physical targets.

    This resolver consumes only Circuit IR and Expression IR. It does not assign
    validity, semantic category, testbench scope, or optimization eligibility.
    """

    _DIAGNOSTIC_CODES = {
        "ALIAS_CYCLE",
        "BINDING_MULTIPLE_ROOTS",
        "BINDING_UNSUPPORTED_EXPRESSION",
        "BINDING_UNRESOLVED_DATAFLOW",
        "ORPHAN_BUILD_ARGUMENT",
        "SHARED_ROOT_NON_EQUAL_EXPRESSIONS",
    }

    def resolve(self, circuit: CircuitIR) -> CircuitIR:
        circuit.diagnostics = [
            diagnostic
            for diagnostic in circuit.diagnostics
            if diagnostic.code not in self._DIAGNOSTIC_CODES
        ]
        argument_set = set(circuit.python_arguments)
        referenced_arguments: set[str] = set()

        for target in circuit.physical_targets:
            binding = self._resolve_target(target, argument_set, circuit)
            target.binding = binding
            target.parameter_name = binding.parameter_name
            referenced_arguments.update(binding.root_symbol_dependencies)

        for argument in sorted(argument_set - referenced_arguments):
            circuit.diagnostics.append(
                DiagnosticIR(
                    code="ORPHAN_BUILD_ARGUMENT",
                    message=(
                        f"Build argument {argument} does not control any recovered "
                        "physical target."
                    ),
                    severity="info",
                )
            )

        ParameterRelationResolver().resolve(circuit)
        return circuit

    def _resolve_target(
        self,
        target: PhysicalTargetIR,
        argument_set: set[str],
        circuit: CircuitIR,
    ) -> BindingIR:
        expression = target.source_expression
        roots = sorted(set(expression.symbol_references) & argument_set)
        common = {
            "parameter_id": target.parameter_id,
            "binding_expression": expression.resolved_text or expression.source_text,
            "normalized_expression": expression.ast_dump,
            "root_symbol_dependencies": roots,
            "alias_chain": list(expression.alias_chain),
        }

        if expression.expression_type == "omitted":
            return BindingIR(
                parameter_name=None,
                binding_status="DECLARED_UNBOUND",
                binding_root_argument=None,
                binding_reason="UNBOUND_OMITTED",
                evidence=["canonical physical target omitted from implementation"],
                review_status="AUTO",
                confidence="HIGH",
                **common,
            )

        if expression.expression_type == "literal":
            literal = expression.resolved_text or expression.source_text or "<literal>"
            return BindingIR(
                parameter_name=None,
                binding_status="DECLARED_UNBOUND",
                binding_root_argument=None,
                binding_reason="UNBOUND_LITERAL",
                evidence=[f"hard-coded literal: {literal}"],
                review_status="AUTO",
                confidence="HIGH",
                **common,
            )

        if expression.expression_type == "unresolved":
            if self._has_alias_cycle(expression.alias_chain):
                reason = "ALIAS_CYCLE"
                circuit.diagnostics.append(
                    DiagnosticIR(
                        code="ALIAS_CYCLE",
                        message=(
                            f"Alias cycle prevents binding resolution for "
                            f"{target.parameter_id}: {list(expression.alias_chain)}"
                        ),
                    )
                )
            else:
                reason = "UNSUPPORTED_EXPRESSION"
                circuit.diagnostics.append(
                    DiagnosticIR(
                        code="BINDING_UNSUPPORTED_EXPRESSION",
                        message=(
                            f"Unsupported expression for {target.parameter_id}: "
                            f"{expression.source_text}"
                        ),
                    )
                )
            return BindingIR(
                parameter_name=None,
                binding_status="UNCERTAIN",
                binding_root_argument=None,
                binding_reason=reason,
                evidence=self._expression_evidence(expression, roots, reason),
                review_status="NEEDS_REVIEW",
                confidence="LOW",
                **common,
            )

        if len(roots) == 1:
            root = roots[0]
            return BindingIR(
                parameter_name=root,
                binding_status="BOUND",
                binding_root_argument=root,
                binding_reason=self._bound_reason(expression.expression_type),
                evidence=self._expression_evidence(expression, roots, "BOUND"),
                review_status="AUTO",
                confidence="HIGH",
                **common,
            )

        if len(roots) > 1:
            circuit.diagnostics.append(
                DiagnosticIR(
                    code="BINDING_MULTIPLE_ROOTS",
                    message=(
                        f"{target.parameter_id} depends on multiple independent build "
                        f"arguments: {roots}"
                    ),
                )
            )
            return BindingIR(
                parameter_name=None,
                binding_status="UNCERTAIN",
                binding_root_argument=None,
                binding_reason="MULTIPLE_INDEPENDENT_ROOT_ARGUMENTS",
                evidence=self._expression_evidence(
                    expression, roots, "multiple independent root arguments"
                ),
                review_status="NEEDS_REVIEW",
                confidence="LOW",
                **common,
            )

        if self._has_unsupported_control_flow(circuit):
            circuit.diagnostics.append(
                DiagnosticIR(
                    code="BINDING_UNRESOLVED_DATAFLOW",
                    message=(
                        f"{target.parameter_id} has no recoverable build-argument root "
                        "and construction control flow was not parsed."
                    ),
                )
            )
            return BindingIR(
                parameter_name=None,
                binding_status="UNCERTAIN",
                binding_root_argument=None,
                binding_reason="UNRESOLVED_CONTROL_FLOW",
                evidence=self._expression_evidence(
                    expression, roots, "unresolved construction control flow"
                ),
                review_status="NEEDS_REVIEW",
                confidence="LOW",
                **common,
            )

        return BindingIR(
            parameter_name=None,
            binding_status="DECLARED_UNBOUND",
            binding_root_argument=None,
            binding_reason="UNBOUND_NO_ROOT_ARGUMENT",
            evidence=self._expression_evidence(
                expression, roots, "no external build argument controls this target"
            ),
            review_status="AUTO",
            confidence="HIGH",
            **common,
        )

    @staticmethod
    def _bound_reason(expression_type: str) -> str:
        return {
            "symbol": "BOUND_DIRECT",
            "alias": "BOUND_ALIAS",
            "arithmetic": "BOUND_SINGLE_ROOT_EXPRESSION",
        }.get(expression_type, "BOUND_STATIC_EXPRESSION")

    @staticmethod
    def _expression_evidence(expression, roots: list[str], reason: str) -> list[str]:
        evidence = [f"binding reason: {reason}"]
        if roots:
            evidence.append(f"root arguments: {roots}")
        if expression.resolved_text or expression.source_text:
            evidence.append(
                f"expression: {expression.resolved_text or expression.source_text}"
            )
        if expression.alias_chain:
            evidence.append(f"alias chain: {list(expression.alias_chain)}")
        return evidence

    @staticmethod
    def _has_alias_cycle(alias_chain: tuple[str, ...]) -> bool:
        return len(alias_chain) != len(set(alias_chain))

    @staticmethod
    def _has_unsupported_control_flow(circuit: CircuitIR) -> bool:
        return any(
            diagnostic.code == "UNSUPPORTED_CONSTRUCTION_CONTROL_FLOW"
            for diagnostic in circuit.diagnostics
        )


def resolve_bindings(circuit: CircuitIR) -> CircuitIR:
    return BindingResolver().resolve(circuit)
