"""Injected Gemini AL callable; one retry owner and no SDK construction."""

from __future__ import annotations

from typing import Any, Callable

from ActiveLearning.inverse_design.llm_client import (
    _messages, _provider_settings, _response_text, _usage_counts,
)

from .gemini_recovery import GeminiRecovery


def build_gemini_chat_transport(*, model: str, api_key: str | None = None,
                                base_url: str | None = None) -> Callable[[Any, float], Any]:
    """Build a transport only; the V2 scheduler still has no real execute path."""
    selected, key, chosen_model, url = _provider_settings(
        "gemini", api_key, model, base_url)
    if selected != "gemini" or chosen_model != model:
        raise ValueError("Gemini transport model mismatch")
    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError("openai package is required for Gemini transport") from error
    # GeminiRecovery is the sole application-level retry controller.
    client = OpenAI(api_key=key, base_url=url, max_retries=0)

    def send(messages: Any, timeout_seconds: float) -> Any:
        if timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        return client.chat.completions.create(
            model=chosen_model, messages=messages, timeout=timeout_seconds)

    return send


class GeminiALClient:
    def __init__(self, recovery: GeminiRecovery, send: Callable[[Any, float], Any], model: str):
        if not isinstance(recovery, GeminiRecovery) or not callable(send):
            raise TypeError("injected recovery and transport are required")
        if recovery.scope.split("/")[1] != model:
            raise ValueError("Gemini model differs from recovery scope")
        self.recovery, self.send, self.model = recovery, send, model

    def __call__(self, prompt, usage_log=None, usage_context=None) -> str:
        messages = _messages(prompt)
        response = self.recovery.call(messages, self.send)
        text = _response_text(response, "gemini")
        counts = _usage_counts(response, "gemini")
        if usage_log is not None and counts is not None:
            usage_log.append({**dict(usage_context or {}), "provider": "gemini",
                              "model": self.model, **counts})
        return text
