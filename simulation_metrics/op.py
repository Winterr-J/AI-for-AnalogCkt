"""Operating-point simulation and health metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


CheckState = bool | None


@dataclass
class OperatingPointResult:
    analysis: Any
    node_voltages_v: dict[str, float]
    mos_operating_points: dict[str, dict[str, float | str | bool | None]]
    supply_current_a: float | None
    checks: dict[str, CheckState]
    warnings: tuple[str, ...]
    passed: bool


def _array(value: Any) -> np.ndarray:
    return np.asarray(value, dtype=float).reshape(-1)


def _mapping_lookup(mapping: Mapping[Any, Any], name: str) -> tuple[str, Any] | None:
    target = name.casefold()
    for key, value in mapping.items():
        if str(key).casefold() == target:
            return str(key), value
    return None


def _analysis_lookup(
    analysis: Any,
    name: str,
    *,
    sections: Sequence[str] = ("nodes", "branches", "internal_parameters"),
) -> tuple[str, Any] | None:
    for section_name in sections:
        section = getattr(analysis, section_name, None)
        if isinstance(section, Mapping):
            match = _mapping_lookup(section, name)
            if match is not None:
                return match
    try:
        return name, analysis[name]
    except (KeyError, IndexError, TypeError):
        return None


def _scalar(analysis: Any, name: str, *, sections: Sequence[str]) -> float | None:
    match = _analysis_lookup(analysis, name, sections=sections)
    if match is None:
        return None
    values = _array(match[1])
    return float(values[0]) if values.size else None


def _node_voltages(analysis: Any) -> dict[str, float]:
    nodes = getattr(analysis, "nodes", {})
    if not isinstance(nodes, Mapping):
        return {}
    result: dict[str, float] = {}
    for name, vector in nodes.items():
        values = _array(vector)
        if values.size:
            result[str(name)] = float(values[0])
    return result


def _canonical_node_name(nodes: Mapping[str, float], requested: str) -> str | None:
    target = requested.casefold()
    return next((name for name in nodes if name.casefold() == target), None)


def _discover_mosfet_names(circuit: Any) -> list[str]:
    result: list[str] = []
    for element in getattr(circuit, "elements", ()):
        name = str(getattr(element, "name", ""))
        class_name = type(element).__name__.casefold()
        if name.upper().startswith("M") or "mosfet" in class_name:
            result.append(name)
    return result


def run_operating_point(
    circuit: Any,
    *,
    vdd_v: float = 3.3,
    output_node: str = "VOUT1",
    supply_source: str = "VVDD",
    bias_nodes: Iterable[str] | None = None,
    intermediate_nodes: Iterable[str] | None = None,
    mosfet_names: Iterable[str] | None = None,
    rail_margin_v: float | None = None,
    minimum_mos_current_a: float = 1e-9,
    maximum_supply_current_a: float = 0.1,
    saturation_tolerance_v: float = 1e-3,
    temperature_c: float = 25.0,
    nominal_temperature_c: float = 25.0,
) -> OperatingPointResult:
    """Run OP and check output rails, internal nodes, MOS conduction and current."""

    if vdd_v <= 0:
        raise ValueError("vdd_v must be positive")
    rail_margin = 0.05 * vdd_v if rail_margin_v is None else float(rail_margin_v)
    if rail_margin < 0 or rail_margin >= 0.5 * vdd_v:
        raise ValueError("rail_margin_v must be in [0, VDD/2)")

    selected_mosfets = list(mosfet_names or _discover_mosfet_names(circuit))
    simulator = circuit.simulator(
        temperature=temperature_c,
        nominal_temperature=nominal_temperature_c,
    )
    analysis = simulator.operating_point()
    internal_names = [
        f"@{name}[{parameter}]"
        for name in selected_mosfets
        for parameter in ("id", "gm", "vgs", "vds", "vth", "vdsat")
    ]
    if internal_names:
        internal_simulator = circuit.simulator(
            temperature=temperature_c,
            nominal_temperature=nominal_temperature_c,
        )
        internal_simulator.save_internal_parameters(*internal_names)
        internal_analysis = internal_simulator.operating_point()
    else:
        internal_analysis = analysis

    nodes = _node_voltages(analysis)
    warnings: list[str] = []
    checks: dict[str, CheckState] = {}
    output_key = _canonical_node_name(nodes, output_node)
    if output_key is None:
        checks["output_not_at_rail"] = None
        warnings.append(f"output node {output_node!r} was not present in OP results")
    else:
        output_v = nodes[output_key]
        output_ok = rail_margin < output_v < vdd_v - rail_margin
        checks["output_not_at_rail"] = output_ok
        if not output_ok:
            warnings.append(f"{output_key}={output_v:.6g} V is at or close to a supply rail")

    requested_bias_nodes = (
        list(bias_nodes)
        if bias_nodes is not None
        else [name for name in nodes if name.casefold().startswith("vb")]
    )
    requested_intermediate_nodes = (
        list(intermediate_nodes)
        if intermediate_nodes is not None
        else [name for name in nodes if name.casefold().startswith("net")]
    )
    for category, requested_nodes in (
        ("bias", requested_bias_nodes),
        ("intermediate", requested_intermediate_nodes),
    ):
        for requested in requested_nodes:
            key = _canonical_node_name(nodes, requested)
            check_name = f"{category}_node_{requested}_within_rails"
            if key is None:
                checks[check_name] = None
                warnings.append(f"{category} node {requested!r} was not present in OP results")
                continue
            value = nodes[key]
            reasonable = rail_margin < value < vdd_v - rail_margin
            checks[check_name] = reasonable
            if not reasonable:
                warnings.append(f"{category} node {key}={value:.6g} V is at or close to a rail")

    mos_points: dict[str, dict[str, float | str | bool | None]] = {}
    for name in selected_mosfets:
        values = {
            parameter: _scalar(
                internal_analysis,
                f"@{name}[{parameter}]",
                sections=("internal_parameters",),
            )
            for parameter in ("id", "gm", "vgs", "vds", "vth", "vdsat")
        }
        drain_current = values["id"]
        conducting = (
            None
            if drain_current is None
            else abs(float(drain_current)) >= minimum_mos_current_a
        )
        if conducting is False:
            region = "cutoff"
        elif conducting is True and values["vds"] is not None and values["vdsat"] is not None:
            region = (
                "saturation"
                if abs(float(values["vds"])) + saturation_tolerance_v
                >= abs(float(values["vdsat"]))
                else "triode"
            )
        else:
            region = "unknown"
        mos_points[name] = {**values, "conducting": conducting, "region": region}
        checks[f"mos_{name}_conducting"] = conducting
        if conducting is False:
            warnings.append(f"MOS {name} appears to be off")
        elif conducting is None:
            warnings.append(f"MOS {name} drain current was not available")

    supply_current = _scalar(analysis, supply_source, sections=("branches",))
    if supply_current is None and not supply_source.casefold().endswith("#branch"):
        supply_current = _scalar(
            analysis,
            f"{supply_source}#branch",
            sections=("branches",),
        )
    if supply_current is None:
        checks["supply_current_not_abnormal"] = None
        warnings.append(f"supply current for {supply_source!r} was not available")
    else:
        current_ok = abs(supply_current) <= maximum_supply_current_a
        checks["supply_current_not_abnormal"] = current_ok
        if not current_ok:
            warnings.append(
                f"abs(supply current)={abs(supply_current):.6g} A exceeds "
                f"{maximum_supply_current_a:.6g} A"
            )

    passed = bool(checks) and all(state is True for state in checks.values())
    return OperatingPointResult(
        analysis=analysis,
        node_voltages_v=nodes,
        mos_operating_points=mos_points,
        supply_current_a=supply_current,
        checks=checks,
        warnings=tuple(warnings),
        passed=passed,
    )
