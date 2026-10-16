"""Candidate legitimacy and scope validation over Circuit IR.

PySpice is the trusted structural input.  This validator does not compare it
with an original netlist, infer semantics, or decide optimization eligibility.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..ir.canonical_templates import canonical_specs_for
from ..ir.circuit_ir import CircuitIR, PhysicalTargetIR
from .scope_rules import classify_scope


VALIDITY_STATUSES = frozenset({"VALID", "INVALID", "UNCERTAIN", "OUT_OF_SCOPE"})

_NON_PHYSICAL_KINDS = frozenset(
    {
        "temporary",
        "temporary_helper",
        "helper_variable",
        "alias_artifact",
        "formatting_variable",
        "orphan_symbol",
        "synthetic_duplicate",
    }
)

_KNOWN_SUBCIRCUIT_PHYSICAL_PARAMETERS = frozenset(
    {
        "w",
        "width",
        "l",
        "length",
        "m",
        "multiplier",
        "nf",
        "finger_count",
        "fingers",
        "area",
        "r",
        "resistance",
        "c",
        "capacitance",
        "inductance",
    }
)

_IDENTITY_BLOCKING_DIAGNOSTICS = frozenset(
    {
        "PYTHON_SYNTAX_ERROR",
        "BUILD_CIRCUIT_NOT_FOUND",
        "UNSUPPORTED_CONSTRUCTION_CONTROL_FLOW",
        "RAW_SPICE_DYNAMIC_STRING",
        "RAW_SPICE_PARSE_ERROR",
        "RAW_SPICE_UNSUPPORTED_ELEMENT",
        "MALFORMED_CONSTRUCTOR",
        "DYNAMIC_INSTANCE_NAME",
    }
)


@dataclass(frozen=True)
class CandidateValidityResult:
    parameter_id: str
    validity_status: str
    validity_rule_id: str
    rule_tier: str
    confidence: str
    review_status: str
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "parameter_id": self.parameter_id,
            "validity_status": self.validity_status,
            "validity_rule_id": self.validity_rule_id,
            "rule_tier": self.rule_tier,
            "confidence": self.confidence,
            "review_status": self.review_status,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class CircuitValidityResult:
    circuit_id: str
    circuit_type: str
    candidates: tuple[CandidateValidityResult, ...]
    circuit_diagnostics: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        counts = Counter(item.validity_status for item in self.candidates)
        return {status: counts.get(status, 0) for status in sorted(VALIDITY_STATUSES)}


class CandidateValidator:
    """Apply frozen structural validity/scope rules to physical targets."""

    def validate(self, circuit: CircuitIR) -> CircuitValidityResult:
        id_counts = Counter(target.parameter_id for target in circuit.physical_targets)
        candidates = tuple(
            self.validate_target(circuit, target, duplicate_count=id_counts[target.parameter_id])
            for target in circuit.physical_targets
        )
        return CircuitValidityResult(
            circuit_id=circuit.circuit_id,
            circuit_type=circuit.circuit_type,
            candidates=candidates,
            circuit_diagnostics=self._identity_diagnostics(circuit),
        )

    def validate_target(
        self,
        circuit: CircuitIR,
        target: PhysicalTargetIR,
        *,
        duplicate_count: int | None = None,
    ) -> CandidateValidityResult:
        count = duplicate_count
        if count is None:
            count = sum(
                candidate.parameter_id == target.parameter_id
                for candidate in circuit.physical_targets
            )
        if count != 1:
            return self._result(
                target,
                "INVALID",
                "INVALID_NON_PHYSICAL_ARTIFACT",
                evidence=(
                    f"parameter_id occurs {count} times and has no unique physical target",
                ),
            )

        scope = classify_scope(target)
        if scope is not None:
            return self._result(
                target,
                scope.validity_status,
                scope.validity_rule_id,
                evidence=scope.evidence,
            )

        kind = target.parameter_kind.strip().lower()
        if kind in _NON_PHYSICAL_KINDS:
            return self._result(
                target,
                "INVALID",
                "INVALID_NON_PHYSICAL_ARTIFACT",
                evidence=(f"non-physical extraction-artifact kind: {kind}",),
            )

        if target.source_kind == "device_instance":
            return self._validate_device_target(circuit, target)
        if target.source_kind == "source_interface":
            return self._validate_source_target(circuit, target)
        if target.source_kind == "model":
            return self._validate_model_target(circuit, target)
        if target.source_kind == "subcircuit":
            return self._validate_subcircuit_target(circuit, target)

        return self._result(
            target,
            "UNCERTAIN",
            "UNCERTAIN_TARGET_IDENTITY",
            evidence=(f"unrecognized physical-target source kind: {target.source_kind}",),
        )

    def _validate_device_target(
        self, circuit: CircuitIR, target: PhysicalTargetIR
    ) -> CandidateValidityResult:
        parent = next(
            (device for device in circuit.devices if device.device_id == target.device_id),
            None,
        )
        if parent is None:
            return self._result(
                target,
                "UNCERTAIN",
                "UNCERTAIN_TARGET_IDENTITY",
                evidence=("parent device instance is not present in Circuit IR",),
            )
        specs = canonical_specs_for(parent.device_type)
        expected_kinds = {spec.parameter_kind for spec in specs}
        if not specs:
            return self._result(
                target,
                "UNCERTAIN",
                "UNCERTAIN_UNSUPPORTED_DEVICE",
                evidence=(f"unsupported device representation: {parent.device_type}",),
            )
        if (
            target.parameter_id not in parent.canonical_parameter_targets
            or target.parameter_kind not in expected_kinds
        ):
            return self._result(
                target,
                "UNCERTAIN",
                "UNCERTAIN_TARGET_IDENTITY",
                evidence=(
                    "target is not a member of the parent's applicable canonical template",
                ),
            )
        return self._result(
            target,
            "VALID",
            "VALID_CANONICAL_DEVICE_TARGET",
            evidence=(
                f"parent device {parent.device_id} exists",
                f"canonical template applies to {parent.device_type}",
            ),
        )

    def _validate_source_target(
        self, circuit: CircuitIR, target: PhysicalTargetIR
    ) -> CandidateValidityResult:
        parent = next(
            (
                source
                for source in circuit.sources
                if source.source_id == target.device_id
                and source.physical_target == target.parameter_id
            ),
            None,
        )
        if parent is None or target.parameter_kind not in {"voltage", "current"}:
            return self._result(
                target,
                "UNCERTAIN",
                "UNCERTAIN_TARGET_IDENTITY",
                evidence=("source value target cannot be matched to one source interface",),
            )
        return self._result(
            target,
            "VALID",
            "VALID_SOURCE_VALUE",
            evidence=(
                f"source interface {parent.source_id} exists",
                f"source type: {parent.source_type}",
            ),
        )

    def _validate_model_target(
        self, circuit: CircuitIR, target: PhysicalTargetIR
    ) -> CandidateValidityResult:
        matches = [
            model
            for model in circuit.models
            if model.physical_target == target.parameter_id
        ]
        if len(matches) != 1 or target.parameter_kind != "model_family":
            return self._result(
                target,
                "UNCERTAIN",
                "UNCERTAIN_TARGET_IDENTITY",
                evidence=("model target cannot be matched to one model record",),
            )
        return self._result(
            target,
            "VALID",
            "VALID_MODEL_TARGET",
            evidence=(f"model record exists: {matches[0].model_id}",),
        )

    def _validate_subcircuit_target(
        self, circuit: CircuitIR, target: PhysicalTargetIR
    ) -> CandidateValidityResult:
        parent = next(
            (
                subcircuit
                for subcircuit in circuit.subcircuits
                if subcircuit.instance_id == target.device_id
            ),
            None,
        )
        if parent is None or target.parameter_id not in parent.canonical_parameter_targets:
            return self._result(
                target,
                "UNCERTAIN",
                "UNCERTAIN_TARGET_IDENTITY",
                evidence=("subcircuit target has no unique parent interface record",),
            )
        if target.parameter_kind == "subcircuit_choice":
            return self._result(
                target,
                "VALID",
                "VALID_SUBCIRCUIT_TARGET",
                evidence=(f"subcircuit instance exists: {parent.instance_id}",),
            )
        if target.parameter_kind == "exposed_parameter":
            exposed_name = target.parameter_id.rsplit(".", 1)[-1].lower()
            if exposed_name in _KNOWN_SUBCIRCUIT_PHYSICAL_PARAMETERS:
                return self._result(
                    target,
                    "VALID",
                    "VALID_SUBCIRCUIT_TARGET",
                    evidence=(
                        f"explicit known physical subcircuit parameter: {exposed_name}",
                    ),
                )
            return self._result(
                target,
                "UNCERTAIN",
                "UNCERTAIN_SUBCIRCUIT_PARAMETER",
                evidence=(
                    f"exposed subcircuit parameter has unknown physical identity: {exposed_name}",
                ),
            )
        return self._result(
            target,
            "UNCERTAIN",
            "UNCERTAIN_TARGET_IDENTITY",
            evidence=(f"unsupported subcircuit parameter kind: {target.parameter_kind}",),
        )

    @staticmethod
    def _result(
        target: PhysicalTargetIR,
        status: str,
        rule_id: str,
        *,
        evidence: tuple[str, ...],
    ) -> CandidateValidityResult:
        uncertain = status == "UNCERTAIN"
        return CandidateValidityResult(
            parameter_id=target.parameter_id,
            validity_status=status,
            validity_rule_id=rule_id,
            rule_tier="TIER_3" if uncertain else "TIER_1",
            confidence="LOW" if uncertain else "HIGH",
            review_status="NEEDS_REVIEW" if uncertain else "AUTO",
            evidence=evidence,
        )

    @staticmethod
    def _identity_diagnostics(circuit: CircuitIR) -> tuple[str, ...]:
        """Expose circuit-level identity risk without relabeling clear targets."""

        return tuple(
            f"UNCERTAIN_TARGET_IDENTITY: {item.code}: {item.message}"
            for item in circuit.diagnostics
            if item.code in _IDENTITY_BLOCKING_DIAGNOSTICS
        )


def validate_candidates(circuit: CircuitIR) -> CircuitValidityResult:
    return CandidateValidator().validate(circuit)
