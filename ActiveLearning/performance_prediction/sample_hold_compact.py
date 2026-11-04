"""SampleHold-only compact prediction prompt; shared predictor math is unchanged."""

from __future__ import annotations

import json
from pathlib import Path


_TEMPLATE = Path(__file__).parent / "prompts/sample_hold_compact_v1.txt"


def build_components(cases, diagnostics):
    pool, identities, compact_cases = [], {}, []
    for case in cases:
        candidate_id = case["candidate_id"]
        neighbors = []
        for historical, diagnostic in zip(
            case["historical_context"], diagnostics[candidate_id], strict=True
        ):
            identity = json.dumps(
                [diagnostic["sample_id"], historical["params"], historical["metrics"]],
                sort_keys=True, ensure_ascii=False,
            )
            if identity not in identities:
                pool_id = f"h{len(pool) + 1:03d}"
                identities[identity] = pool_id
                pool.append({"id": pool_id, "params": historical["params"],
                             "metrics": historical["metrics"]})
            comparison = {
                name: {key: value for key, value in values.items()
                       if key not in {"candidate_value", "historical_value"}}
                for name, values in historical["comparison_to_candidate"].items()
            }
            neighbors.append({"id": identities[identity],
                              "comparison_to_candidate": comparison})
        compact_cases.append({"candidate_id": candidate_id,
                              "params": case["params"], "neighbors": neighbors})
    return pool, compact_cases


def build_prompt(circuit_info, definitions, cases, diagnostics, parameter_template):
    if circuit_info.get("circuit_type") != "SampleHold":
        raise ValueError("SampleHold compact prompt cannot route another circuit type")
    if not isinstance(parameter_template, dict):
        raise ValueError("SampleHold compact prompt requires a parameter template")
    variables = parameter_template["optimization_space"]["variables"]
    entries = {item["name"]: item for item in parameter_template["parameters"]}
    if set(entries) != set(variables):
        raise ValueError("SampleHold optimization parameter definitions mismatch")
    parameter_definitions = {
        name: {
            "unit": entries[name].get("unit"),
            "scale": entries[name]["admissible_range"].get("scale"),
            "lower": entries[name]["admissible_range"]["lower"],
            "upper": entries[name]["admissible_range"]["upper"],
        }
        for name in variables
    }
    pool, compact_cases = build_components(cases, diagnostics)
    prompt = _TEMPLATE.read_text(encoding="utf-8")
    for name, value in {
        "circuit_info": circuit_info,
        "parameter_definitions": parameter_definitions,
        "performance_definitions": definitions,
        "historical_pool": pool,
        "prediction_cases": compact_cases,
    }.items():
        prompt = prompt.replace(
            f"{{{name}}}",
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    return prompt
