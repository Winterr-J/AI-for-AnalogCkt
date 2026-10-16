"""Orchestrate the V1 LLM inverse-design pipeline."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

from ActiveLearning.common import parameter_to_similarity_vector

from .context_selector import select_best_known_anchor, select_inverse_design_context
from .input_builder import build_inverse_design_input
from .llm_client import call_inverse_design_llm
from .prompt_builder import build_inverse_design_prompt


def _strip_markdown_fence(raw_response: str) -> str:
    stripped = raw_response.strip()
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[0].strip().casefold() in {"```", "```json"}:
        if lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _parse_x_seed(raw_response: str) -> dict[str, Any]:
    if not isinstance(raw_response, str):
        raise TypeError("LLM response must be text")
    payload = json.loads(_strip_markdown_fence(raw_response))
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    if "x_seed" not in payload:
        raise ValueError("LLM response is missing x_seed")
    if not isinstance(payload["x_seed"], Mapping):
        raise ValueError("LLM response x_seed must be a mapping")
    return dict(payload["x_seed"])


def validate_parameters(
    params: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
) -> dict[str, Any]:
    """Load and call the existing D0 validator only when a seed is ready."""

    from ActiveLearning.InitializeD0.validation import (
        validate_parameters as validate_d0_parameters,
    )

    return validate_d0_parameters(params, parameter_template)


def _compare_seed_to_anchor(
    x_seed: Mapping[str, Any],
    anchor_params: Mapping[str, Any],
) -> dict[str, Any]:
    comparison = {}
    for name, anchor_value in anchor_params.items():
        seed_value = x_seed.get(name)
        ratio = None
        if (
            isinstance(seed_value, Real)
            and not isinstance(seed_value, bool)
            and isinstance(anchor_value, Real)
            and not isinstance(anchor_value, bool)
        ):
            seed_numeric = float(seed_value)
            anchor_numeric = float(anchor_value)
            if (
                seed_numeric != 0.0
                and anchor_numeric != 0.0
                and (seed_numeric < 0.0) == (anchor_numeric < 0.0)
            ):
                ratio = (
                    abs(seed_numeric) / abs(anchor_numeric)
                    if seed_numeric < 0.0
                    else seed_numeric / anchor_numeric
                )
        comparison[name] = {
            "seed_value": seed_value,
            "anchor_value": anchor_value,
            "magnitude_ratio_or_relative_ratio": ratio,
        }
    return comparison


def propose_inverse_design_seed(
    records: Sequence[Mapping[str, Any]],
    circuit_info: Mapping[str, Any],
    parameter_template: Mapping[str, Any],
    target_specs: Mapping[str, Any] | None,
    *,
    context_k: int = 5,
    mmr_lambda: float = 0.7,
    api_key: str | None = None,
    model: str | None = None,
    llm_client=None,
) -> dict[str, Any]:
    """Propose and lightly validate one LLM-generated inverse-design seed."""

    if target_specs is None or target_specs == {}:
        raise ValueError("target_specs are required for inverse design")

    selected_context, context_diagnostics = select_inverse_design_context(
        records,
        target_specs,
        k=context_k,
        lambda_weight=mmr_lambda,
        parameter_template=parameter_template,
        return_diagnostics=True,
    )
    best_known_anchor = select_best_known_anchor(records, target_specs)
    inverse_input = build_inverse_design_input(
        context_samples=selected_context,
        circuit_info=circuit_info,
        parameter_template=parameter_template,
        target_specs=target_specs,
        best_known_anchor=best_known_anchor,
    )
    prompt = build_inverse_design_prompt(inverse_input)
    if llm_client is None:
        raw_response = call_inverse_design_llm(
            prompt,
            api_key=api_key,
            model=model,
        )
    else:
        raw_response = llm_client(prompt)
    x_seed = _parse_x_seed(raw_response)
    parameter_result = validate_parameters(x_seed, parameter_template)
    if parameter_result["parameter_valid"] is True:
        seed_vector = parameter_to_similarity_vector(x_seed, parameter_template)
        anchor_vector = parameter_to_similarity_vector(
            best_known_anchor["params"],
            parameter_template,
        )
        x_seed_anchor_distance = float(math.dist(seed_vector, anchor_vector))
    else:
        x_seed_anchor_distance = None
    return {
        "x_seed": x_seed,
        "inverse_context": context_diagnostics,
        "best_known_anchor": best_known_anchor,
        "x_seed_vs_anchor": _compare_seed_to_anchor(
            x_seed,
            best_known_anchor["params"],
        ),
        "x_seed_anchor_distance": x_seed_anchor_distance,
        **parameter_result,
    }
