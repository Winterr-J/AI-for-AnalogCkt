import os
import sys
import types
from unittest.mock import patch

import pytest

from ActiveLearning.inverse_design.llm_client import (
    call_inverse_design_llm,
    _response_text,
    _usage_counts,
)


@pytest.mark.parametrize(
    "transport,expected",
    [
        ("chat_completions", "chat"),
        ("responses", "responses"),
    ],
)
def test_openai_transport_selection(transport, expected):
    calls = []

    chat_response = types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content='{"ok":true}'),
                finish_reason="stop",
            )
        ],
        usage=types.SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        ),
    )

    responses_response = types.SimpleNamespace(
        output_text='{"ok":true}',
        usage=types.SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        ),
    )

    def chat_create(**kwargs):
        calls.append("chat")
        return chat_response

    def responses_create(**kwargs):
        calls.append("responses")
        return responses_response

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda **kwargs: types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=chat_create)
        ),
        responses=types.SimpleNamespace(create=responses_create),
    )

    with patch.dict(sys.modules, {"openai": fake_openai}):
        with patch.dict(os.environ, {"AL_OPENAI_TRANSPORT": transport}):
            result = call_inverse_design_llm(
                "Return JSON",
                provider="openai",
                model="gpt-6.1-sol",
                api_key="mock-key",
                base_url="https://example.invalid/v1",
                max_attempts=1,
            )

    assert result == '{"ok":true}'
    assert calls == [expected]

    response = chat_response if expected == "chat" else responses_response
    parser = "chat_completions" if expected == "chat" else "openai"

    assert _response_text(response, parser) == '{"ok":true}'
    assert _usage_counts(response, parser) == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
