"""Circuit-type-neutral PySpice testbench construction primitives."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Sequence

import numpy as np

from .errors import BindingValidationError
from .models import SemanticTerminalBinding


_SPICE_NUMBER = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(?P<suffix>meg|[tgkmunpf])?$",
    re.IGNORECASE,
)
_SPICE_SUFFIXES = {
    "t": 1.0e12,
    "g": 1.0e9,
    "meg": 1.0e6,
    "k": 1.0e3,
    "m": 1.0e-3,
    "u": 1.0e-6,
    "n": 1.0e-9,
    "p": 1.0e-12,
    "f": 1.0e-15,
}
_GROUND_NAMES = {"0", "gnd", "vss"}


def _finite(value: Any, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise BindingValidationError(f"{label} must be numeric") from error
    if not math.isfinite(result):
        raise BindingValidationError(f"{label} must be finite")
    return result


def _spice_float(value: str) -> float:
    match = _SPICE_NUMBER.fullmatch(value.strip())
    if match is None:
        raise ValueError(value)
    number = float(match.group("number"))
    suffix = match.group("suffix")
    return number * (1.0 if suffix is None else _SPICE_SUFFIXES[suffix.casefold()])


@dataclass(frozen=True)
class SourceSpecification:
    """Independent-source DC and small-signal AC specification."""

    dc_value: float | None = None
    ac_magnitude: float | None = None
    ac_phase_deg: float | None = None

    def validated(self) -> "SourceSpecification":
        dc_value = None if self.dc_value is None else _finite(self.dc_value, label="source DC value")
        magnitude = (
            None
            if self.ac_magnitude is None
            else _finite(self.ac_magnitude, label="source AC magnitude")
        )
        phase = (
            None
            if self.ac_phase_deg is None
            else _finite(self.ac_phase_deg, label="source AC phase")
        )
        if magnitude is not None and magnitude < 0.0:
            raise BindingValidationError("source AC magnitude cannot be negative")
        if phase is not None and magnitude is None:
            raise BindingValidationError("source AC phase requires an AC magnitude")
        return SourceSpecification(dc_value, magnitude, phase)

    def to_pyspice_value(self) -> float | str:
        spec = self.validated()
        dc = 0.0 if spec.dc_value is None else spec.dc_value
        if spec.ac_magnitude is None:
            return dc
        phase = 0.0 if spec.ac_phase_deg is None else spec.ac_phase_deg
        return f"DC {dc:.17g} AC {spec.ac_magnitude:.17g} {phase:.17g}"


@dataclass(frozen=True)
class TerminalRemap:
    element: str
    terminal: str
    original_node: str
    new_node: str


@dataclass(frozen=True)
class FeedbackEdgeRequest:
    from_node: str
    test_source_name: str = "VTEST"
    dc_value: float = 0.0
    ac_magnitude: float = 1.0
    ac_phase_deg: float = 0.0
    to_node: str | None = None
    to_terminal: SemanticTerminalBinding | None = None
    temporary_node_base: str = "FB_IN"
    remove_elements: tuple[str, ...] = ()
    feedback_factor: float = 1.0
    unity_feedback_confirmed: bool = True


@dataclass(frozen=True)
class LoadAttachment:
    output_node: str
    reference_node: str = "0"
    capacitance_f: float | None = None
    resistance_ohm: float | None = None
    differential: bool = False
    name: str = "LOAD"
    provenance: str = "testbench"


@dataclass(frozen=True)
class NativeOperatingPoint:
    node_voltages: Mapping[str, float]
    branch_currents: Mapping[str, float]
    simulation_valid: bool
    error: str | None = None


@dataclass
class TestbenchMetadata:
    mode: str
    source_overrides: list[dict[str, Any]] = field(default_factory=list)
    temporary_nodes: list[str] = field(default_factory=list)
    terminal_remaps: list[dict[str, Any]] = field(default_factory=list)
    removed_elements: list[str] = field(default_factory=list)
    feedback_edge: dict[str, Any] | None = None
    load: dict[str, Any] | None = None
    nodeset_used: bool = False
    nodeset: dict[str, float] = field(default_factory=dict)
    native_op_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TestbenchBuild:
    circuit: Any
    metadata: Mapping[str, Any]
    native_operating_point: NativeOperatingPoint | None = None


def _mapping_item_casefold(mapping: Any, name: str) -> Any:
    target = name.casefold()
    for key, value in mapping.items():
        if str(key).casefold() == target:
            return value
    raise KeyError(name)


def probe_node_voltage(analysis: Any, node: str) -> np.ndarray:
    """Return the raw real/complex vector for one node."""

    return np.asarray(_mapping_item_casefold(analysis.nodes, node))


def probe_differential_voltage(
    analysis: Any,
    positive_node: str,
    negative_node: str,
) -> np.ndarray:
    return probe_node_voltage(analysis, positive_node) - probe_node_voltage(
        analysis, negative_node
    )


def probe_voltage_source_branch_current(analysis: Any, source: str) -> np.ndarray:
    """Return the raw branch-current vector for an independent voltage source."""

    name = source.casefold()
    candidates = (name, name[1:] if name.startswith("v") else f"v{name}")
    for candidate in candidates:
        try:
            return np.asarray(_mapping_item_casefold(analysis.branches, candidate))
        except KeyError:
            continue
    raise KeyError(source)


def _scalar_mapping(values: Any) -> dict[str, float]:
    result: dict[str, float] = {}
    for name, value in values.items():
        array = np.asarray(value).reshape(-1)
        if array.size:
            result[str(name)] = float(np.real(array[0]))
    return result


def capture_native_operating_point(analysis: Any) -> NativeOperatingPoint:
    """Convert a PySpice OP analysis to circuit-type-neutral structured data."""

    return NativeOperatingPoint(
        node_voltages=_scalar_mapping(analysis.nodes),
        branch_currents=_scalar_mapping(analysis.branches),
        simulation_valid=True,
    )


def _native_value(values: Mapping[str, float], name: str) -> float:
    target = name.casefold()
    for key, value in values.items():
        if str(key).casefold() == target:
            return float(value)
    raise BindingValidationError(f"native operating point has no node {name!r}")


def derive_feedback_dc_offset(
    native_operating_point: NativeOperatingPoint,
    *,
    output_node: str,
    feedback_input_node: str,
) -> float:
    """Derive VTEST DC from native OP while preserving source orientation."""

    if not native_operating_point.simulation_valid:
        raise BindingValidationError("cannot derive feedback offset from invalid native OP")
    output = _native_value(native_operating_point.node_voltages, output_node)
    input_value = _native_value(
        native_operating_point.node_voltages, feedback_input_node
    )
    return output - input_value


def native_op_nodeset(
    native_operating_point: NativeOperatingPoint,
) -> dict[str, float]:
    """Copy a valid native OP into decorated-testbench nodeset metadata."""

    if not native_operating_point.simulation_valid:
        raise BindingValidationError("cannot build nodeset from invalid native OP")
    return {
        str(node): float(value)
        for node, value in native_operating_point.node_voltages.items()
        if str(node).casefold() not in _GROUND_NAMES and math.isfinite(float(value))
    }


def run_native_operating_point(
    circuit: Any,
    *,
    temperature: float = 25.0,
    nominal_temperature: float = 25.0,
) -> NativeOperatingPoint:
    """Run only a native OP and return structured values, never OpAmp metrics."""

    try:
        simulator = circuit.simulator(
            temperature=float(temperature),
            nominal_temperature=float(nominal_temperature),
        )
        return capture_native_operating_point(simulator.operating_point())
    except Exception as error:
        return NativeOperatingPoint(
            node_voltages={},
            branch_currents={},
            simulation_valid=False,
            error=f"{type(error).__name__}: {error}",
        )


def run_decorated_operating_point(
    circuit: Any,
    *,
    temperature: float = 25.0,
    nominal_temperature: float = 25.0,
) -> NativeOperatingPoint:
    """Run OP after decoration/nodeset using the same structured contract."""

    return run_native_operating_point(
        circuit,
        temperature=temperature,
        nominal_temperature=nominal_temperature,
    )


class GenericTestbenchDecorator:
    """Apply temporary, in-memory-only structural edits to one circuit instance."""

    def __init__(self, circuit: Any, *, mode: str) -> None:
        self.circuit = circuit
        self.metadata = TestbenchMetadata(mode=str(mode))
        self._source_specs: dict[str, SourceSpecification] = {}
        self._native_op: NativeOperatingPoint | None = None

    def allocate_temporary_node(self, base: str = "TB_NODE") -> str:
        candidate = str(base)
        existing = {str(name).casefold() for name in self.circuit.node_names}
        existing.update(name.casefold() for name in self.metadata.temporary_nodes)
        index = 1
        while candidate.casefold() in existing:
            index += 1
            candidate = f"{base}_{index}"
        self.metadata.temporary_nodes.append(candidate)
        return candidate

    def _parse_source(self, element: Any) -> SourceSpecification:
        value = element.dc_value
        try:
            return SourceSpecification(dc_value=float(value))
        except (TypeError, ValueError):
            tokens = str(value).replace("=", " ").split()
            dc: float | None = None
            magnitude: float | None = None
            phase: float | None = None
            index = 0
            if tokens and tokens[0].casefold() not in {"dc", "ac"}:
                dc = _spice_float(tokens[0])
                index = 1
            while index < len(tokens):
                token = tokens[index].casefold()
                if token == "dc" and index + 1 < len(tokens):
                    dc = _spice_float(tokens[index + 1])
                    index += 2
                elif token == "ac" and index + 1 < len(tokens):
                    magnitude = _spice_float(tokens[index + 1])
                    phase = _spice_float(tokens[index + 2]) if index + 2 < len(tokens) else 0.0
                    index += 3
                else:
                    index += 1
            return SourceSpecification(dc, magnitude, phase).validated()

    def _replace_source(self, element_name: str, spec: SourceSpecification) -> None:
        try:
            element = self.circuit[element_name]
        except Exception as error:
            raise BindingValidationError(
                f"source override element {element_name!r} does not exist"
            ) from error
        full_name = str(element.name)
        prefix = full_name[:1].casefold()
        if prefix not in {"v", "i"}:
            raise BindingValidationError(
                f"source override element {element_name!r} is not an independent source"
            )
        nodes = [str(node) for node in element.node_names]
        if len(nodes) != 2:
            raise BindingValidationError(
                f"source override element {element_name!r} has unsupported terminals"
            )
        suffix = full_name[1:]
        value = spec.to_pyspice_value()
        element.detach()
        if prefix == "v":
            self.circuit.V(suffix, nodes[0], nodes[1], value)
        else:
            self.circuit.I(suffix, nodes[0], nodes[1], value)

    def apply_source_overrides(
        self,
        overrides: Mapping[str, SourceSpecification | Mapping[str, Any]],
    ) -> None:
        """Apply any number of coordinated DC/AC source specifications."""

        if not isinstance(overrides, Mapping):
            raise BindingValidationError("source overrides must be a mapping")
        for element_name, raw in overrides.items():
            try:
                element = self.circuit[element_name]
            except Exception as error:
                raise BindingValidationError(
                    f"source override element {element_name!r} does not exist"
                ) from error
            current = self._source_specs.get(str(element_name)) or self._parse_source(element)
            if isinstance(raw, SourceSpecification):
                requested = raw
                quantity = None
            elif isinstance(raw, Mapping):
                quantity = raw.get("quantity")
                if "value" in raw and "dc_value" not in raw:
                    dc_value = raw["value"]
                else:
                    dc_value = raw.get("dc_value")
                requested = SourceSpecification(
                    dc_value=None if dc_value is None else _finite(dc_value, label="source DC value"),
                    ac_magnitude=(
                        None
                        if raw.get("ac_magnitude") is None
                        else _finite(raw["ac_magnitude"], label="source AC magnitude")
                    ),
                    ac_phase_deg=(
                        None
                        if raw.get("ac_phase_deg", raw.get("ac_phase")) is None
                        else _finite(
                            raw.get("ac_phase_deg", raw.get("ac_phase")),
                            label="source AC phase",
                        )
                    ),
                )
            else:
                raise BindingValidationError(
                    f"source override for {element_name!r} must be structured"
                )
            prefix = str(element.name)[:1].casefold()
            if quantity is not None:
                expected = "voltage" if prefix == "v" else "current"
                if str(quantity).casefold() != expected:
                    raise BindingValidationError(
                        f"source quantity {quantity!r} does not match {element_name!r}"
                    )
            merged = SourceSpecification(
                dc_value=current.dc_value if requested.dc_value is None else requested.dc_value,
                ac_magnitude=(
                    current.ac_magnitude
                    if requested.ac_magnitude is None
                    else requested.ac_magnitude
                ),
                ac_phase_deg=(
                    current.ac_phase_deg
                    if requested.ac_phase_deg is None
                    else requested.ac_phase_deg
                ),
            ).validated()
            self._replace_source(str(element_name), merged)
            self._source_specs[str(element_name)] = merged
            self.metadata.source_overrides.append(
                {"element": str(element_name), **asdict(merged)}
            )

    def set_source_dc(self, element_name: str, value: float) -> None:
        self.apply_source_overrides(
            {element_name: SourceSpecification(dc_value=float(value))}
        )

    def set_source_ac_magnitude(self, element_name: str, value: float) -> None:
        self.apply_source_overrides(
            {element_name: SourceSpecification(ac_magnitude=float(value))}
        )

    def set_source_ac_phase(self, element_name: str, value: float) -> None:
        try:
            element = self.circuit[element_name]
        except Exception as error:
            raise BindingValidationError(
                f"source override element {element_name!r} does not exist"
            ) from error
        current = self._source_specs.get(element_name) or self._parse_source(element)
        if current.ac_magnitude is None:
            raise BindingValidationError(
                f"cannot set AC phase for {element_name!r} before AC magnitude"
            )
        self.apply_source_overrides(
            {
                element_name: SourceSpecification(
                    ac_magnitude=current.ac_magnitude,
                    ac_phase_deg=float(value),
                )
            }
        )

    @staticmethod
    def _terminal_index(element: Any, terminal: str) -> int:
        name = str(element.name)
        prefix = name[:1].casefold()
        terminal_name = str(terminal).casefold()
        if terminal_name.isdigit():
            index = int(terminal_name)
            if index < len(element.node_names):
                return index
        maps = {
            "m": {"drain": 0, "gate": 1, "source": 2, "bulk": 3},
            "r": {"p": 0, "plus": 0, "positive": 0, "n": 1, "minus": 1, "negative": 1},
            "c": {"p": 0, "plus": 0, "positive": 0, "n": 1, "minus": 1, "negative": 1},
            "l": {"p": 0, "plus": 0, "positive": 0, "n": 1, "minus": 1, "negative": 1},
            "v": {"p": 0, "plus": 0, "positive": 0, "n": 1, "minus": 1, "negative": 1},
            "i": {"p": 0, "plus": 0, "positive": 0, "n": 1, "minus": 1, "negative": 1},
        }
        try:
            return maps[prefix][terminal_name]
        except KeyError as error:
            raise BindingValidationError(
                f"unsupported terminal {terminal!r} for element {name!r}"
            ) from error

    def _recreate_element(self, element: Any, nodes: Sequence[str]) -> Any:
        full_name = str(element.name)
        prefix = full_name[:1].casefold()
        suffix = full_name[1:]
        if prefix == "m":
            kwargs = {
                key: getattr(element, key)
                for key in ("width", "length", "multiplier")
                if getattr(element, key, None) is not None
            }
            model = element.model
            element.detach()
            return self.circuit.MOSFET(suffix, *nodes, model=model, **kwargs)
        if prefix in {"r", "c", "l"}:
            attribute = {"r": "resistance", "c": "capacitance", "l": "inductance"}[prefix]
            value = getattr(element, attribute)
            element.detach()
            constructor = {"r": self.circuit.R, "c": self.circuit.C, "l": self.circuit.L}[prefix]
            return constructor(suffix, *nodes, value)
        if prefix in {"v", "i"}:
            value = element.dc_value
            element.detach()
            constructor = self.circuit.V if prefix == "v" else self.circuit.I
            return constructor(suffix, *nodes, value)
        raise BindingValidationError(
            f"terminal remapping is not implemented for element {full_name!r}"
        )

    def remap_terminal(
        self,
        element_name: str,
        terminal: str,
        new_node: str,
    ) -> TerminalRemap:
        """Recreate one in-memory element with a named terminal redirected."""

        try:
            element = self.circuit[element_name]
        except Exception as error:
            raise BindingValidationError(
                f"terminal-remap element {element_name!r} does not exist"
            ) from error
        index = self._terminal_index(element, terminal)
        nodes = [str(node) for node in element.node_names]
        original = nodes[index]
        nodes[index] = str(new_node)
        self._recreate_element(element, nodes)
        remap = TerminalRemap(str(element_name), str(terminal), original, str(new_node))
        self.metadata.terminal_remaps.append(asdict(remap))
        return remap

    def remove_element(self, element_name: str) -> None:
        try:
            self.circuit[element_name].detach()
        except Exception as error:
            raise BindingValidationError(
                f"testbench removal element {element_name!r} does not exist"
            ) from error
        self.metadata.removed_elements.append(str(element_name))

    def decorate_feedback_edge(
        self,
        request: FeedbackEdgeRequest,
        *,
        native_operating_point: NativeOperatingPoint | None = None,
        nodeset: Mapping[str, float] | None = None,
    ) -> str:
        """Insert an AC test source into an existing or synthesized edge."""

        for element_name in request.remove_elements:
            self.remove_element(element_name)
        if request.to_terminal is not None:
            try:
                target_element = self.circuit[request.to_terminal.element]
            except Exception as error:
                raise BindingValidationError(
                    f"feedback target element {request.to_terminal.element!r} does not exist"
                ) from error
            terminal_index = self._terminal_index(
                target_element, request.to_terminal.terminal
            )
            original_node = str(target_element.node_names[terminal_index])
            if original_node.casefold() != request.from_node.casefold():
                raise BindingValidationError(
                    "feedback-edge source node does not match the terminal's original node: "
                    f"{request.from_node!r} != {original_node!r}"
                )
            temporary_node = self.allocate_temporary_node(request.temporary_node_base)
            self.remap_terminal(
                request.to_terminal.element,
                request.to_terminal.terminal,
                temporary_node,
            )
            destination = temporary_node
        elif request.to_node is not None:
            destination = request.to_node
            temporary_node = request.to_node
        else:
            raise BindingValidationError(
                "feedback decoration requires to_terminal or to_node"
            )
        source_name = request.test_source_name
        suffix = source_name[1:] if source_name.upper().startswith("V") else source_name
        full_name = f"V{suffix}"
        if full_name in {str(name) for name in self.circuit.element_names}:
            raise BindingValidationError(f"feedback test source {full_name!r} already exists")
        spec = SourceSpecification(
            request.dc_value,
            request.ac_magnitude,
            request.ac_phase_deg,
        ).validated()
        feedback_factor = _finite(
            request.feedback_factor, label="feedback factor"
        )
        if feedback_factor == 0.0:
            raise BindingValidationError("feedback factor cannot be zero")
        self.circuit.V(suffix, request.from_node, destination, spec.to_pyspice_value())
        if native_operating_point is not None:
            self._native_op = native_operating_point
            self.metadata.native_op_used = True
        if nodeset:
            self.apply_nodeset(nodeset)
        self.metadata.feedback_edge = {
            "from_node": request.from_node,
            "to_node": destination,
            "test_source": full_name,
            "dc_value": spec.dc_value,
            "ac_magnitude": spec.ac_magnitude,
            "ac_phase_deg": spec.ac_phase_deg,
            "existing_edge": request.to_terminal is not None,
            "temporary_node": temporary_node,
            "feedback_factor": feedback_factor,
            "unity_feedback_confirmed": bool(request.unity_feedback_confirmed),
        }
        return destination

    def attach_load(self, attachment: LoadAttachment) -> dict[str, str]:
        """Attach an external R/C load without changing optimizer parameters."""

        reference = attachment.reference_node
        if not attachment.output_node or not reference:
            raise BindingValidationError("load nodes must be non-empty")
        result: dict[str, str] = {}
        if attachment.capacitance_f is not None:
            capacitance = _finite(attachment.capacitance_f, label="load capacitance")
            if capacitance <= 0.0:
                raise BindingValidationError("load capacitance must be positive")
            suffix = attachment.name
            element_name = f"C{suffix}"
            index = 1
            names = {str(name).casefold() for name in self.circuit.element_names}
            while element_name.casefold() in names:
                index += 1
                element_name = f"C{suffix}_{index}"
            self.circuit.C(element_name[1:], attachment.output_node, reference, capacitance)
            result["capacitor"] = element_name
        if attachment.resistance_ohm is not None:
            resistance = _finite(attachment.resistance_ohm, label="load resistance")
            if resistance <= 0.0:
                raise BindingValidationError("load resistance must be positive")
            suffix = attachment.name
            element_name = f"R{suffix}"
            index = 1
            names = {str(name).casefold() for name in self.circuit.element_names}
            while element_name.casefold() in names:
                index += 1
                element_name = f"R{suffix}_{index}"
            self.circuit.R(element_name[1:], attachment.output_node, reference, resistance)
            result["resistor"] = element_name
        self.metadata.load = {**asdict(attachment), "elements": result}
        return result

    def apply_nodeset(self, nodeset: Mapping[str, float]) -> None:
        if not isinstance(nodeset, Mapping):
            raise BindingValidationError("nodeset must be a mapping")
        applied: dict[str, float] = {}
        for node, raw_value in sorted(nodeset.items(), key=lambda item: str(item[0]).casefold()):
            node_name = str(node)
            if node_name.casefold() in _GROUND_NAMES:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.$:+-]+", node_name):
                raise BindingValidationError(f"unsafe nodeset node name {node_name!r}")
            value = _finite(raw_value, label=f"nodeset {node_name}")
            self.circuit.raw_spice += f".nodeset V({node_name})={value:.17g}\n"
            applied[node_name] = value
        self.metadata.nodeset_used = bool(applied)
        self.metadata.nodeset.update(applied)

    def finalize(self) -> TestbenchBuild:
        return TestbenchBuild(
            circuit=self.circuit,
            metadata=self.metadata.to_dict(),
            native_operating_point=self._native_op,
        )
