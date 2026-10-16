"""Quantity-driven perturbation policies for CurrentMirror functional tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ExcitationPerturbationPolicy:
    """Nominal-centered scales for one external excitation quantity."""

    excitation_quantity: str
    primary_scales: tuple[float, ...]
    fallback_scales: tuple[float, ...] | None = None


def _validated_scales(scales: Sequence[float]) -> tuple[float, ...]:
    result = tuple(float(scale) for scale in scales)
    if not result:
        raise ValueError("perturbation scales must not be empty")
    if any(scale <= 0.0 for scale in result):
        raise ValueError("perturbation scales must contain only positive values")
    return result


def resolve_excitation_perturbation_policy(
    excitation_quantity: str | None,
    *,
    current_scales: Sequence[float] = (0.8, 1.0, 1.2),
) -> ExcitationPerturbationPolicy:
    """Return a generic policy selected by controllable-source quantity.

    Missing quantity metadata is treated as ``current`` for compatibility with
    legacy CurrentMirror adapters, whose reference perturbation API scales a
    current-source parameter.
    """

    quantity = str(excitation_quantity or "current").strip().casefold()
    if quantity == "current":
        return ExcitationPerturbationPolicy(
            excitation_quantity="current",
            primary_scales=_validated_scales(current_scales),
            fallback_scales=None,
        )
    if quantity == "voltage":
        return ExcitationPerturbationPolicy(
            excitation_quantity="voltage",
            primary_scales=(0.95, 1.0, 1.05),
            fallback_scales=(0.90, 1.0, 1.10),
        )
    raise ValueError(f"unsupported reference excitation quantity: {quantity!r}")


__all__ = [
    "ExcitationPerturbationPolicy",
    "resolve_excitation_perturbation_policy",
]
