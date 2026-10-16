"""Gold-independent local topology features for passive electrical values."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..ir import CircuitIR, PhysicalTargetIR
from .semantic_result import SemanticPrediction
from .source_role_features import extract_source_role_features


_GROUND = frozenset({"0", "gnd", "GND"})
_PASSIVE_TYPES = frozenset({"resistor", "capacitor", "inductor"})
_PASSIVE_KINDS = {
    "resistance": "R",
    "capacitance": "C",
    "inductance": "L",
}
_CONTROL_ROLES = frozenset({"MOS_GATE", "BJT_BASE"})
_CONDUCTION_ROLES = frozenset(
    {"MOS_SOURCE", "MOS_DRAIN", "BJT_EMITTER", "BJT_COLLECTOR"}
)


@dataclass(frozen=True)
class PassiveEndpointFeatures:
    node: str
    grounded: bool
    active_terminal_counts: Mapping[str, int]
    passive_neighbor_count: int
    passive_neighbor_kinds: tuple[str, ...]
    subcircuit_pin_count: int
    source_roles: tuple[str, ...]
    two_hop_active_terminal_counts: Mapping[str, int]
    two_hop_source_roles: tuple[str, ...]

    @property
    def active_control_count(self) -> int:
        return sum(self.active_terminal_counts.get(role, 0) for role in _CONTROL_ROLES)

    @property
    def active_conduction_count(self) -> int:
        return sum(self.active_terminal_counts.get(role, 0) for role in _CONDUCTION_ROLES)

    @property
    def two_hop_active_count(self) -> int:
        return sum(self.two_hop_active_terminal_counts.values())

    @property
    def internal_relation(self) -> bool:
        return bool(
            not self.grounded
            and (
                self.active_control_count
                or self.active_conduction_count
                or self.passive_neighbor_count
                or self.subcircuit_pin_count
                or self.two_hop_active_count
            )
        )


@dataclass(frozen=True)
class PassiveRoleFeatures:
    passive_kind: str
    binding_status: str
    endpoint_a: PassiveEndpointFeatures
    endpoint_b: PassiveEndpointFeatures
    ground_relation: str
    supply_relation: bool
    source_interface_relation: bool
    output_interface_relation: bool
    internal_node_relation: bool
    subcircuit_relation: bool
    circuit_type: str
    motifs: tuple[str, ...]
    shared_ground_only: bool
    shared_non_ground_node_count: int
    has_shared_non_ground_passive_neighbor: bool
    same_node_pair_passive_count: int
    same_node_pair_passive_kinds: tuple[str, ...]
    is_parallel_passive_member: bool
    parallel_passive_count: int
    parallel_passive_kinds: tuple[str, ...]
    is_series_chain_member: bool
    series_chain_neighbor_count: int
    series_chain_passive_kinds: tuple[str, ...]
    passive_loop_membership: str
    rlc_proximity: str
    has_direct_inductor_neighbor: bool
    shares_non_ground_node_with_inductor: bool
    shares_node_pair_with_inductor: bool
    source_relation: str
    source_bridge_roles: tuple[str, ...]
    is_source_to_source: bool
    source_shunt_terminal_only: bool
    source_shunt_has_control_continuation: bool
    source_shunt_has_conduction_continuation: bool
    source_shunt_has_passive_continuation: bool
    endpoint_a_continuation: str
    endpoint_b_continuation: str
    terminal_branch_status: str

    @property
    def all_source_roles(self) -> frozenset[str]:
        return frozenset(
            (*self.endpoint_a.source_roles, *self.endpoint_b.source_roles)
        )

    @property
    def endpoint_a_source_roles(self) -> tuple[str, ...]:
        return self.endpoint_a.source_roles

    @property
    def endpoint_b_source_roles(self) -> tuple[str, ...]:
        return self.endpoint_b.source_roles

    def evidence(self) -> tuple[str, ...]:
        return (
            f"passive_kind={self.passive_kind}",
            f"binding={self.binding_status}",
            f"endpoints={self.endpoint_a.node},{self.endpoint_b.node}",
            f"ground_relation={self.ground_relation}",
            f"supply_relation={str(self.supply_relation).lower()}",
            "endpoint_a=" + _endpoint_summary(self.endpoint_a),
            "endpoint_b=" + _endpoint_summary(self.endpoint_b),
            f"source_roles={','.join(sorted(self.all_source_roles)) or 'none'}",
            f"subcircuit_relation={str(self.subcircuit_relation).lower()}",
            f"circuit_type={self.circuit_type}",
            f"motifs={','.join(self.motifs) or 'none'}",
        )


def extract_passive_role_features(
    circuit: CircuitIR,
    target: PhysicalTargetIR,
    runtime_predictions: Sequence[SemanticPrediction],
) -> PassiveRoleFeatures | None:
    passive_kind = _PASSIVE_KINDS.get(target.parameter_kind)
    if target.source_kind != "device_instance" or passive_kind is None:
        return None
    matches = [
        device
        for device in circuit.devices
        if device.device_id == target.device_id
        and device.device_type.lower() in _PASSIVE_TYPES
    ]
    if len(matches) != 1:
        return None
    device = matches[0]
    endpoint_nodes = tuple(device.terminals.values())
    if len(endpoint_nodes) != 2:
        return None

    source_roles = _runtime_source_roles(circuit, runtime_predictions)
    endpoint_a = _endpoint(circuit, endpoint_nodes[0], device.device_id, source_roles)
    endpoint_b = _endpoint(circuit, endpoint_nodes[1], device.device_id, source_roles)
    topology = _passive_topology_relations(
        circuit,
        device.device_id,
        endpoint_nodes,
        endpoint_a,
        endpoint_b,
    )
    grounded = endpoint_a.grounded or endpoint_b.grounded
    ground_relation = "SHUNT_TO_GROUND" if grounded else "BOTH_NON_GROUND"
    all_roles = set((*endpoint_a.source_roles, *endpoint_b.source_roles))
    motifs = _motifs(passive_kind, endpoint_a, endpoint_b, circuit.circuit_type)
    binding = target.binding.binding_status if target.binding is not None else "UNCERTAIN"
    return PassiveRoleFeatures(
        passive_kind=passive_kind,
        binding_status=binding,
        endpoint_a=endpoint_a,
        endpoint_b=endpoint_b,
        ground_relation=ground_relation,
        supply_relation="SUPPLY" in all_roles,
        source_interface_relation=bool(all_roles),
        output_interface_relation="OUTPUT" in all_roles,
        internal_node_relation=endpoint_a.internal_relation or endpoint_b.internal_relation,
        subcircuit_relation=bool(
            endpoint_a.subcircuit_pin_count or endpoint_b.subcircuit_pin_count
        ),
        circuit_type=circuit.circuit_type,
        motifs=tuple(sorted(motifs)),
        shared_ground_only=topology["shared_ground_only"],
        shared_non_ground_node_count=topology["shared_non_ground_node_count"],
        has_shared_non_ground_passive_neighbor=topology[
            "has_shared_non_ground_passive_neighbor"
        ],
        same_node_pair_passive_count=topology["same_node_pair_passive_count"],
        same_node_pair_passive_kinds=topology["same_node_pair_passive_kinds"],
        is_parallel_passive_member=topology["same_node_pair_passive_count"] > 0,
        parallel_passive_count=topology["same_node_pair_passive_count"],
        parallel_passive_kinds=topology["same_node_pair_passive_kinds"],
        is_series_chain_member=topology["series_chain_neighbor_count"] > 0,
        series_chain_neighbor_count=topology["series_chain_neighbor_count"],
        series_chain_passive_kinds=topology["series_chain_passive_kinds"],
        passive_loop_membership=topology["passive_loop_membership"],
        rlc_proximity=topology["rlc_proximity"],
        has_direct_inductor_neighbor=topology["has_direct_inductor_neighbor"],
        shares_non_ground_node_with_inductor=topology[
            "shares_non_ground_node_with_inductor"
        ],
        shares_node_pair_with_inductor=topology["shares_node_pair_with_inductor"],
        source_relation=_source_relation(endpoint_a, endpoint_b),
        source_bridge_roles=tuple(sorted(set(endpoint_a.source_roles) | set(endpoint_b.source_roles)))
        if endpoint_a.source_roles and endpoint_b.source_roles
        else (),
        is_source_to_source=bool(endpoint_a.source_roles and endpoint_b.source_roles),
        source_shunt_terminal_only=_source_shunt_flag(
            endpoint_a, endpoint_b, "terminal"
        ),
        source_shunt_has_control_continuation=_source_shunt_flag(
            endpoint_a, endpoint_b, "control"
        ),
        source_shunt_has_conduction_continuation=_source_shunt_flag(
            endpoint_a, endpoint_b, "conduction"
        ),
        source_shunt_has_passive_continuation=_source_shunt_flag(
            endpoint_a, endpoint_b, "passive"
        ),
        endpoint_a_continuation=_endpoint_continuation(endpoint_a),
        endpoint_b_continuation=_endpoint_continuation(endpoint_b),
        terminal_branch_status=_terminal_branch_status(endpoint_a, endpoint_b),
    )


def _passive_topology_relations(
    circuit: CircuitIR,
    target_device_id: str,
    endpoint_nodes: tuple[str, ...],
    endpoint_a: PassiveEndpointFeatures,
    endpoint_b: PassiveEndpointFeatures,
) -> dict[str, object]:
    target_pair = frozenset(endpoint_nodes)
    shared_ground_only = False
    shared_non_ground_nodes: set[str] = set()
    same_pair_kinds: list[str] = []
    series_kinds: list[str] = []
    has_direct_inductor = False
    shares_pair_with_inductor = False

    passive_devices = [
        item
        for item in circuit.devices
        if item.device_type.lower() in _PASSIVE_TYPES
    ]
    endpoint_by_node = {
        endpoint_a.node: endpoint_a,
        endpoint_b.node: endpoint_b,
    }
    for neighbor in passive_devices:
        if neighbor.device_id == target_device_id:
            continue
        neighbor_nodes = tuple(neighbor.terminals.values())
        shared_nodes = set(endpoint_nodes) & set(neighbor_nodes)
        if not shared_nodes:
            continue
        shared_non_ground = {node for node in shared_nodes if node not in _GROUND}
        if shared_nodes and not shared_non_ground:
            shared_ground_only = True
        shared_non_ground_nodes.update(shared_non_ground)
        same_pair = frozenset(neighbor_nodes) == target_pair
        if same_pair:
            same_pair_kinds.append(neighbor.device_type.lower())
            if neighbor.device_type.lower() == "inductor":
                shares_pair_with_inductor = True
        if shared_non_ground and neighbor.device_type.lower() == "inductor":
            has_direct_inductor = True
        for node in shared_non_ground:
            if _is_conservative_series_junction(
                circuit,
                node,
                target_device_id,
                neighbor.device_id,
                endpoint_by_node[node],
            ):
                series_kinds.append(neighbor.device_type.lower())

    same_pair_kinds_tuple = tuple(sorted(same_pair_kinds))
    series_kinds_tuple = tuple(sorted(series_kinds))
    if same_pair_kinds_tuple:
        loop_membership = "DIRECT_PARALLEL"
    elif series_kinds_tuple:
        loop_membership = "DIRECT_SERIES"
    elif shared_non_ground_nodes:
        loop_membership = "UNCERTAIN"
    else:
        loop_membership = "NONE"

    if shared_non_ground_nodes:
        proximity = "DIRECT_NODE"
    elif shared_ground_only:
        proximity = "GROUND_ONLY"
    elif _has_two_hop_passive_proximity(circuit, target_device_id, endpoint_nodes):
        proximity = "TWO_HOP"
    else:
        proximity = "NONE"

    return {
        "shared_ground_only": shared_ground_only,
        "shared_non_ground_node_count": len(shared_non_ground_nodes),
        "has_shared_non_ground_passive_neighbor": bool(shared_non_ground_nodes),
        "same_node_pair_passive_count": len(same_pair_kinds_tuple),
        "same_node_pair_passive_kinds": same_pair_kinds_tuple,
        "series_chain_neighbor_count": len(series_kinds_tuple),
        "series_chain_passive_kinds": series_kinds_tuple,
        "passive_loop_membership": loop_membership,
        "rlc_proximity": proximity,
        "has_direct_inductor_neighbor": has_direct_inductor,
        "shares_non_ground_node_with_inductor": has_direct_inductor,
        "shares_node_pair_with_inductor": shares_pair_with_inductor,
    }


def _is_conservative_series_junction(
    circuit: CircuitIR,
    node: str,
    target_device_id: str,
    neighbor_device_id: str,
    endpoint: PassiveEndpointFeatures,
) -> bool:
    if node in _GROUND or endpoint.source_roles or endpoint.subcircuit_pin_count:
        return False
    if endpoint.active_control_count or endpoint.active_conduction_count:
        return False
    connected_passives = {
        item.device_id
        for item in circuit.devices
        if item.device_type.lower() in _PASSIVE_TYPES
        and node in item.terminals.values()
    }
    if connected_passives != {target_device_id, neighbor_device_id}:
        return False
    if any(node in source.terminals.values() for source in circuit.sources):
        return False
    return not any(node in subcircuit.nodes for subcircuit in circuit.subcircuits)


def _has_two_hop_passive_proximity(
    circuit: CircuitIR,
    target_device_id: str,
    endpoint_nodes: tuple[str, ...],
) -> bool:
    target_nodes = {node for node in endpoint_nodes if node not in _GROUND}
    if not target_nodes:
        return False
    for device in circuit.devices:
        if device.device_id == target_device_id:
            continue
        if device.device_type.lower() in _PASSIVE_TYPES:
            continue
        device_nodes = set(device.terminals.values())
        if not target_nodes & device_nodes:
            continue
        other_nodes = {node for node in device_nodes - target_nodes if node not in _GROUND}
        if any(
            passive.device_id != target_device_id
            and passive.device_type.lower() in _PASSIVE_TYPES
            and bool(other_nodes & set(passive.terminals.values()))
            for passive in circuit.devices
        ):
            return True
    return False


def _source_relation(
    a: PassiveEndpointFeatures,
    b: PassiveEndpointFeatures,
) -> str:
    roles_a, roles_b = set(a.source_roles), set(b.source_roles)
    if roles_a and roles_b:
        if roles_a == roles_b:
            return "SAME_ROLE_SOURCE_TO_SOURCE"
        return "DIFFERENT_ROLE_SOURCE_TO_SOURCE"
    if (roles_a and b.grounded) or (roles_b and a.grounded):
        return "SOURCE_SHUNT_TO_GROUND"
    if roles_a or roles_b:
        return "SOURCE_TO_INTERNAL"
    return "NO_SOURCE"


def _source_shunt_endpoint(
    a: PassiveEndpointFeatures,
    b: PassiveEndpointFeatures,
) -> PassiveEndpointFeatures | None:
    if a.source_roles and b.grounded:
        return a
    if b.source_roles and a.grounded:
        return b
    return None


def _source_shunt_flag(
    a: PassiveEndpointFeatures,
    b: PassiveEndpointFeatures,
    flag: str,
) -> bool:
    endpoint = _source_shunt_endpoint(a, b)
    if endpoint is None:
        return False
    if flag == "control":
        return endpoint.active_control_count > 0
    if flag == "conduction":
        return endpoint.active_conduction_count > 0
    if flag == "passive":
        return endpoint.passive_neighbor_count > 0
    if flag == "terminal":
        return not (
            endpoint.active_control_count
            or endpoint.active_conduction_count
            or endpoint.passive_neighbor_count
            or endpoint.subcircuit_pin_count
        )
    raise ValueError(f"Unsupported source-shunt flag: {flag}")


def _endpoint_continuation(endpoint: PassiveEndpointFeatures) -> str:
    if endpoint.grounded or endpoint.subcircuit_pin_count:
        return "UNKNOWN"
    has_active = bool(endpoint.active_control_count or endpoint.active_conduction_count)
    has_passive = endpoint.passive_neighbor_count > 0
    if has_active and has_passive:
        return "HAS_ACTIVE_AND_PASSIVE_CONTINUATION"
    if has_active:
        return "HAS_ACTIVE_CONTINUATION"
    if has_passive:
        return "HAS_PASSIVE_CONTINUATION"
    return "TERMINAL_ONLY"


def _terminal_branch_status(
    a: PassiveEndpointFeatures,
    b: PassiveEndpointFeatures,
) -> str:
    endpoints = [item for item in (a, b) if not item.grounded]
    if not endpoints or any(item.subcircuit_pin_count for item in endpoints):
        return "UNKNOWN"
    has_active = any(
        item.active_control_count or item.active_conduction_count for item in endpoints
    )
    has_passive = any(item.passive_neighbor_count for item in endpoints)
    if has_active and has_passive:
        return "HAS_ACTIVE_AND_PASSIVE_CONTINUATION"
    if has_active:
        return "HAS_ACTIVE_CONTINUATION"
    if has_passive:
        return "HAS_PASSIVE_CONTINUATION"
    return "TERMINAL_ONLY"


def _runtime_source_roles(
    circuit: CircuitIR,
    predictions: Sequence[SemanticPrediction],
) -> Mapping[str, tuple[str, ...]]:
    prediction_by_id = {
        item.parameter_id: item
        for item in predictions
        if item.source_kind == "source_interface"
    }
    roles: dict[str, set[str]] = defaultdict(set)
    for source in circuit.sources:
        prediction = prediction_by_id.get(source.physical_target)
        if prediction is None or prediction.semantic_status != "RESOLVED":
            continue
        target = next(
            (
                item for item in circuit.physical_targets
                if item.parameter_id == source.physical_target
            ),
            None,
        )
        if target is None:
            continue
        features = extract_source_role_features(circuit, target)
        if features is None or features.source_node is None:
            continue
        role = _source_role_from_prediction(prediction, features.name_hint)
        if role is not None:
            roles[features.source_node].add(role)
    return {node: tuple(sorted(values)) for node, values in roles.items()}


def _source_role_from_prediction(
    prediction: SemanticPrediction, name_hint: str
) -> str | None:
    if prediction.rule_id == "T1_SUPPLY_RAIL":
        return "SUPPLY"
    rule_roles = {
        "T2_INTERNAL_BIAS_CURRENT": "BIAS",
        "T2_INTERNAL_BIAS_VOLTAGE": "BIAS",
        "T2_INPUT_INTERFACE": "INPUT",
        "T2_COMMON_MODE_INPUT": "COMMON_MODE",
        "T2_FUNCTIONAL_REFERENCE": "REFERENCE",
        "T2_FUNCTIONAL_CONTROL": "CONTROL",
        "T2_CLOCK_SWITCH_CONTROL": "CLOCK",
        "T2_RF_INTERFACE": "RF",
        "T2_LO_INTERFACE": "LO",
        "T2_OUTPUT_COMPLIANCE": "OUTPUT",
    }
    return rule_roles.get(prediction.rule_id) or (
        name_hint if prediction.predicted_semantic_category == "operating_condition" else None
    )


def _endpoint(
    circuit: CircuitIR,
    node: str,
    target_device_id: str,
    source_roles: Mapping[str, tuple[str, ...]],
) -> PassiveEndpointFeatures:
    roles: Counter[str] = Counter()
    passive_neighbors = []
    subcircuit_count = 0
    for device in circuit.devices:
        if device.device_id == target_device_id:
            continue
        connected = [terminal for terminal, value in device.terminals.items() if value == node]
        if not connected:
            continue
        family = device.device_type.lower()
        if family in {"mos", "nmos", "pmos"}:
            for terminal in connected:
                roles[f"MOS_{terminal.upper()}"] += 1
        elif family in {"bjt", "bjt_npn", "bjt_pnp"}:
            for terminal in connected:
                roles[f"BJT_{terminal.upper()}"] += 1
        elif family in _PASSIVE_TYPES:
            passive_neighbors.append(device)
    subcircuit_count = sum(node in subcircuit.nodes for subcircuit in circuit.subcircuits)

    two_hop_roles: Counter[str] = Counter()
    two_hop_source_roles: set[str] = set()
    for passive in passive_neighbors:
        other_nodes = {
            value for value in passive.terminals.values()
            if value != node and value not in _GROUND
        }
        for other in other_nodes:
            two_hop_source_roles.update(source_roles.get(other, ()))
            for device in circuit.devices:
                if device.device_id in {target_device_id, passive.device_id}:
                    continue
                connected = [
                    terminal for terminal, value in device.terminals.items() if value == other
                ]
                family = device.device_type.lower()
                if family in {"mos", "nmos", "pmos"}:
                    for terminal in connected:
                        two_hop_roles[f"MOS_{terminal.upper()}"] += 1
                elif family in {"bjt", "bjt_npn", "bjt_pnp"}:
                    for terminal in connected:
                        two_hop_roles[f"BJT_{terminal.upper()}"] += 1

    return PassiveEndpointFeatures(
        node=node,
        grounded=node in _GROUND,
        active_terminal_counts=dict(roles),
        passive_neighbor_count=len(passive_neighbors),
        passive_neighbor_kinds=tuple(
            sorted(device.device_type.lower() for device in passive_neighbors)
        ),
        subcircuit_pin_count=subcircuit_count,
        source_roles=source_roles.get(node, ()),
        two_hop_active_terminal_counts=dict(two_hop_roles),
        two_hop_source_roles=tuple(sorted(two_hop_source_roles)),
    )


def _motifs(
    passive_kind: str,
    a: PassiveEndpointFeatures,
    b: PassiveEndpointFeatures,
    circuit_type: str,
) -> set[str]:
    motifs: set[str] = set()
    roles_a, roles_b = set(a.source_roles), set(b.source_roles)
    all_roles = roles_a | roles_b
    nonground = [item for item in (a, b) if not item.grounded]
    if len(nonground) == 1 and nonground[0].internal_relation:
        motifs.add("INTERNAL_TO_GROUND")
    if len(nonground) == 2 and all(item.internal_relation for item in nonground):
        motifs.add("BETWEEN_INTERNAL_NODES")
    if all_roles and not (a.grounded or b.grounded):
        motifs.add("SOURCE_SERIES")
    if all_roles and (a.grounded or b.grounded):
        motifs.add("SOURCE_SHUNT")
    if "OUTPUT" in all_roles:
        motifs.add("OUTPUT_SHUNT" if (a.grounded or b.grounded) else "OUTPUT_SERIES")
    if "SUPPLY" in all_roles and any(item.internal_relation for item in nonground):
        motifs.add("SUPPLY_TO_INTERNAL")
    if all_roles & {"BIAS", "REFERENCE"} and any(
        item.internal_relation for item in nonground
    ):
        motifs.add("BIAS_TO_INTERNAL")
        motifs.add("REFERENCE_BIAS_NETWORK")
    if passive_kind == "C" and all_roles & {"INPUT", "COMMON_MODE", "RF", "LO"}:
        if any(item.active_control_count or item.two_hop_active_count for item in nonground):
            motifs.add("COUPLING_PATH")
    if len(nonground) == 2:
        control = any(item.active_control_count for item in nonground)
        conduction = any(item.active_conduction_count for item in nonground)
        if control and conduction:
            motifs.add("FEEDBACK_LIKE_PATH")
    if passive_kind == "R" and any(
        item.active_terminal_counts.get("MOS_SOURCE", 0)
        or item.active_terminal_counts.get("BJT_EMITTER", 0)
        for item in nonground
    ):
        motifs.add("DEGENERATION_LIKE_PATH")
    neighbor_kinds = set((*a.passive_neighbor_kinds, *b.passive_neighbor_kinds))
    if neighbor_kinds:
        motifs.add("RLC_NETWORK")
    if passive_kind in {"L", "C"} and (
        ({"inductor", "capacitor"} & neighbor_kinds)
    ):
        motifs.add("LC_RESONANT_NETWORK")
    if circuit_type in {"ADC / DAC", "Comparator", "Switched-cap / sampler"}:
        if passive_kind == "C" and not all_roles & {"INPUT", "OUTPUT", "RF", "LO"}:
            if any(item.internal_relation or item.subcircuit_pin_count for item in nonground):
                motifs.add("CLOCKED_STORAGE_NETWORK")
    if circuit_type == "Power converter" and any(
        item.active_conduction_count for item in nonground
    ):
        motifs.add("POWER_SWITCHING_PATH")
    return motifs


def _endpoint_summary(endpoint: PassiveEndpointFeatures) -> str:
    active = ",".join(
        f"{key}:{value}"
        for key, value in sorted(endpoint.active_terminal_counts.items())
        if value
    ) or "none"
    two_hop = ",".join(
        f"{key}:{value}"
        for key, value in sorted(endpoint.two_hop_active_terminal_counts.items())
        if value
    ) or "none"
    return (
        f"node:{endpoint.node};ground:{str(endpoint.grounded).lower()};"
        f"active:{active};passive_neighbors:{endpoint.passive_neighbor_count};"
        f"subcircuit_pins:{endpoint.subcircuit_pin_count};"
        f"source_roles:{','.join(endpoint.source_roles) or 'none'};"
        f"two_hop_active:{two_hop};"
        f"two_hop_source_roles:{','.join(endpoint.two_hop_source_roles) or 'none'}"
    )
