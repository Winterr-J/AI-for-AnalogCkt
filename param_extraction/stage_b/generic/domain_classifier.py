"""Classify optimizer-domain shape from frozen Stage A parameter kinds."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import StageATargetRecord
from ..core.types import DomainType, RangeStatus


@dataclass(frozen=True)
class DomainClassification:
    domain_type: DomainType
    unit: str | None
    range_status: RangeStatus
    range_reason: str


_CONTINUOUS = {
    "width": "m",
    "w": "m",
    "length": "m",
    "l": "m",
    "resistance": "ohm",
    "resistor": "ohm",
    "r": "ohm",
    "capacitance": "F",
    "capacitor": "F",
    "c": "F",
    "inductance": "H",
    "inductor": "H",
    "voltage": "V",
    "bias_voltage": "V",
    "current": "A",
    "bias_current": "A",
}

_INTEGER = {
    "multiplicity",
    "m",
    "nf",
    "fingers",
    "number_of_fingers",
}

_CATEGORICAL = {
    "model",
    "model_family",
    "subcircuit_choice",
    "topology_choice",
}


def classify_domain(target: StageATargetRecord) -> DomainClassification:
    kind = target.parameter_kind.strip().lower()
    if kind in _CONTINUOUS:
        return DomainClassification(
            domain_type=DomainType.CONTINUOUS,
            unit=_CONTINUOUS[kind],
            range_status=RangeStatus.UNCERTAIN,
            range_reason="RANGE_PROVIDER_NOT_APPLIED",
        )
    if kind in _INTEGER:
        return DomainClassification(
            domain_type=DomainType.INTEGER,
            unit=None,
            range_status=RangeStatus.UNCERTAIN,
            range_reason="INTEGER_RANGE_NOT_DEFINED_V1",
        )
    if kind in _CATEGORICAL:
        return DomainClassification(
            domain_type=DomainType.CATEGORICAL,
            unit=None,
            range_status=RangeStatus.UNCERTAIN,
            range_reason="CATEGORICAL_DOMAIN_NOT_SUPPORTED_V1",
        )
    return DomainClassification(
        domain_type=DomainType.UNSUPPORTED,
        unit=None,
        range_status=RangeStatus.UNCERTAIN,
        range_reason="DOMAIN_NOT_SUPPORTED_V1",
    )
