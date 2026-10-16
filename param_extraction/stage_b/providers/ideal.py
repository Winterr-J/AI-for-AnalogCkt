"""Frozen Stage B v1 target-local numerical ranges for Ideal technology."""

from __future__ import annotations

import math

from ..core.models import (
    CircuitContext,
    Diagnostic,
    NumericRange,
    OperatingContext,
    RangeDecision,
    TargetRangeRequest,
    TechnologyContext,
)
from ..core.types import DomainType, RangeStatus, Severity
from .base import RangeProvider


class IdealRangeProvider(RangeProvider):
    provider_name = "ideal_v1"

    def construct_target_range(
        self,
        target: TargetRangeRequest,
        circuit_context: CircuitContext,
        operating_context: OperatingContext,
        technology_context: TechnologyContext,
    ) -> RangeDecision:
        if technology_context.technology.strip().lower() != "ideal":
            return self._uncertain(
                target,
                "UNSUPPORTED_TECHNOLOGY_FOR_IDEAL_PROVIDER",
                "IdealRangeProvider requires technology=ideal.",
            )

        kind = target.parameter_kind.strip().lower()
        device = (target.device_type or "").strip().lower()
        if target.domain_type == DomainType.INTEGER:
            return self._uncertain(
                target,
                "INTEGER_RANGE_NOT_DEFINED_V1",
                "Stage B Ideal v1 does not define an integer range.",
            )

        if kind in {"width", "w", "length", "l"} and device in {
            "nmos",
            "pmos",
            "mos",
            "mosfet",
            "nfet",
            "pfet",
        }:
            return RangeDecision(
                range=NumericRange(lower=1e-8, upper=1e-4, scale="log"),
                status=RangeStatus.VALID,
                reason="IDEAL_MOS_ENGINEERING_RANGE_V1",
                provider=self.provider_name,
                evidence=(
                    "IDEAL_MOS_ENGINEERING_RANGE_V1",
                    "engineering admissible range; not a PDK legal range",
                    "SI unit: meter",
                ),
            )

        if kind in {
            "resistance",
            "resistor",
            "r",
            "capacitance",
            "capacitor",
            "c",
            "inductance",
            "inductor",
        }:
            nominal = target.nominal_value
            if nominal is None or not _is_finite_number(nominal) or float(nominal) <= 0:
                return self._uncertain(
                    target,
                    "PASSIVE_NOMINAL_REQUIRED",
                    "A finite positive passive nominal is required.",
                )
            value = float(nominal)
            return RangeDecision(
                range=NumericRange(lower=value / 100.0, upper=value * 100.0, scale="log"),
                status=RangeStatus.VALID,
                reason="IDEAL_PASSIVE_NOMINAL_WINDOW_V1",
                provider=self.provider_name,
                evidence=(
                    "IDEAL_PASSIVE_NOMINAL_WINDOW_V1",
                    "target-local range = nominal / 100 through nominal * 100",
                    "strictly positive physical domain",
                ),
            )

        if target.semantic_category == "bias_parameter" and kind in {
            "voltage",
            "bias_voltage",
        }:
            vdd = operating_context.vdd
            vss = operating_context.vss
            if vdd is None or vss is None:
                return self._uncertain(
                    target,
                    "OPERATING_RAILS_REQUIRED",
                    "Both VDD and VSS are required for a bias-voltage range.",
                )
            if not _is_finite_number(vdd) or not _is_finite_number(vss):
                return self._uncertain(
                    target,
                    "NONFINITE_OPERATING_RAIL",
                    "VDD and VSS must be finite.",
                )
            if float(vdd) <= float(vss):
                return self._conflict(
                    target,
                    "INVALID_OPERATING_RAIL_ORDER",
                    "Bias-voltage context requires VDD > VSS.",
                )
            return RangeDecision(
                range=NumericRange(lower=float(vss), upper=float(vdd), scale="linear"),
                status=RangeStatus.VALID,
                reason="IDEAL_BIAS_VOLTAGE_RAIL_RANGE_V1",
                provider=self.provider_name,
                evidence=(
                    "IDEAL_BIAS_VOLTAGE_RAIL_RANGE_V1",
                    "target-local range = [VSS, VDD]",
                ),
            )

        if target.semantic_category == "bias_parameter" and kind in {
            "current",
            "bias_current",
        }:
            nominal = target.nominal_value
            if nominal is None or not _is_finite_number(nominal) or float(nominal) == 0:
                return self._uncertain(
                    target,
                    "BIAS_CURRENT_NOMINAL_REQUIRED",
                    "A finite non-zero bias-current nominal is required.",
                )
            value = float(nominal)
            magnitude = abs(value)
            if value > 0:
                lower, upper, sign = magnitude / 100.0, magnitude * 100.0, "positive"
            else:
                lower, upper, sign = -magnitude * 100.0, -magnitude / 100.0, "negative"
            return RangeDecision(
                range=NumericRange(
                    lower=lower,
                    upper=upper,
                    scale="log-magnitude",
                    transform="log_magnitude",
                    fixed_sign=sign,
                ),
                status=RangeStatus.VALID,
                reason="IDEAL_BIAS_CURRENT_MAGNITUDE_RANGE_V1",
                provider=self.provider_name,
                evidence=(
                    "IDEAL_BIAS_CURRENT_MAGNITUDE_RANGE_V1",
                    "magnitude range = abs(nominal) / 100 through abs(nominal) * 100",
                    f"fixed current sign: {sign}",
                ),
            )

        return self._uncertain(
            target,
            "IDEAL_RANGE_RULE_NOT_DEFINED_V1",
            "No frozen Ideal v1 numerical rule applies to this target.",
        )

    def _uncertain(
        self, target: TargetRangeRequest, code: str, message: str
    ) -> RangeDecision:
        return RangeDecision(
            status=RangeStatus.UNCERTAIN,
            reason=code,
            provider=self.provider_name,
            diagnostics=(
                Diagnostic(
                    code=code,
                    message=message,
                    severity=Severity.WARNING,
                    parameter_ids=(target.parameter_id,),
                ),
            ),
        )

    def _conflict(
        self, target: TargetRangeRequest, code: str, message: str
    ) -> RangeDecision:
        return RangeDecision(
            status=RangeStatus.CONFLICT,
            reason=code,
            provider=self.provider_name,
            diagnostics=(
                Diagnostic(
                    code=code,
                    message=message,
                    severity=Severity.ERROR,
                    parameter_ids=(target.parameter_id,),
                ),
            ),
        )


def _is_finite_number(value: float | int) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
