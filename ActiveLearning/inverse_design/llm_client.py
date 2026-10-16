"""Small OpenAI Responses / Gemini Chat Completions LLM adapter."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from typing import Any


_API_KEY_PLACEHOLDER = "YOUR_API_KEY_HERE"
_MODEL_PLACEHOLDER = "YOUR_MODEL_NAME_HERE"
_BASE_URL_PLACEHOLDER = "YOUR_BASE_URL_HERE"
_GEMINI_MODEL = "gemini-3.5-flash-lite"
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Retain these exports for existing callers. Provider settings are resolved
# at call time so Gemini never inherits an OpenAI key or model by accident.
LLM_API_KEY = os.getenv("OPENAI_API_KEY", _API_KEY_PLACEHOLDER)
LLM_MODEL = os.getenv("OPENAI_MODEL", _MODEL_PLACEHOLDER)
LLM_BASE_URL = os.getenv("OPENAI_BASE_URL", _BASE_URL_PLACEHOLDER)


class LLMQuotaExceededError(RuntimeError):
    """A provider explicitly reported exhausted quota or billing allowance."""


class LLMTransientError(RuntimeError):
    """A transient provider failure persisted through bounded retries."""


def _value(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _provider_settings(
    provider: str | None,
    api_key: str | None,
    model: str | None,
    base_url: str | None,
) -> tuple[str, str, str, str]:
    selected = provider if provider is not None else os.getenv("AL_LLM_PROVIDER", "openai")
    selected = selected.strip().lower()
    if selected not in {"openai", "gemini"}:
        raise ValueError(f"Unsupported LLM provider: {selected!r}")

    if selected == "gemini":
        key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY")
        chosen_model = model if model is not None else os.getenv("GEMINI_MODEL", _GEMINI_MODEL)
        url = base_url if base_url is not None else os.getenv("GEMINI_BASE_URL", _GEMINI_BASE_URL)
    else:
        key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", LLM_API_KEY)
        chosen_model = model if model is not None else os.getenv("OPENAI_MODEL", LLM_MODEL)
        url = base_url if base_url is not None else os.getenv("OPENAI_BASE_URL", LLM_BASE_URL)

    if (
        not key or key == _API_KEY_PLACEHOLDER
        or not chosen_model or chosen_model == _MODEL_PLACEHOLDER
        or not url or url == _BASE_URL_PLACEHOLDER
    ):
        raise ValueError(f"LLM API configuration has not been configured for {selected}")
    return selected, key, chosen_model, url


def _messages(prompt: str | Mapping[str, str] | Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    """Preserve current string prompts and allow explicit system/user turns."""
    if isinstance(prompt, str):
        items = [{"role": "user", "content": prompt}]
    elif isinstance(prompt, Mapping):
        if set(prompt) - {"system", "user"} or "user" not in prompt:
            raise ValueError("LLM prompt mapping must contain user and optional system text")
        items = [
            {"role": role, "content": prompt[role]}
            for role in ("system", "user") if role in prompt
        ]
    elif isinstance(prompt, Sequence):
        items = list(prompt)
    else:
        raise TypeError("LLM prompt must be text or system/user messages")

    messages = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("LLM messages must be role/content mappings")
        role, content = item.get("role"), item.get("content")
        if role not in {"system", "user"} or not isinstance(content, str) or not content.strip():
            raise ValueError("LLM messages require nonempty system/user text")
        messages.append({"role": role, "content": content})
    if not messages or not any(item["role"] == "user" for item in messages):
        raise ValueError("LLM messages require a user turn")
    return messages


def _response_text(response: Any, provider: str) -> str:
    if provider == "openai":
        result = _value(response, "output_text")
    else:
        choices = _value(response, "choices")
        message = _value(choices[0], "message") if choices else None
        content = _value(message, "content")
        if isinstance(content, str):
            result = content
        elif isinstance(content, list):
            result = "".join(
                part for item in content
                if isinstance(part := _value(item, "text"), str)
            )
        else:
            result = None
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("LLM response did not contain text output")
    return result


def _usage_counts(response: Any, provider: str) -> dict[str, int] | None:
    usage = _value(response, "usage")
    if usage is None:
        return None
    if provider == "gemini":
        input_count = _value(usage, "prompt_tokens")
        output_count = _value(usage, "completion_tokens")
    else:
        input_count = _value(usage, "input_tokens")
        output_count = _value(usage, "output_tokens")
    total_count = _value(usage, "total_tokens")
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in (input_count, output_count)):
        return None
    if total_count is None:
        total_count = input_count + output_count
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count < 0:
        return None
    return {
        "input_tokens": input_count,
        "output_tokens": output_count,
        "total_tokens": total_count,
    }


def _failure_kind(error: Exception) -> str | None:
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    detail = _value(body, "error") or body
    code = str(getattr(error, "code", "") or _value(detail, "code") or "").lower()
    message = str(_value(detail, "message") or "").lower()
    if status == 429:
        if code in {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached"} or any(
            phrase in message for phrase in ("daily quota", "quota exhausted", "billing limit")
        ):
            return "quota"
        return "rate_limit"
    if status in {500, 502, 503, 504}:
        return "service_unavailable"
    if isinstance(error, TimeoutError) or type(error).__name__ in {
        "APITimeoutError", "APIConnectionError",
    }:
        return "timeout"
    return None


def call_inverse_design_llm(
    prompt: str | Mapping[str, str] | Sequence[Mapping[str, str]],
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    usage_log=None,
    usage_context=None,
    *,
    provider: str | None = None,
    request_timeout: float = 30.0,
    max_attempts: int = 3,
) -> str:
    """Return raw text for the existing inverse-design/prediction JSON parsers."""
    selected, key, chosen_model, url = _provider_settings(provider, api_key, model, base_url)
    if request_timeout <= 0 or max_attempts < 1:
        raise ValueError("request_timeout and max_attempts must be positive")
    messages = _messages(prompt)

    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError("The openai package is required for configured LLM calls") from error

    # Disable SDK retries: this adapter owns the bounded retry/backoff policy.
    client = OpenAI(api_key=key, base_url=url, timeout=request_timeout, max_retries=0)
    for attempt in range(1, max_attempts + 1):
        try:
            if selected == "gemini":
                response = client.chat.completions.create(model=chosen_model, messages=messages)
            else:
                response = client.responses.create(
                    model=chosen_model,
                    input=prompt if isinstance(prompt, str) else messages,
                    reasoning={"effort": "none"},
                )
        except Exception as error:
            kind = _failure_kind(error)
            if kind == "quota":
                raise LLMQuotaExceededError(
                    f"{selected} quota is exhausted for model {chosen_model}; no retry attempted"
                ) from error
            if kind is None:
                raise
            if attempt == max_attempts:
                raise LLMTransientError(
                    f"{selected} {kind} for model {chosen_model} after {attempt} attempts"
                ) from error
            time.sleep(min(2 ** (attempt - 1), 8))
            continue

        output_text = _response_text(response, selected)
        counts = _usage_counts(response, selected)
        if usage_log is not None and counts is not None:
            usage_log.append({
                **dict(usage_context or {}),
                "provider": selected,
                "model": chosen_model,
                **counts,
            })
        return output_text

    raise AssertionError("unreachable LLM retry state")
