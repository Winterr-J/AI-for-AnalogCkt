"""SCIntegrator compact prediction prompts; retrieval and committee math stay shared."""

from __future__ import annotations

import json
from pathlib import Path

from .sample_hold_compact import build_components


_PROMPTS = Path(__file__).parent / "prompts"
_V1_GENERIC = _PROMPTS / "generic_performance_prediction_v1.txt"
_SC_GUIDANCE = _PROMPTS / "circuit_types/sc_integrator_bulk_v1.txt"
_V2 = _PROMPTS / "circuit_types/sc_integrator_compact_v2.txt"


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _without_repeated_values(comparison):
    return {
        name: {key: value for key, value in values.items()
               if key not in {"candidate_value", "historical_value"}}
        for name, values in comparison.items()
    }


def build_p0_components(cases):
    """Keep neighbors inline, dropping only values already in their two vectors."""
    return [
        {"candidate_id": case["candidate_id"], "params": case["params"],
         "historical_context": [
             {"params": neighbor["params"], "metrics": neighbor["metrics"],
              "comparison_to_candidate": _without_repeated_values(
                  neighbor["comparison_to_candidate"])}
             for neighbor in case["historical_context"]]}
        for case in cases
    ]


def build_p0_prompt(circuit_info, definitions, cases):
    """Offline comparison baseline: V1 instructions with compact inline data."""
    if circuit_info.get("circuit_type") != "SCIntegrator":
        raise ValueError("SCIntegrator P0 prompt cannot route another circuit type")
    prompt = _V1_GENERIC.read_text(encoding="utf-8")
    for name, value in {
        "circuit_info": circuit_info,
        "performance_definitions": definitions,
        "prediction_cases": build_p0_components(cases),
    }.items():
        prompt = prompt.replace(f"{{{name}}}", _compact_json(value))
    return prompt.rstrip() + "\n\n" + _SC_GUIDANCE.read_text(
        encoding="utf-8").strip() + "\n"


def build_prompt(circuit_info, definitions, cases, diagnostics):
    if circuit_info.get("circuit_type") != "SCIntegrator":
        raise ValueError("SCIntegrator Compact V2 cannot route another circuit type")
    pool, compact_cases = build_components(cases, diagnostics)
    prompt = _V2.read_text(encoding="utf-8")
    for name, value in {
        "circuit_info": circuit_info,
        "performance_definitions": definitions,
        "historical_pool": pool,
        "prediction_cases": compact_cases,
        "candidate_ids": [case["candidate_id"] for case in compact_cases],
        "metric_names": list(definitions),
    }.items():
        prompt = prompt.replace(f"{{{name}}}", _compact_json(value))
    return prompt.rstrip() + "\n\n" + _SC_GUIDANCE.read_text(
        encoding="utf-8").strip() + "\n"
