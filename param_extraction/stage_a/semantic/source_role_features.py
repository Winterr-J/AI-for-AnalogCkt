"""Gold-independent structural features for source/interface role templates."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Mapping

from ..ir import CircuitIR, PhysicalTargetIR


_GROUND_NODES = frozenset({"0", "gnd", "GND"})
_PASSIVE_TYPES = frozenset({"resistor", "capacitor", "inductor"})


@dataclass(frozen=True)
class SourceRoleFeatures:
    source_type: str
    grounded: bool
    binding_status: str
    source_node: str | None
    terminal_role_counts: Mapping[str, int]
    fanout_count: int
    active_control_terminal_count: int
    active_conduction_terminal_count: int
    passive_neighbor_count: int
    subcircuit_neighbor_count: int
    passive_relay_control_count: int
    passive_relay_conduction_count: int
    drives_only_control_terminals: bool
    drives_multiple_control_terminals: bool
    connects_internal_active_node: bool
    connects_passive_network: bool
    interface_evidence: tuple[str, ...]
    circuit_type: str
    name_hint: str

    @property
    def has_active_path(self) -> bool:
        return bool(
            self.connects_internal_active_node
            or self.passive_relay_control_count
            or self.passive_relay_conduction_count
        )

    @property
    def has_interface_path(self) -> bool:
        return bool(
            self.fanout_count
            or self.passive_relay_control_count
            or self.passive_relay_conduction_count
        )

    def evidence(self) -> tuple[str, ...]:
        roles = ",".join(
            f"{key}:{value}" for key, value in sorted(self.terminal_role_counts.items())
            if value
        ) or "none"
        return (
            f"source_type={self.source_type}",
            f"grounded={str(self.grounded).lower()}",
            f"binding={self.binding_status}",
            f"source_node={self.source_node}",
            f"terminal_roles={roles}",
            f"fanout={self.fanout_count}",
            f"active_control={self.active_control_terminal_count}",
            f"active_conduction={self.active_conduction_terminal_count}",
            f"passive_neighbors={self.passive_neighbor_count}",
            f"subcircuit_neighbors={self.subcircuit_neighbor_count}",
            f"passive_relay_control={self.passive_relay_control_count}",
            f"passive_relay_conduction={self.passive_relay_conduction_count}",
            f"circuit_type={self.circuit_type}",
            f"name_hint={self.name_hint}",
            *self.interface_evidence,
        )


def extract_source_role_features(
    circuit: CircuitIR, target: PhysicalTargetIR
) -> SourceRoleFeatures | None:
    if target.source_kind != "source_interface":
        return None
    matches = [
        source
        for source in circuit.sources
        if source.source_id == target.device_id
        and source.physical_target == target.parameter_id
    ]
    if len(matches) != 1:
        return None
    source = matches[0]
    p_node = source.terminals.get("p", "")
    n_node = source.terminals.get("n", "")
    grounded_p = p_node in _GROUND_NODES
    grounded_n = n_node in _GROUND_NODES
    grounded = grounded_p ^ grounded_n
    source_node = n_node if grounded_p else p_node if grounded_n else p_node or n_node or None

    direct_roles, passive_devices, subcircuit_count = _roles_at_node(
        circuit, source_node
    )
    relay_roles: Counter[str] = Counter()
    for passive in passive_devices:
        other_nodes = {
            node for node in passive.terminals.values()
            if node != source_node and node not in _GROUND_NODES
        }
        for node in other_nodes:
            node_roles, _, _ = _roles_at_node(
                circuit, node, excluded_device_id=passive.device_id
            )
            relay_roles.update(node_roles)

    control = direct_roles["MOS_GATE"] + direct_roles["BJT_BASE"]
    conduction = sum(
        direct_roles[key]
        for key in ("MOS_SOURCE", "MOS_DRAIN", "BJT_EMITTER", "BJT_COLLECTOR")
    )
    relay_control = relay_roles["MOS_GATE"] + relay_roles["BJT_BASE"]
    relay_conduction = sum(
        relay_roles[key]
        for key in ("MOS_SOURCE", "MOS_DRAIN", "BJT_EMITTER", "BJT_COLLECTOR")
    )
    passive_count = len(passive_devices)
    fanout = sum(direct_roles.values())
    interface_evidence = (
        f"source_interface={source.source_id}",
        f"source_terminals=p:{p_node},n:{n_node}",
        "source_created_interface_node=true" if source_node else "source_created_interface_node=false",
    )
    binding = target.binding.binding_status if target.binding is not None else "UNCERTAIN"
    return SourceRoleFeatures(
        source_type="voltage" if source.source_type == "voltage_source" else "current",
        grounded=grounded,
        binding_status=binding,
        source_node=source_node,
        terminal_role_counts=dict(direct_roles),
        fanout_count=fanout,
        active_control_terminal_count=control,
        active_conduction_terminal_count=conduction,
        passive_neighbor_count=passive_count,
        subcircuit_neighbor_count=subcircuit_count,
        passive_relay_control_count=relay_control,
        passive_relay_conduction_count=relay_conduction,
        drives_only_control_terminals=(
            control > 0 and conduction == 0 and passive_count == 0 and subcircuit_count == 0
        ),
        drives_multiple_control_terminals=control > 1,
        connects_internal_active_node=(control + conduction) > 0,
        connects_passive_network=passive_count > 0,
        interface_evidence=interface_evidence,
        circuit_type=circuit.circuit_type,
        name_hint=_name_hint(target.parameter_name or target.parameter_id),
    )


def _roles_at_node(
    circuit: CircuitIR,
    node: str | None,
    *,
    excluded_device_id: str | None = None,
) -> tuple[Counter[str], list[object], int]:
    roles: Counter[str] = Counter()
    passives: list[object] = []
    if not node:
        return roles, passives, 0
    for device in circuit.devices:
        if device.device_id == excluded_device_id:
            continue
        connected = [name for name, value in device.terminals.items() if value == node]
        if not connected:
            continue
        device_type = device.device_type.lower()
        if device_type in {"mos", "nmos", "pmos"}:
            for terminal in connected:
                roles[_terminal_role("MOS", terminal)] += 1
        elif device_type in {"bjt", "bjt_npn", "bjt_pnp"}:
            for terminal in connected:
                roles[_terminal_role("BJT", terminal)] += 1
        elif device_type in _PASSIVE_TYPES:
            roles["PASSIVE"] += 1
            passives.append(device)
        else:
            roles["OTHER_DEVICE"] += 1
    subcircuit_count = sum(node in subcircuit.nodes for subcircuit in circuit.subcircuits)
    if subcircuit_count:
        roles["SUBCIRCUIT_PIN"] += subcircuit_count
    return roles, passives, subcircuit_count


def _terminal_role(prefix: str, terminal: str) -> str:
    normalized = terminal.upper()
    aliases = {
        "GATE": "GATE",
        "SOURCE": "SOURCE",
        "DRAIN": "DRAIN",
        "BULK": "BULK",
        "BASE": "BASE",
        "EMITTER": "EMITTER",
        "COLLECTOR": "COLLECTOR",
    }
    return f"{prefix}_{aliases.get(normalized, normalized)}"


def _name_hint(name: str) -> str:
    token = re.sub(r"[^A-Z0-9]", "", name.upper())
    if token in {"VDD", "VSS"}:
        return "SUPPLY"
    if token.startswith("VCM"):
        return "COMMON_MODE"
    if any(part in token for part in ("CLK", "TRACK", "HOLD", "LATCH", "PHASE")):
        return "CLOCK"
    if token.startswith(("VBIAS", "IBIAS", "VB", "IB")):
        return "BIAS"
    if token.startswith(("VREF", "IREF")):
        return "REFERENCE"
    if token.startswith(("VCONT", "VCTRL", "ICONT", "ICTRL")):
        return "CONTROL"
    if token.startswith(("VIN", "IIN")):
        return "INPUT"
    if token.startswith(("VRF", "IRF", "RF")):
        return "RF"
    if token.startswith(("VLO", "ILO", "LO")):
        return "LO"
    if token.startswith(("VOUT", "IOUT")):
        return "OUTPUT"
    return "UNKNOWN"
