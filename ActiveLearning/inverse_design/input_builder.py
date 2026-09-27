"""Build structured, provider-independent inputs for inverse design."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any


_STRUCTURED_RELATIONS = {
    "equality",
    "ratio",
    "shared_parameter",
    "discrete_compatibility",
}


def _rule_references(rule: Mapping[str, Any]) -> set[str]:
    references = set()
    for key in ("members", "applies_to", "dependent_variables"):
        values = rule.get(key, ())
        if isinstance(values, (list, tuple, set)):
            references.update(str(value) for value in values)
    for key in (
        "left",
        "right",
        "dependent",
        "independent",
        "independent_variable",
    ):
        if rule.get(key) is not None:
            references.add(str(rule[key]))
    return references


def _extract_parameter_space(parameter_template: Mapping[str, Any]) -> dict[str, Any]:
    variable_names = list(parameter_template["optimization_space"]["variables"])
    definitions = {
        parameter["name"]: parameter for parameter in parameter_template["parameters"]
    }
    variables = {}
    for name in variable_names:
        definition = definitions[name]
        variable = {
            "type": deepcopy(definition["variable_type"]),
            "nominal": deepcopy(definition.get("nominal_value")),
        }
        variable_type = definition["variable_type"]
        if variable_type == "continuous":
            admissible_range = definition["admissible_range"]
            variable["lower"] = deepcopy(admissible_range["lower"])
            variable["upper"] = deepcopy(admissible_range["upper"])
        elif variable_type in {"discrete", "categorical"}:
            variable["options"] = deepcopy(definition.get("discrete_options", []))
        variables[name] = variable

    optimization_names = set(variable_names)
    constraints = []
    for rule in [
        *(parameter_template.get("bindings") or []),
        *(parameter_template.get("constraints") or []),
    ]:
        relation_type = rule.get("relation_type", rule.get("type"))
        if (
            relation_type in _STRUCTURED_RELATIONS
            and _rule_references(rule) & optimization_names
        ):
            constraints.append(deepcopy(rule))

    return {"variables": variables, "constraints": constraints}


def _prepare_context_samples(
    context_samples: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    prepared = []
    for sample in context_samples:
        context = {
            "params": deepcopy(sample["params"]),
            "metrics": deepcopy(sample["metrics"]),
            "functional_feasible": deepcopy(
                sample.get("status", {}).get("functional_feasible")
            ),
        }
        if "sample_id" in sample:
            context = {"sample_id": deepcopy(sample["sample_id"]), **context}
        prepared.append(context)
    return prepared


def build_inverse_design_input(
    context_samples: Sequence[Mapping[str, Any]],
    circuit_info: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
    target_specs: Mapping[str, Any] | None,
    best_known_anchor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the structured inputs consumed by downstream design stages."""

    return {
        "circuit_info": deepcopy(circuit_info),
        "parameter_space": _extract_parameter_space(parameter_template),
        "target_specs": deepcopy(target_specs),
        "best_known_anchor": deepcopy(best_known_anchor),
        "context_samples": _prepare_context_samples(context_samples),
    }
