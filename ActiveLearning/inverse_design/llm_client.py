"""Minimal DeepSeek OpenAI-compatible client wrapper."""

from __future__ import annotations

import os


_API_KEY_PLACEHOLDER = "YOUR_API_KEY_HERE"
_MODEL_PLACEHOLDER = "YOUR_MODEL_NAME_HERE"
_BASE_URL_PLACEHOLDER = "YOUR_BASE_URL_HERE"


# ---------------------------------------------------------------------
# Keep these module-level variables for backward compatibility.
# Other ActiveLearning modules import LLM_API_KEY / LLM_MODEL directly.
# ---------------------------------------------------------------------

LLM_API_KEY = os.getenv(
    "OPENAI_API_KEY",
    _API_KEY_PLACEHOLDER,
)

LLM_MODEL = os.getenv(
    "OPENAI_MODEL",
    _MODEL_PLACEHOLDER,
)

LLM_BASE_URL = os.getenv(
    "OPENAI_BASE_URL",
    _BASE_URL_PLACEHOLDER,
)


def call_inverse_design_llm(
    prompt: str,
    api_key: str = LLM_API_KEY,
    model: str = LLM_MODEL,
    base_url: str = LLM_BASE_URL,
    usage_log=None,
    usage_context=None,
) -> str:
    """Send one prompt to DeepSeek and return the raw response text."""

    # -----------------------------------------------------------------
    # Configuration validation
    # -----------------------------------------------------------------

    if (
        not api_key
        or api_key == _API_KEY_PLACEHOLDER
        or not model
        or model == _MODEL_PLACEHOLDER
        or not base_url
        or base_url == _BASE_URL_PLACEHOLDER
    ):
        raise ValueError(
            "LLM API configuration has not been configured"
        )

    # -----------------------------------------------------------------
    # Import OpenAI-compatible client
    # -----------------------------------------------------------------

    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError(
            "The openai package is required for configured LLM calls"
        ) from error

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
    )

    # -----------------------------------------------------------------
    # DeepSeek Responses API call
    # -----------------------------------------------------------------

    response = client.responses.create(
        model=model,
        input=prompt,
        reasoning={
            "effort": "none",
        },
    )

    # -----------------------------------------------------------------
    # Extract text
    # -----------------------------------------------------------------

    output_text = response.output_text

    if not isinstance(output_text, str) or not output_text.strip():
        raise RuntimeError(
            "LLM response did not contain text output"
        )

    # -----------------------------------------------------------------
    # Token usage logging
    # -----------------------------------------------------------------

    usage = getattr(response, "usage", None)

    if usage_log is not None and usage is not None:
        usage_log.append(
            {
                **dict(usage_context or {}),
                "model": model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            }
        )

    return output_text