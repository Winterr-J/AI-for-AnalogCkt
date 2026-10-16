"""Extract only safe equality relations from resolved binding facts."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import re

from ..ir.circuit_ir import CircuitIR, DiagnosticIR, ParameterRelationIR


class ParameterRelationResolver:
    """Generate equality only for identical normalized single-root expressions."""

    def resolve(self, circuit: CircuitIR) -> CircuitIR:
        circuit.parameter_relations = []
        circuit.diagnostics = [
            diagnostic
            for diagnostic in circuit.diagnostics
            if diagnostic.code != "SHARED_ROOT_NON_EQUAL_EXPRESSIONS"
        ]
        by_root: dict[str, list] = defaultdict(list)
        for target in circuit.physical_targets:
            binding = target.binding
            if (
                binding is not None
                and binding.binding_status == "BOUND"
                and binding.binding_root_argument is not None
                and binding.normalized_expression is not None
            ):
                by_root[binding.binding_root_argument].append(target)

        for root in sorted(by_root):
            targets = by_root[root]
            by_expression: dict[str, list] = defaultdict(list)
            for target in targets:
                assert target.binding is not None
                by_expression[target.binding.normalized_expression].append(target)

            if len(by_expression) > 1 and len(targets) > 1:
                circuit.diagnostics.append(
                    DiagnosticIR(
                        code="SHARED_ROOT_NON_EQUAL_EXPRESSIONS",
                        message=(
                            f"Root {root} controls multiple targets through different "
                            "expressions; no cross-expression equality was generated."
                        ),
                        severity="info",
                    )
                )

            for normalized, group in sorted(by_expression.items()):
                if len(group) < 2:
                    continue
                members = sorted(target.parameter_id for target in group)
                source_expression = group[0].binding.binding_expression  # type: ignore[union-attr]
                digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
                safe_root = re.sub(r"[^A-Za-z0-9_]+", "_", root)
                circuit.parameter_relations.append(
                    ParameterRelationIR(
                        relation_id=f"EQ_{safe_root}_{digest}",
                        relation_type="equality",
                        members=members,
                        root_symbol=root,
                        source_expression=source_expression,
                        evidence=[
                            f"shared root binding argument: {root}",
                            f"identical normalized expression: {normalized}",
                            f"tied physical targets: {members}",
                        ],
                    )
                )
        return circuit
