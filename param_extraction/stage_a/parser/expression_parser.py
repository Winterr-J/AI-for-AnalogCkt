"""Safe static parsing of construction-side Python expressions."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Mapping

from ..ir.circuit_ir import ExpressionIR


@dataclass(frozen=True)
class StaticStringIR:
    text: str
    placeholders: dict[str, ExpressionIR]


class ExpressionParser:
    """Recover simple symbols, aliases, literals, arithmetic, and strings.

    This class never evaluates user source. Alias resolution retains static AST
    structure and symbol dependencies for the future Binding Resolver.
    """

    def __init__(self, aliases: Mapping[str, ast.AST] | None = None) -> None:
        self.aliases: dict[str, ast.AST] = dict(aliases or {})

    def set_alias(self, name: str, expression: ast.AST) -> None:
        self.aliases[name] = expression

    def parse(self, node: ast.AST | None) -> ExpressionIR:
        if node is None:
            return ExpressionIR.omitted()

        source_text = self._unparse(node)
        direct_symbol = node.id if isinstance(node, ast.Name) else None
        resolved, alias_chain, cycle = self._resolve(node, ())
        if cycle:
            return ExpressionIR(
                expression_type="unresolved",
                source_text=source_text,
                resolved_text=self._unparse(resolved),
                direct_symbol=direct_symbol,
                symbol_references=tuple(sorted(self._symbols(resolved))),
                alias_chain=alias_chain,
                ast_dump=ast.dump(resolved, include_attributes=False),
            )

        if isinstance(resolved, ast.Constant):
            expression_type = "literal"
        elif isinstance(resolved, ast.Name):
            expression_type = "alias" if alias_chain else "symbol"
        elif self._is_simple_arithmetic(resolved):
            expression_type = "arithmetic"
        else:
            expression_type = "unresolved"

        return ExpressionIR(
            expression_type=expression_type,
            source_text=source_text,
            resolved_text=self._unparse(resolved),
            direct_symbol=direct_symbol,
            symbol_references=tuple(sorted(self._symbols(resolved))),
            alias_chain=alias_chain,
            ast_dump=ast.dump(resolved, include_attributes=False),
        )

    def render_static_string(self, node: ast.AST) -> StaticStringIR | None:
        """Render static string concatenation/f-strings using safe placeholders."""

        placeholders: dict[str, ExpressionIR] = {}
        counter = 0

        def marker(value_node: ast.AST) -> str:
            nonlocal counter
            key = f"__STAGE_A_EXPR_{counter}__"
            counter += 1
            placeholders[key] = self.parse(value_node)
            return key

        def render(part: ast.AST) -> str | None:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                return part.value
            if isinstance(part, ast.BinOp) and isinstance(part.op, ast.Add):
                left = render(part.left)
                right = render(part.right)
                if left is None or right is None:
                    return None
                return left + right
            if (
                isinstance(part, ast.Call)
                and isinstance(part.func, ast.Name)
                and part.func.id == "str"
                and len(part.args) == 1
                and not part.keywords
            ):
                return marker(part.args[0])
            if isinstance(part, ast.JoinedStr):
                pieces: list[str] = []
                for value in part.values:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        pieces.append(value.value)
                    elif isinstance(value, ast.FormattedValue):
                        pieces.append(marker(value.value))
                    else:
                        return None
                return "".join(pieces)
            return None

        text = render(node)
        if text is None:
            return None
        return StaticStringIR(text=text, placeholders=placeholders)

    def _resolve(
        self, node: ast.AST, stack: tuple[str, ...]
    ) -> tuple[ast.AST, tuple[str, ...], bool]:
        if isinstance(node, ast.Name):
            if node.id not in self.aliases:
                return node, (), False
            if node.id in stack:
                return node, stack + (node.id,), True

            target = self.aliases[node.id]
            resolved, nested_chain, cycle = self._resolve(target, stack + (node.id,))
            if nested_chain:
                chain = nested_chain
            elif isinstance(resolved, ast.Name):
                chain = (resolved.id,)
            else:
                chain = tuple(sorted(self._symbols(resolved)))
            if not chain or chain[-1] != node.id:
                chain = chain + (node.id,)
            return resolved, chain, cycle

        if isinstance(node, ast.BinOp):
            left, left_chain, left_cycle = self._resolve(node.left, stack)
            right, right_chain, right_cycle = self._resolve(node.right, stack)
            resolved = ast.copy_location(ast.BinOp(left=left, op=node.op, right=right), node)
            return resolved, self._merge_chains(left_chain, right_chain), left_cycle or right_cycle

        if isinstance(node, ast.UnaryOp):
            operand, chain, cycle = self._resolve(node.operand, stack)
            resolved = ast.copy_location(ast.UnaryOp(op=node.op, operand=operand), node)
            return resolved, chain, cycle

        return node, (), False

    @staticmethod
    def _merge_chains(*chains: tuple[str, ...]) -> tuple[str, ...]:
        merged: list[str] = []
        for chain in chains:
            for name in chain:
                if name not in merged:
                    merged.append(name)
        return tuple(merged)

    @staticmethod
    def _symbols(node: ast.AST) -> set[str]:
        return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}

    @classmethod
    def _is_simple_arithmetic(cls, node: ast.AST) -> bool:
        if isinstance(node, (ast.Name, ast.Constant)):
            return True
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return cls._is_simple_arithmetic(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
        ):
            return cls._is_simple_arithmetic(node.left) and cls._is_simple_arithmetic(
                node.right
            )
        return False

    @staticmethod
    def _unparse(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return ast.dump(node, include_attributes=False)
