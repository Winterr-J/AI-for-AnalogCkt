"""Convert the topology-only AnalogGenie Cadence netlist dialect to PySpice.

The AnalogGenie ``.cir`` files used here contain only device connectivity, for
example ``M0 (VOUT1 VIN1 VSS VSS) nmos4``.  They do not contain model cards,
device dimensions, source values, or passive values.  The generated Python
therefore exposes every missing value as a required keyword argument instead
of inventing a simulation-ready number.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEVICE_RE = re.compile(
    r"^\s*(?P<name>\S+)\s*\((?P<nodes>[^)]*)\)\s*"
    r"(?P<kind>\S+)(?P<tail>.*)$"
)
COMMENT_PREFIXES = ("*", "#", ";", "//")
SUPPORTED_KINDS = {
    "nmos4": ("mosfet", 4),
    "pmos4": ("mosfet", 4),
    "resistor": ("resistor", 2),
    "capacitor": ("capacitor", 2),
    "inductor": ("inductor", 2),
    "diode": ("diode", 2),
    "npn": ("bjt", 4),
    "pnp": ("bjt", 4),
    # AnalogGenie also uses a few schematic-level blocks as hierarchy
    # instances.  Preserve them as PySpice X elements instead of expanding or
    # guessing their transistor implementations.
    "transmission_gate": ("subcircuit", 5),
    "inverter": ("subcircuit", 4),
    "xor": ("subcircuit", 5),
    "pfd": ("subcircuit", 6),
}
GROUND_NAMES = {"0", "GND", "VSS"}


@dataclass(frozen=True)
class CadenceDevice:
    """One connectivity-only device declaration from an AnalogGenie netlist."""

    name: str
    nodes: tuple[str, ...]
    kind: str


def parse_cadence_netlist(text: str) -> list[CadenceDevice]:
    """Parse the small Cadence dialect used by the AnalogGenie dataset.

    Blank lines and full-line comments are ignored.  Unsupported devices,
    unexpected node counts, and trailing instance parameters fail loudly so a
    partially translated topology cannot be mistaken for a correct one.
    Duplicate source instance labels are made unique with a ``_dupN`` suffix;
    labels do not affect connectivity, but PySpice requires unique names.
    """

    devices: list[CadenceDevice] = []
    seen_names: set[str] = set()
    duplicate_counts: dict[str, int] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(COMMENT_PREFIXES):
            continue
        match = DEVICE_RE.match(raw_line)
        if match is None:
            raise ValueError(f"line {line_number}: unsupported syntax: {raw_line!r}")

        source_name = match.group("name")
        occurrence = duplicate_counts.get(source_name, 0) + 1
        duplicate_counts[source_name] = occurrence
        name = source_name
        if occurrence > 1:
            name = f"{source_name}_dup{occurrence}"
            while name in seen_names:
                occurrence += 1
                duplicate_counts[source_name] = occurrence
                name = f"{source_name}_dup{occurrence}"
        kind = match.group("kind").lower()
        nodes = tuple(match.group("nodes").split())
        trailing = match.group("tail").strip()
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"line {line_number}: unsupported device kind {kind!r}")
        expected_nodes = SUPPORTED_KINDS[kind][1]
        if len(nodes) != expected_nodes:
            raise ValueError(
                f"line {line_number}: {kind} requires {expected_nodes} nodes, "
                f"found {len(nodes)}"
            )
        if trailing:
            raise ValueError(
                f"line {line_number}: instance parameters are not yet supported: "
                f"{trailing!r}"
            )
        seen_names.add(name)
        devices.append(CadenceDevice(name=name, nodes=nodes, kind=kind))

    if not devices:
        raise ValueError("netlist contains no devices")
    return devices


def parse_ports(text: str) -> list[str]:
    """Parse an AnalogGenie ``Port<number>.txt`` file."""

    ports = text.split()
    if len(ports) != len(set(ports)):
        raise ValueError("port list contains duplicate names")
    return ports


def _identifier(value: str) -> str:
    identifier = re.sub(r"\W", "_", value)
    if not identifier or identifier[0].isdigit():
        identifier = f"_{identifier}"
    return identifier


def _element_suffix(name: str, expected_prefix: str) -> str:
    if name.upper().startswith(expected_prefix):
        suffix = name[1:]
        return suffix or name
    return name


def _node_expression(node: str) -> str:
    if node.upper() in GROUND_NAMES:
        return "circuit.gnd"
    return json.dumps(node)


def _source_parameters(ports: Sequence[str]) -> list[str]:
    parameters: list[str] = []
    for port in ports:
        upper = port.upper()
        if upper in GROUND_NAMES or upper.startswith("VOUT"):
            continue
        parameters.append(_identifier(port))
    return parameters


def _missing_parameters(
    devices: Sequence[CadenceDevice], ports: Sequence[str]
) -> list[str]:
    parameters = _source_parameters(ports)
    kinds = {device.kind for device in devices}
    if "nmos4" in kinds:
        parameters.append("NMOS_MODEL")
    if "pmos4" in kinds:
        parameters.append("PMOS_MODEL")
    if "npn" in kinds:
        parameters.append("NPN_MODEL")
    if "pnp" in kinds:
        parameters.append("PNP_MODEL")
    if "diode" in kinds:
        parameters.append("DIODE_MODEL")
    for kind in ("transmission_gate", "inverter", "xor", "pfd"):
        if kind in kinds:
            parameters.append(f"{kind.upper()}_SUBCIRCUIT")
    for device in devices:
        device_id = _identifier(device.name)
        if device.kind == "resistor":
            parameters.append(device_id)
        elif device.kind == "capacitor":
            parameters.append(device_id)
        elif device.kind == "inductor":
            parameters.append(device_id)
        elif device.kind in {"nmos4", "pmos4"}:
            parameters.extend((f"W_{device_id}", f"L_{device_id}"))
    return list(dict.fromkeys(parameters))


def _render_sources(ports: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for port in ports:
        upper = port.upper()
        parameter = _identifier(port)
        node = json.dumps(port)
        if upper in GROUND_NAMES or upper.startswith("VOUT"):
            continue
        if upper.startswith("IB"):
            lines.append(
                f"    circuit.I({json.dumps(port)}, circuit.gnd, {node}, {parameter})"
            )
        else:
            lines.append(
                f"    circuit.V({json.dumps(port)}, {node}, circuit.gnd, {parameter})"
            )
    return lines


def _render_device(device: CadenceDevice) -> str:
    nodes = [_node_expression(node) for node in device.nodes]
    name = _identifier(device.name)
    if device.kind in {"nmos4", "pmos4"}:
        suffix = _element_suffix(device.name, "M")
        model = "NMOS_MODEL" if device.kind == "nmos4" else "PMOS_MODEL"
        return (
            f"    circuit.MOSFET({json.dumps(suffix)}, {', '.join(nodes)}, "
            f"model={model}, width=W_{name}, length=L_{name})"
        )
    if device.kind == "resistor":
        suffix = _element_suffix(device.name, "R")
        return f"    circuit.R({json.dumps(suffix)}, {', '.join(nodes)}, {name})"
    if device.kind == "capacitor":
        suffix = _element_suffix(device.name, "C")
        return f"    circuit.C({json.dumps(suffix)}, {', '.join(nodes)}, {name})"
    if device.kind == "inductor":
        suffix = _element_suffix(device.name, "L")
        return f"    circuit.L({json.dumps(suffix)}, {', '.join(nodes)}, {name})"
    if device.kind == "diode":
        suffix = _element_suffix(device.name, "D")
        return (
            f"    circuit.D({json.dumps(suffix)}, {', '.join(nodes)}, "
            "model=DIODE_MODEL)"
        )
    if device.kind in {"npn", "pnp"}:
        # PySpice 1.5 cannot pass its optional BJT substrate pin through the
        # public BJT helper.  Keep all four Cadence terminals exactly by adding
        # the corresponding SPICE instance through Circuit.raw_spice.
        suffix = _element_suffix(device.name, "Q")
        raw_nodes = ["0" if node.upper() in GROUND_NAMES else node for node in device.nodes]
        model = "NPN_MODEL" if device.kind == "npn" else "PNP_MODEL"
        prefix = json.dumps(f"Q{suffix} {' '.join(raw_nodes)} ")
        return f'    circuit.raw_spice += {prefix} + str({model}) + "\\n"'
    if device.kind in {"transmission_gate", "inverter", "xor", "pfd"}:
        subcircuit = f"{device.kind.upper()}_SUBCIRCUIT"
        return (
            f"    circuit.X({json.dumps(device.name)}, {subcircuit}, "
            f"{', '.join(nodes)})"
        )
    raise AssertionError(f"unhandled device kind: {device.kind}")


def render_pyspice(
    devices: Sequence[CadenceDevice],
    ports: Sequence[str],
    *,
    title: str,
    source_name: str,
) -> str:
    """Render a self-contained PySpice circuit-construction module."""

    parameters = _missing_parameters(devices, ports)
    parameter_lines = [f"    {name}: Any," for name in parameters]
    source_lines = _render_sources(ports)
    device_lines = [_render_device(device) for device in devices]

    lines = [
        '"""Generated from ' + source_name + " by cadence_to_pyspice.py.",
        "",
        "All values absent from the source netlist remain required variables.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from PySpice.Spice.Netlist import Circuit",
        "",
        "",
        "def build_circuit(",
        "    *,",
        *parameter_lines,
        ") -> Circuit:",
        f"    circuit = Circuit({json.dumps(title)})",
        "",
        "    # VSS is treated as the PySpice ground node. Other source values",
        "    # are caller-supplied variables because the dataset has no values.",
        *source_lines,
        "",
        "    # Model names, W/L dimensions, and passive values are variables.",
        *device_lines,
        "    return circuit",
        "",
    ]
    return "\n".join(lines)


def convert_file(
    cadence_path: Path,
    port_path: Path,
    output_path: Path,
    *,
    title: str | None = None,
) -> Path:
    """Convert one ``.cir`` plus its port file and write a Python module."""

    devices = parse_cadence_netlist(cadence_path.read_text(encoding="utf-8"))
    ports = parse_ports(port_path.read_text(encoding="utf-8"))
    rendered = render_pyspice(
        devices,
        ports,
        title=title or f"AnalogGenie topology {cadence_path.stem}",
        source_name=cadence_path.name,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an AnalogGenie Cadence topology netlist to PySpice code."
    )
    parser.add_argument("cadence_netlist", type=Path)
    parser.add_argument("port_file", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    convert_file(
        args.cadence_netlist,
        args.port_file,
        args.output,
        title=args.title,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
