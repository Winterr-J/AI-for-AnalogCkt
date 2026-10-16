"""Lightweight parser for statically recoverable ``circuit.raw_spice`` lines."""

from __future__ import annotations

from dataclasses import dataclass, field
import ast
import shlex

from ..ir.circuit_ir import ExpressionIR
from .expression_parser import ExpressionParser, StaticStringIR


@dataclass
class RawElementIR:
    original_id: str
    element_kind: str
    terminals: dict[str, str]
    model_reference: ExpressionIR | None = None
    parameters: dict[str, ExpressionIR] = field(default_factory=dict)
    subcircuit_reference: ExpressionIR | None = None
    source_line: int | None = None


@dataclass
class RawParseDiagnostic:
    code: str
    message: str
    line: int | None = None


class RawSpiceParser:
    """Parse common Q/M/R/C/L/D/X records without running SPICE."""

    def __init__(self, expression_parser: ExpressionParser) -> None:
        self.expression_parser = expression_parser

    def parse_static_string(
        self, rendered: StaticStringIR, *, source_line: int | None = None
    ) -> tuple[list[RawElementIR], list[RawParseDiagnostic]]:
        elements: list[RawElementIR] = []
        diagnostics: list[RawParseDiagnostic] = []
        for offset, line in enumerate(rendered.text.splitlines()):
            line_number = source_line + offset if source_line is not None else None
            stripped = line.strip()
            if not stripped or stripped.startswith("*") or stripped.startswith("."):
                continue
            try:
                element = self._parse_line(stripped, rendered.placeholders, line_number)
            except (ValueError, IndexError) as exc:
                diagnostics.append(
                    RawParseDiagnostic(
                        code="RAW_SPICE_PARSE_ERROR",
                        message=f"{stripped}: {exc}",
                        line=line_number,
                    )
                )
                continue
            if element is None:
                diagnostics.append(
                    RawParseDiagnostic(
                        code="RAW_SPICE_UNSUPPORTED_ELEMENT",
                        message=f"Unsupported raw SPICE record: {stripped}",
                        line=line_number,
                    )
                )
            else:
                elements.append(element)
        return elements, diagnostics

    def _parse_line(
        self,
        line: str,
        placeholders: dict[str, ExpressionIR],
        source_line: int | None,
    ) -> RawElementIR | None:
        tokens = shlex.split(line, posix=True)
        if not tokens:
            return None
        original_id = tokens[0]
        designator = original_id[0].upper()
        positional, named = self._split_parameters(tokens[1:], placeholders)

        if designator == "M":
            if len(positional) < 5:
                raise ValueError("MOS record requires D G S B model")
            return RawElementIR(
                original_id=original_id,
                element_kind="mos",
                terminals=dict(zip(("drain", "gate", "source", "bulk"), positional[:4])),
                model_reference=self._token_expression(positional[4], placeholders),
                parameters=self._canonical_named(named),
                source_line=source_line,
            )

        if designator == "Q":
            if len(positional) < 4:
                raise ValueError("BJT record requires C B E model")
            model_token = positional[-1]
            node_tokens = positional[:-1]
            terminal_names = ["collector", "base", "emitter", "substrate"]
            return RawElementIR(
                original_id=original_id,
                element_kind="bjt",
                terminals=dict(zip(terminal_names, node_tokens[:4])),
                model_reference=self._token_expression(model_token, placeholders),
                parameters=self._canonical_named(named),
                source_line=source_line,
            )

        if designator in {"R", "C", "L"}:
            if len(positional) < 3:
                raise ValueError(f"{designator} record requires two nodes and a value")
            family = {"R": "resistor", "C": "capacitor", "L": "inductor"}[designator]
            value_slot = {"R": "resistance", "C": "capacitance", "L": "inductance"}[
                designator
            ]
            parameters = self._canonical_named(named)
            parameters[value_slot] = self._token_expression(positional[2], placeholders)
            return RawElementIR(
                original_id=original_id,
                element_kind=family,
                terminals={"p": positional[0], "n": positional[1]},
                parameters=parameters,
                source_line=source_line,
            )

        if designator == "D":
            if len(positional) < 3:
                raise ValueError("Diode record requires anode cathode model")
            return RawElementIR(
                original_id=original_id,
                element_kind="diode",
                terminals={"anode": positional[0], "cathode": positional[1]},
                model_reference=self._token_expression(positional[2], placeholders),
                parameters=self._canonical_named(named),
                source_line=source_line,
            )

        if designator == "X":
            if len(positional) < 2:
                raise ValueError("Subcircuit record requires nodes and a subcircuit name")
            return RawElementIR(
                original_id=original_id,
                element_kind="subcircuit",
                terminals={f"node_{index}": node for index, node in enumerate(positional[:-1])},
                subcircuit_reference=self._token_expression(positional[-1], placeholders),
                parameters={key: value for key, value in named.items()},
                source_line=source_line,
            )
        return None

    @staticmethod
    def _split_parameters(
        tokens: list[str], placeholders: dict[str, ExpressionIR]
    ) -> tuple[list[str], dict[str, ExpressionIR]]:
        positional: list[str] = []
        named: dict[str, ExpressionIR] = {}
        for token in tokens:
            if "=" not in token:
                positional.append(token)
                continue
            key, value = token.split("=", 1)
            named[key.lower()] = RawSpiceParser._token_expression(value, placeholders)
        return positional, named

    @staticmethod
    def _canonical_named(named: dict[str, ExpressionIR]) -> dict[str, ExpressionIR]:
        aliases = {
            "w": "W",
            "width": "W",
            "l": "L",
            "length": "L",
            "m": "m",
            "multiplier": "m",
            "nf": "nf",
            "area": "area",
        }
        return {aliases.get(key, key): value for key, value in named.items()}

    @staticmethod
    def _token_expression(
        token: str, placeholders: dict[str, ExpressionIR]
    ) -> ExpressionIR:
        if token in placeholders:
            return placeholders[token]
        try:
            value = float(token)
        except ValueError:
            node: ast.AST = ast.Constant(token)
        else:
            node = ast.Constant(value)
        return ExpressionParser().parse(node)
