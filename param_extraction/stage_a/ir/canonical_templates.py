"""Central technology-independent canonical physical parameter templates."""

from __future__ import annotations

from dataclasses import dataclass

from .circuit_ir import DeviceIR, ExpressionIR, PhysicalTargetIR


@dataclass(frozen=True)
class CanonicalParameterSpec:
    suffix: str
    parameter_kind: str


CANONICAL_DEVICE_TEMPLATES: dict[str, tuple[CanonicalParameterSpec, ...]] = {
    "mos": (
        CanonicalParameterSpec("W", "width"),
        CanonicalParameterSpec("L", "length"),
        CanonicalParameterSpec("m", "multiplicity"),
        CanonicalParameterSpec("nf", "finger_count"),
    ),
    "bjt": (
        CanonicalParameterSpec("area", "area"),
        CanonicalParameterSpec("m", "multiplicity"),
    ),
    "resistor": (
        CanonicalParameterSpec("resistance", "resistance"),
        CanonicalParameterSpec("W", "width"),
        CanonicalParameterSpec("L", "length"),
        CanonicalParameterSpec("m", "multiplicity"),
    ),
    "capacitor": (
        CanonicalParameterSpec("capacitance", "capacitance"),
        CanonicalParameterSpec("W", "width"),
        CanonicalParameterSpec("L", "length"),
        CanonicalParameterSpec("m", "multiplicity"),
    ),
    "inductor": (CanonicalParameterSpec("inductance", "inductance"),),
    "diode": (
        CanonicalParameterSpec("area", "area"),
        CanonicalParameterSpec("m", "multiplicity"),
    ),
}


_DEVICE_FAMILY: dict[str, str] = {
    "mos": "mos",
    "nmos": "mos",
    "pmos": "mos",
    "bjt": "bjt",
    "bjt_npn": "bjt",
    "bjt_pnp": "bjt",
    "resistor": "resistor",
    "capacitor": "capacitor",
    "inductor": "inductor",
    "diode": "diode",
}


def canonical_specs_for(device_type: str) -> tuple[CanonicalParameterSpec, ...]:
    family = _DEVICE_FAMILY.get(device_type.lower())
    return CANONICAL_DEVICE_TEMPLATES.get(family or "", ())


def generate_device_targets(device: DeviceIR) -> list[PhysicalTargetIR]:
    """Generate every canonical target, including omitted parameter slots."""

    targets: list[PhysicalTargetIR] = []
    for spec in canonical_specs_for(device.device_type):
        expression = device.explicit_parameter_expressions.get(
            spec.suffix, ExpressionIR.omitted()
        )
        target = PhysicalTargetIR(
            parameter_id=f"{device.device_id}.{spec.suffix}",
            device_id=device.device_id,
            device_type=device.device_type,
            parameter_kind=spec.parameter_kind,
            source_kind="device_instance",
            parameter_name=expression.direct_symbol if expression.explicit else None,
            source_expression=expression,
        )
        targets.append(target)
        device.canonical_parameter_targets.append(target.parameter_id)
    return targets


def source_target(
    *,
    parameter_id: str,
    source_id: str,
    source_type: str,
    value_expression: ExpressionIR,
) -> PhysicalTargetIR:
    kind = "voltage" if source_type == "voltage_source" else "current"
    return PhysicalTargetIR(
        parameter_id=parameter_id,
        device_id=source_id,
        device_type=source_type,
        parameter_kind=kind,
        source_kind="source_interface",
        parameter_name=value_expression.direct_symbol,
        source_expression=value_expression,
    )


def model_target(
    *, parameter_id: str, expression: ExpressionIR, device_id: str | None
) -> PhysicalTargetIR:
    return PhysicalTargetIR(
        parameter_id=parameter_id,
        device_id=device_id,
        device_type=None,
        parameter_kind="model_family",
        source_kind="model",
        parameter_name=expression.direct_symbol,
        source_expression=expression,
    )


def subcircuit_choice_target(
    *, instance_id: str, expression: ExpressionIR
) -> PhysicalTargetIR:
    return PhysicalTargetIR(
        parameter_id=f"{instance_id}.subcircuit_choice",
        device_id=instance_id,
        device_type="subcircuit",
        parameter_kind="subcircuit_choice",
        source_kind="subcircuit",
        parameter_name=expression.direct_symbol,
        source_expression=expression,
    )
