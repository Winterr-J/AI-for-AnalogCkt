"""Conservative, type-independent Tier-1 semantic rules.

Rules use canonical physical-target identity and reproducible structural facts
only. Names are never sufficient evidence for a semantic decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ir import CircuitIR, PhysicalTargetIR


@dataclass(frozen=True)
class Tier1Decision:
    semantic_category: str
    rule_id: str
    evidence: tuple[str, ...]


_MOS_TYPES = frozenset({"mos", "nmos", "pmos"})
_BJT_TYPES = frozenset({"bjt", "bjt_npn", "bjt_pnp"})
_PASSIVE_GEOMETRY_TYPES = frozenset({"resistor", "capacitor"})
_SIZE_KINDS = frozenset({"width", "length", "multiplicity", "finger_count"})
_AREA_MULTIPLICITY_KINDS = frozenset({"area", "multiplicity"})
_PASSIVE_ELECTRICAL_KINDS = frozenset({"resistance", "capacitance", "inductance"})
_SUPPLY_RAILS = frozenset({"VDD", "VSS"})
_GROUND_NODES = frozenset({"0", "gnd", "GND"})


def apply_tier1_rule(
    circuit: CircuitIR, target: PhysicalTargetIR
) -> Tier1Decision | None:
    """Return the first uniquely applicable deterministic rule, else None."""

    device_type = (target.device_type or "").lower()
    kind = target.parameter_kind.lower()

    if target.source_kind == "device_instance":
        if device_type in _MOS_TYPES and kind in _SIZE_KINDS:
            return _device_decision(target, "T1_MOS_PHYSICAL_SIZE")
        if device_type in _BJT_TYPES and kind in _AREA_MULTIPLICITY_KINDS:
            return _device_decision(target, "T1_BJT_PHYSICAL_SIZE")
        if device_type == "diode" and kind in _AREA_MULTIPLICITY_KINDS:
            return _device_decision(target, "T1_DIODE_PHYSICAL_SIZE")
        if device_type in _PASSIVE_GEOMETRY_TYPES and kind in _SIZE_KINDS:
            return _device_decision(target, "T1_PASSIVE_GEOMETRY")
        if kind in _PASSIVE_ELECTRICAL_KINDS:
            return None

    if target.source_kind == "model" and kind == "model_family":
        return Tier1Decision(
            "constraint_or_process",
            "T1_MODEL_FAMILY",
            (
                "source_kind=model",
                "parameter_kind=model_family",
                f"canonical_target={target.parameter_id}",
            ),
        )

    if target.source_kind == "subcircuit" and kind == "subcircuit_choice":
        return Tier1Decision(
            "topology_or_discrete",
            "T1_SUBCIRCUIT_CHOICE",
            (
                "source_kind=subcircuit",
                "parameter_kind=subcircuit_choice",
                f"canonical_target={target.parameter_id}",
            ),
        )

    supply = _deterministic_supply_source(circuit, target)
    if supply is not None:
        rail, source_id, p_node, n_node = supply
        return Tier1Decision(
            "operating_condition",
            "T1_SUPPLY_RAIL",
            (
                "source_type=voltage_source",
                f"source_id={source_id}",
                f"rail={rail}",
                f"terminals=p:{p_node},n:{n_node}",
                "one_terminal_is_ground=true",
                "non_ground_terminal_matches_rail=true",
            ),
        )

    return None


def _device_decision(target: PhysicalTargetIR, rule_id: str) -> Tier1Decision:
    return Tier1Decision(
        "device_parameter",
        rule_id,
        (
            f"device_type={target.device_type}",
            f"parameter_kind={target.parameter_kind}",
            f"canonical_target={target.parameter_id}",
        ),
    )


def _deterministic_supply_source(
    circuit: CircuitIR, target: PhysicalTargetIR
) -> tuple[str, str, str, str] | None:
    if (
        target.source_kind != "source_interface"
        or target.parameter_kind != "voltage"
        or target.device_type != "voltage_source"
    ):
        return None
    matches = [
        source
        for source in circuit.sources
        if source.source_id == target.device_id
        and source.physical_target == target.parameter_id
        and source.source_type == "voltage_source"
    ]
    if len(matches) != 1:
        return None
    source = matches[0]
    rail = target.parameter_id.upper()
    parameter_name = (target.parameter_name or "").upper()
    original_id = source.original_source_id.upper()
    p_node = source.terminals.get("p", "")
    n_node = source.terminals.get("n", "")
    p_upper = p_node.upper()
    n_upper = n_node.upper()
    if rail not in _SUPPLY_RAILS:
        return None
    if parameter_name != rail or original_id != rail:
        return None
    grounded_p = p_node in _GROUND_NODES
    grounded_n = n_node in _GROUND_NODES
    if grounded_p == grounded_n:
        return None
    non_ground = n_upper if grounded_p else p_upper
    if non_ground != rail:
        return None
    return rail, source.source_id, p_node, n_node
