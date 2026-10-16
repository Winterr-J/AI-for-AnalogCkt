"""Circuit-agnostic parameter validity checks driven by a parameter template."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


_REL_TOL = 1.0e-9
_ABS_TOL = 1.0e-12


def _validate_optimization_parameters(
    params: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
) -> list[str]:
    violations = []
    variable_names = parameter_template["optimization_space"]["variables"]
    unexpected_names = sorted(set(params) - set(variable_names))
    for name in unexpected_names:
        violations.append(f"Unexpected optimization parameter: {name}")
    parameters_by_name = {
        parameter["name"]: parameter for parameter in parameter_template["parameters"]
    }

    for name in variable_names:
        if name not in params:
            violations.append(f"Missing optimization parameter: {name}")
            continue

        definition = parameters_by_name[name]
        variable_type = definition["variable_type"]
        value = params[name]
        if variable_type == "continuous":
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                violations.append(f"{name}={value!r} is not a valid continuous value")
                continue
            lower = float(definition["admissible_range"]["lower"])
            upper = float(definition["admissible_range"]["upper"])
            if not math.isfinite(numeric_value) or not lower <= numeric_value <= upper:
                violations.append(
                    f"{name}={value!r} is outside admissible range "
                    f"[{lower!r}, {upper!r}]"
                )
        elif variable_type == "discrete":
            options = definition.get("discrete_options", [])
            if value not in options:
                violations.append(
                    f"{name}={value!r} is not in allowed discrete options {options!r}"
                )
        else:
            violations.append(f"{name} has unsupported variable_type {variable_type!r}")
    return violations


def _equality_members(rule: Mapping[str, Any]) -> list[str]:
    members = rule.get("members")
    if isinstance(members, (list, tuple)) and len(members) >= 2:
        return [str(member) for member in members]
    if "left" in rule and "right" in rule:
        return [str(rule["left"]), str(rule["right"])]
    if "dependent" in rule and "independent" in rule:
        return [str(rule["dependent"]), str(rule["independent"])]
    return []


def _values_equal(first: Any, second: Any) -> bool:
    try:
        return math.isclose(
            float(first),
            float(second),
            rel_tol=_REL_TOL,
            abs_tol=_ABS_TOL,
        )
    except (TypeError, ValueError):
        return first == second


def _validate_relations(
    params: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
) -> list[str]:
    violations = []
    rules = [
        *(parameter_template.get("bindings") or []),
        *(parameter_template.get("constraints") or []),
    ]
    for rule in rules:
        relation_type = rule.get("relation_type", rule.get("type"))
        if relation_type == "equality":
            members = _equality_members(rule)
            if len(members) < 2 or any(member not in params for member in members):
                continue
            reference = members[0]
            for member in members[1:]:
                if not _values_equal(params[reference], params[member]):
                    violations.append(
                        f"{reference}={params[reference]!r} violates equality constraint "
                        f"{reference} = {member} with {member}={params[member]!r}"
                    )
        elif relation_type == "ratio":
            dependent = rule.get("dependent")
            independent = rule.get("independent")
            ratio = rule.get("ratio")
            if dependent not in params or independent not in params or ratio is None:
                continue
            try:
                actual = float(params[dependent])
                expected = float(ratio) * float(params[independent])
            except (TypeError, ValueError):
                continue
            if not math.isclose(
                actual,
                expected,
                rel_tol=_REL_TOL,
                abs_tol=_ABS_TOL,
            ):
                violations.append(
                    f"{dependent}={params[dependent]!r} violates ratio constraint "
                    f"{dependent} = {ratio!r} * {independent} with "
                    f"{independent}={params[independent]!r}"
                )
        # shared_parameter is expanded by the circuit adapter/builder. Other
        # unstructured rule types are intentionally left to future versions.
    return violations


def validate_parameters(
    params: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate candidate parameters against explicit template rules."""

    violations = _validate_optimization_parameters(params, parameter_template)
    violations.extend(_validate_relations(params, parameter_template))
    return {
        "parameter_valid": not violations,
        "violations": violations,
    }
