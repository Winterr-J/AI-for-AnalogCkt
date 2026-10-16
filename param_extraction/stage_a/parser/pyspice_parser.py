"""Static AST parser for PySpice circuit-construction code.

The parser never imports or executes a user circuit module. It extracts only
construction statements and records unsupported structures as diagnostics.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Iterable

from ..ir.canonical_templates import (
    generate_device_targets,
    model_target,
    source_target,
    subcircuit_choice_target,
)
from ..ir.circuit_ir import (
    CircuitIR,
    DeviceIR,
    DiagnosticIR,
    ModelIR,
    PhysicalTargetIR,
    SourceIR,
    SubcircuitIR,
)
from .expression_parser import ExpressionParser
from .raw_spice_parser import RawElementIR, RawSpiceParser


_CIRCUIT_ID = re.compile(r"circuit_[0-9]{4}", re.IGNORECASE)
_CONSTRUCTORS = {"MOSFET", "BJT", "R", "C", "L", "D", "V", "I", "X", "model"}


class PySpiceParser:
    """Parse one PySpice source file into a technology-independent Circuit IR."""

    def __init__(self) -> None:
        self.expression_parser = ExpressionParser()
        self.raw_parser = RawSpiceParser(self.expression_parser)
        self.ir: CircuitIR | None = None
        self.circuit_variable = "circuit"
        self._instance_ids: set[str] = set()
        self._target_ids: set[str] = set()

    def parse_source(
        self,
        source: str,
        *,
        circuit_type: str,
        circuit_id: str | None = None,
        source_name: str = "<memory>",
    ) -> CircuitIR:
        inferred_id = circuit_id or self._infer_circuit_id(source_name) or "circuit_unknown"
        self.ir = CircuitIR(circuit_id=inferred_id, circuit_type=circuit_type)
        self.expression_parser = ExpressionParser()
        self.raw_parser = RawSpiceParser(self.expression_parser)
        self._instance_ids = set()
        self._target_ids = set()

        try:
            tree = ast.parse(source, filename=source_name)
        except SyntaxError as exc:
            self._diagnostic(
                "PYTHON_SYNTAX_ERROR",
                exc.msg,
                line=exc.lineno,
                column=exc.offset,
                severity="error",
            )
            return self.ir

        function = self._find_build_function(tree)
        if function is None:
            self._diagnostic(
                "BUILD_CIRCUIT_NOT_FOUND",
                "No build_circuit function was found.",
                severity="error",
            )
            return self.ir

        self.ir.python_arguments = self._function_arguments(function)
        self._process_statements(function.body)
        return self.ir

    def parse_file(
        self,
        path: str | Path,
        *,
        circuit_type: str,
        circuit_id: str | None = None,
    ) -> CircuitIR:
        source_path = Path(path)
        return self.parse_source(
            source_path.read_text(encoding="utf-8"),
            circuit_type=circuit_type,
            circuit_id=circuit_id,
            source_name=str(source_path),
        )

    def _process_statements(self, statements: Iterable[ast.stmt]) -> None:
        assert self.ir is not None
        for statement in statements:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name):
                    if self._is_circuit_assignment(statement.value):
                        self.circuit_variable = target.id
                        self._recover_id_from_circuit_title(statement.value)
                    else:
                        parsed_before_binding = self.expression_parser.parse(statement.value)
                        self.ir.local_aliases[target.id] = parsed_before_binding
                        self.expression_parser.set_alias(target.id, statement.value)
                    continue

            if isinstance(statement, ast.AugAssign) and self._is_raw_spice_target(
                statement.target
            ):
                if not isinstance(statement.op, ast.Add):
                    self._diagnostic(
                        "RAW_SPICE_UNSUPPORTED_OPERATION",
                        "Only raw_spice += expressions are supported.",
                        line=statement.lineno,
                    )
                    continue
                rendered = self.expression_parser.render_static_string(statement.value)
                if rendered is None:
                    self._diagnostic(
                        "RAW_SPICE_DYNAMIC_STRING",
                        "raw_spice expression cannot be resolved statically.",
                        line=statement.lineno,
                    )
                    continue
                elements, diagnostics = self.raw_parser.parse_static_string(
                    rendered, source_line=statement.lineno
                )
                for diagnostic in diagnostics:
                    self._diagnostic(
                        diagnostic.code, diagnostic.message, line=diagnostic.line
                    )
                for element in elements:
                    self._add_raw_element(element)
                continue

            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                if self._is_constructor_call(statement.value):
                    self._parse_constructor(statement.value)
                continue

            if isinstance(statement, (ast.Return, ast.Pass)):
                continue

            if isinstance(statement, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
                self._diagnostic(
                    "UNSUPPORTED_CONSTRUCTION_CONTROL_FLOW",
                    f"Construction control flow {type(statement).__name__} is not parsed.",
                    line=getattr(statement, "lineno", None),
                )

    @staticmethod
    def _find_build_function(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build_circuit":
                return node
        return None

    @staticmethod
    def _function_arguments(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
        if function.args.vararg:
            arguments.append(function.args.vararg)
        if function.args.kwarg:
            arguments.append(function.args.kwarg)
        return [argument.arg for argument in arguments]

    @staticmethod
    def _is_circuit_assignment(value: ast.AST) -> bool:
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "Circuit"
        )

    def _recover_id_from_circuit_title(self, value: ast.AST) -> None:
        assert self.ir is not None
        if not isinstance(value, ast.Call) or not value.args:
            return
        title = self._literal_string(value.args[0])
        match = _CIRCUIT_ID.search(title or "")
        if match and self.ir.circuit_id == "circuit_unknown":
            self.ir.circuit_id = match.group(0).lower()

    def _is_raw_spice_target(self, target: ast.AST) -> bool:
        return (
            isinstance(target, ast.Attribute)
            and target.attr == "raw_spice"
            and isinstance(target.value, ast.Name)
            and target.value.id == self.circuit_variable
        )

    def _is_constructor_call(self, call: ast.Call) -> bool:
        return (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == self.circuit_variable
            and call.func.attr in _CONSTRUCTORS
        )

    def _parse_constructor(self, call: ast.Call) -> None:
        method = call.func.attr  # type: ignore[union-attr]
        if method == "MOSFET":
            self._parse_mosfet(call)
        elif method == "BJT":
            self._parse_bjt(call)
        elif method in {"R", "C", "L"}:
            self._parse_passive(call, method)
        elif method == "D":
            self._parse_diode(call)
        elif method in {"V", "I"}:
            self._parse_source(call, method)
        elif method == "X":
            self._parse_subcircuit(call)
        elif method == "model":
            self._parse_model_declaration(call)

    def _parse_mosfet(self, call: ast.Call) -> None:
        if len(call.args) < 5:
            self._malformed(call, "MOSFET requires name, drain, gate, source, and bulk")
            return
        original_id, device_id = self._constructor_id("M", call.args[0], call)
        keywords = self._keywords(call)
        model_node = keywords.get("model") or self._arg(call, 5)
        model_expression = self.expression_parser.parse(model_node) if model_node else None
        device_type = self._infer_polarity("mos", model_expression)
        expressions = self._device_expressions(
            keywords,
            {
                "width": "W",
                "w": "W",
                "length": "L",
                "l": "L",
                "multiplier": "m",
                "m": "m",
                "nf": "nf",
            },
        )
        terminals = {
            "drain": self._node(call.args[1]),
            "gate": self._node(call.args[2]),
            "source": self._node(call.args[3]),
            "bulk": self._node(call.args[4]),
        }
        self._add_device(
            DeviceIR(
                device_id=device_id,
                original_device_id=original_id,
                device_type=device_type,
                terminals=terminals,
                model_reference=model_expression,
                explicit_parameter_expressions=expressions,
                source_location=self._location(call),
            )
        )

    def _parse_bjt(self, call: ast.Call) -> None:
        if len(call.args) < 4:
            self._malformed(call, "BJT requires name, collector, base, and emitter")
            return
        original_id, device_id = self._constructor_id("Q", call.args[0], call)
        keywords = self._keywords(call)
        model_node = keywords.get("model") or self._arg(call, 4)
        model_expression = self.expression_parser.parse(model_node) if model_node else None
        terminals = {
            "collector": self._node(call.args[1]),
            "base": self._node(call.args[2]),
            "emitter": self._node(call.args[3]),
        }
        substrate = keywords.get("substrate")
        if substrate is not None:
            terminals["substrate"] = self._node(substrate)
        expressions = self._device_expressions(
            keywords, {"area": "area", "multiplier": "m", "m": "m"}
        )
        self._add_device(
            DeviceIR(
                device_id=device_id,
                original_device_id=original_id,
                device_type=self._infer_polarity("bjt", model_expression),
                terminals=terminals,
                model_reference=model_expression,
                explicit_parameter_expressions=expressions,
                source_location=self._location(call),
            )
        )

    def _parse_passive(self, call: ast.Call, method: str) -> None:
        if len(call.args) < 3:
            self._malformed(call, f"{method} requires name and two nodes")
            return
        family = {"R": "resistor", "C": "capacitor", "L": "inductor"}[method]
        value_slot = {"R": "resistance", "C": "capacitance", "L": "inductance"}[
            method
        ]
        original_id, device_id = self._constructor_id(method, call.args[0], call)
        keywords = self._keywords(call)
        value_node = self._arg(call, 3) or keywords.get(value_slot) or keywords.get("value")
        expressions: dict[str, object] = {}
        if value_node is not None:
            expressions[value_slot] = self.expression_parser.parse(value_node)
        alias_map = {
            "width": "W",
            "w": "W",
            "length": "L",
            "l": "L",
            "multiplier": "m",
            "m": "m",
        }
        expressions.update(self._device_expressions(keywords, alias_map))
        model_node = keywords.get("model")
        model_expression = self.expression_parser.parse(model_node) if model_node else None
        self._add_device(
            DeviceIR(
                device_id=device_id,
                original_device_id=original_id,
                device_type=family,
                terminals={"p": self._node(call.args[1]), "n": self._node(call.args[2])},
                model_reference=model_expression,
                explicit_parameter_expressions=expressions,  # type: ignore[arg-type]
                source_location=self._location(call),
            )
        )

    def _parse_diode(self, call: ast.Call) -> None:
        if len(call.args) < 3:
            self._malformed(call, "D requires name, anode, and cathode")
            return
        original_id, device_id = self._constructor_id("D", call.args[0], call)
        keywords = self._keywords(call)
        model_node = keywords.get("model") or self._arg(call, 3)
        model_expression = self.expression_parser.parse(model_node) if model_node else None
        expressions = self._device_expressions(
            keywords, {"area": "area", "multiplier": "m", "m": "m"}
        )
        self._add_device(
            DeviceIR(
                device_id=device_id,
                original_device_id=original_id,
                device_type="diode",
                terminals={
                    "anode": self._node(call.args[1]),
                    "cathode": self._node(call.args[2]),
                },
                model_reference=model_expression,
                explicit_parameter_expressions=expressions,
                source_location=self._location(call),
            )
        )

    def _parse_source(self, call: ast.Call, method: str) -> None:
        if len(call.args) < 3:
            self._malformed(call, f"{method} requires name and two nodes")
            return
        original_name = self._literal_string(call.args[0])
        if original_name is None:
            self._malformed(call, f"{method} source name must be statically known")
            return
        prefix = method
        base_id = original_name if original_name.upper().startswith(prefix) else prefix + original_name
        source_id = self._allocate_instance_id(base_id, call)
        keywords = self._keywords(call)
        value_node = self._arg(call, 3) or keywords.get("value") or keywords.get("dc_value")
        value_expression = self.expression_parser.parse(value_node)
        parameter_id = original_name if not original_name.isdigit() else base_id
        source_type = "voltage_source" if method == "V" else "current_source"
        terminals = {"p": self._node(call.args[1]), "n": self._node(call.args[2])}
        source = SourceIR(
            source_id=source_id,
            original_source_id=base_id,
            source_type=source_type,
            terminals=terminals,
            value_expression=value_expression,
            physical_target=parameter_id,
            source_location=self._location(call),
        )
        assert self.ir is not None
        self.ir.sources.append(source)
        self.ir.add_nodes(*terminals.values())
        self._add_target(
            source_target(
                parameter_id=parameter_id,
                source_id=source_id,
                source_type=source_type,
                value_expression=value_expression,
            )
        )

    def _parse_subcircuit(self, call: ast.Call) -> None:
        if len(call.args) < 2:
            self._malformed(call, "X requires instance name and subcircuit reference")
            return
        original_id, instance_id = self._constructor_id("X", call.args[0], call)
        reference = self.expression_parser.parse(call.args[1])
        nodes = [self._node(node) for node in call.args[2:]]
        exposed = {
            keyword.arg: self.expression_parser.parse(keyword.value)
            for keyword in call.keywords
            if keyword.arg is not None
        }
        target = subcircuit_choice_target(instance_id=instance_id, expression=reference)
        subcircuit = SubcircuitIR(
            instance_id=instance_id,
            original_instance_id=original_id,
            subcircuit_reference=reference,
            nodes=nodes,
            exposed_parameters=exposed,
            canonical_parameter_targets=[target.parameter_id],
            source_location=self._location(call),
        )
        assert self.ir is not None
        self.ir.subcircuits.append(subcircuit)
        self.ir.add_nodes(*nodes)
        self._add_target(target)
        for name, expression in exposed.items():
            self._add_target(
                PhysicalTargetIR(
                    parameter_id=f"{instance_id}.{name}",
                    device_id=instance_id,
                    device_type="subcircuit",
                    parameter_kind="exposed_parameter",
                    source_kind="subcircuit",
                    parameter_name=expression.direct_symbol,
                    source_expression=expression,
                )
            )
            subcircuit.canonical_parameter_targets.append(f"{instance_id}.{name}")

    def _parse_model_declaration(self, call: ast.Call) -> None:
        if not call.args:
            self._malformed(call, "model requires a model name")
            return
        name_expression = self.expression_parser.parse(call.args[0])
        model_id = self._expression_identifier(name_expression) or "anonymous_model"
        model_type = self._literal_string(self._arg(call, 1)) if self._arg(call, 1) else None
        declared = {
            keyword.arg: self.expression_parser.parse(keyword.value)
            for keyword in call.keywords
            if keyword.arg is not None
        }
        self._register_model(
            model_id=model_id,
            model_type=model_type,
            expression=name_expression,
            declared_parameters=declared,
        )

    def _add_device(self, device: DeviceIR) -> None:
        assert self.ir is not None
        self.ir.devices.append(device)
        self.ir.add_nodes(*device.terminals.values())
        for target in generate_device_targets(device):
            self._add_target(target)
        if device.model_reference is not None:
            self._register_model_reference(device.model_reference)

    def _add_raw_element(self, raw: RawElementIR) -> None:
        assert self.ir is not None
        if raw.element_kind == "subcircuit":
            original = raw.original_id
            instance_id = self._allocate_instance_id(original, None, line=raw.source_line)
            reference = raw.subcircuit_reference or self.expression_parser.parse(None)
            target = subcircuit_choice_target(instance_id=instance_id, expression=reference)
            subcircuit = SubcircuitIR(
                instance_id=instance_id,
                original_instance_id=original,
                subcircuit_reference=reference,
                nodes=list(raw.terminals.values()),
                exposed_parameters=raw.parameters,
                canonical_parameter_targets=[target.parameter_id],
                source_location=f"raw_spice:{raw.source_line}",
            )
            self.ir.subcircuits.append(subcircuit)
            self.ir.add_nodes(*raw.terminals.values())
            self._add_target(target)
            return

        device_id = self._allocate_instance_id(raw.original_id, None, line=raw.source_line)
        device_type = self._infer_polarity(raw.element_kind, raw.model_reference)
        device = DeviceIR(
            device_id=device_id,
            original_device_id=raw.original_id,
            device_type=device_type,
            terminals=raw.terminals,
            model_reference=raw.model_reference,
            explicit_parameter_expressions=raw.parameters,
            source_location=f"raw_spice:{raw.source_line}",
        )
        self._add_device(device)

    def _register_model_reference(self, expression) -> None:
        model_id = self._expression_identifier(expression)
        if model_id is None:
            return
        self._register_model(
            model_id=model_id,
            model_type=None,
            expression=expression,
            declared_parameters={},
        )

    def _register_model(
        self,
        *,
        model_id: str,
        model_type: str | None,
        expression,
        declared_parameters: dict,
    ) -> None:
        assert self.ir is not None
        existing = next((model for model in self.ir.models if model.model_id == model_id), None)
        if existing is not None:
            if model_type and not existing.model_type:
                existing.model_type = model_type
            existing.declared_parameters.update(declared_parameters)
            return
        target_id = model_id
        model = ModelIR(
            model_id=model_id,
            model_type=model_type,
            reference_expression=expression,
            declared_parameters=declared_parameters,
            physical_target=target_id,
        )
        self.ir.models.append(model)
        self._add_target(model_target(parameter_id=target_id, expression=expression, device_id=None))

    def _add_target(self, target: PhysicalTargetIR) -> None:
        assert self.ir is not None
        if target.parameter_id in self._target_ids:
            return
        self._target_ids.add(target.parameter_id)
        self.ir.physical_targets.append(target)

    def _constructor_id(
        self, prefix: str, name_node: ast.AST, call: ast.Call
    ) -> tuple[str, str]:
        raw_name = self._literal_string(name_node)
        if raw_name is None:
            raw_name = f"UNRESOLVED_LINE_{call.lineno}"
            self._diagnostic(
                "DYNAMIC_INSTANCE_NAME",
                "Instance name is not statically known.",
                line=call.lineno,
            )
        original_id = raw_name if raw_name.upper().startswith(prefix) else prefix + raw_name
        return original_id, self._allocate_instance_id(original_id, call)

    def _allocate_instance_id(
        self,
        base_id: str,
        node: ast.AST | None,
        *,
        line: int | None = None,
    ) -> str:
        if base_id not in self._instance_ids:
            self._instance_ids.add(base_id)
            return base_id
        index = 2
        while f"{base_id}_dup{index}" in self._instance_ids:
            index += 1
        normalized = f"{base_id}_dup{index}"
        self._instance_ids.add(normalized)
        self._diagnostic(
            "DUPLICATE_INSTANCE_ID",
            f"Duplicate {base_id} normalized to {normalized}; no instance was overwritten.",
            line=line or getattr(node, "lineno", None),
        )
        return normalized

    def _device_expressions(
        self, keywords: dict[str, ast.AST], aliases: dict[str, str]
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for keyword, suffix in aliases.items():
            if keyword in keywords:
                result[suffix] = self.expression_parser.parse(keywords[keyword])
        return result

    @staticmethod
    def _keywords(call: ast.Call) -> dict[str, ast.AST]:
        return {keyword.arg.lower(): keyword.value for keyword in call.keywords if keyword.arg}

    @staticmethod
    def _arg(call: ast.Call, index: int) -> ast.AST | None:
        return call.args[index] if len(call.args) > index else None

    def _node(self, node: ast.AST) -> str:
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "gnd"
            and isinstance(node.value, ast.Name)
            and node.value.id == self.circuit_variable
        ):
            return "0"
        literal = self._literal_value(node)
        if literal is not None:
            return str(literal)
        try:
            return ast.unparse(node)
        except Exception:
            return ast.dump(node, include_attributes=False)

    @staticmethod
    def _literal_value(node: ast.AST | None):
        if node is None:
            return None
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError):
            return None

    @classmethod
    def _literal_string(cls, node: ast.AST | None) -> str | None:
        value = cls._literal_value(node)
        return value if isinstance(value, str) else None

    @classmethod
    def _expression_identifier(cls, expression) -> str | None:
        if expression.direct_symbol:
            return expression.direct_symbol
        if expression.expression_type == "literal" and expression.resolved_text:
            try:
                value = ast.literal_eval(expression.resolved_text)
            except (ValueError, SyntaxError):
                return None
            return str(value)
        return None

    @classmethod
    def _infer_polarity(cls, family: str, model_expression) -> str:
        if family not in {"mos", "bjt"}:
            return family
        text = ""
        if model_expression is not None:
            text = " ".join(
                filter(
                    None,
                    [
                        model_expression.source_text,
                        model_expression.resolved_text,
                        model_expression.direct_symbol,
                    ],
                )
            ).lower()
        if family == "mos":
            if "pmos" in text or "pfet" in text:
                return "pmos"
            if "nmos" in text or "nfet" in text:
                return "nmos"
            return "mos"
        if "pnp" in text:
            return "bjt_pnp"
        if "npn" in text:
            return "bjt_npn"
        return "bjt"

    @staticmethod
    def _location(node: ast.AST) -> str:
        return f"python:{getattr(node, 'lineno', '?')}"

    @staticmethod
    def _infer_circuit_id(source_name: str) -> str | None:
        match = _CIRCUIT_ID.search(source_name)
        return match.group(0).lower() if match else None

    def _malformed(self, node: ast.AST, message: str) -> None:
        self._diagnostic("MALFORMED_CONSTRUCTOR", message, line=getattr(node, "lineno", None))

    def _diagnostic(
        self,
        code: str,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        severity: str = "warning",
    ) -> None:
        assert self.ir is not None
        self.ir.diagnostics.append(
            DiagnosticIR(
                code=code,
                message=message,
                severity=severity,
                line=line,
                column=column,
            )
        )


def parse_pyspice_source(
    source: str,
    *,
    circuit_type: str,
    circuit_id: str | None = None,
    source_name: str = "<memory>",
) -> CircuitIR:
    return PySpiceParser().parse_source(
        source,
        circuit_type=circuit_type,
        circuit_id=circuit_id,
        source_name=source_name,
    )


def parse_pyspice_file(
    path: str | Path, *, circuit_type: str, circuit_id: str | None = None
) -> CircuitIR:
    return PySpiceParser().parse_file(
        path, circuit_type=circuit_type, circuit_id=circuit_id
    )
