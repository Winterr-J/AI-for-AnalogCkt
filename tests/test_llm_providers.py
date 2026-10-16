"""Provider adapter tests with no external API calls or credentials."""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ActiveLearning.inverse_design import inverse_designer
from ActiveLearning.inverse_design.llm_client import (
    LLMQuotaExceededError,
    LLMTransientError,
    call_inverse_design_llm,
)
from ActiveLearning.performance_prediction.performance_predictor import _parse_predictions


class FakeHTTPError(Exception):
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self.body = body


def _fake_openai(response=None, side_effect=None):
    responses_create = MagicMock(return_value=response, side_effect=side_effect)
    chat_create = MagicMock(return_value=response, side_effect=side_effect)
    client = SimpleNamespace(
        responses=SimpleNamespace(create=responses_create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
    )
    constructor = MagicMock(return_value=client)
    return SimpleNamespace(OpenAI=constructor), constructor, responses_create, chat_create


def _gemini_response(text='{"x_seed": {"A": 1.0}}', total=13):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, total_tokens=total),
    )


class LLMProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {
            "GEMINI_MODEL": "gemini-3.5-flash-lite",
            "GEMINI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_gemini_env_uses_chat_completions_and_existing_json_parser(self) -> None:
        fake, constructor, responses_create, chat_create = _fake_openai(_gemini_response())
        usage_log = []
        with (
            patch.dict(os.environ, {
                "AL_LLM_PROVIDER": "gemini",
                "GEMINI_API_KEY": "mock-only-key",
                "GEMINI_MODEL": "gemini-3.5-flash-lite",
            }),
            patch.dict(sys.modules, {"openai": fake}),
        ):
            text = call_inverse_design_llm(
                "PROMPT", usage_log=usage_log,
                usage_context={"iteration": 1, "stage": "inverse_design"},
            )
        self.assertEqual(inverse_designer._parse_x_seed(text), {"A": 1.0})
        constructor.assert_called_once_with(
            api_key="mock-only-key",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            timeout=30.0,
            max_retries=0,
        )
        chat_create.assert_called_once_with(
            model="gemini-3.5-flash-lite",
            messages=[{"role": "user", "content": "PROMPT"}],
        )
        responses_create.assert_not_called()
        self.assertEqual(usage_log, [{
            "iteration": 1, "stage": "inverse_design", "provider": "gemini",
            "model": "gemini-3.5-flash-lite", "input_tokens": 10,
            "output_tokens": 3, "total_tokens": 13,
        }])

    def test_system_user_messages_and_content_parts(self) -> None:
        response = _gemini_response()
        response.choices[0].message.content = [
            {"type": "text", "text": '{"x_seed": '},
            {"type": "text", "text": '{"A": 2.0}}'},
        ]
        fake, _, _, chat_create = _fake_openai(response)
        with patch.dict(sys.modules, {"openai": fake}):
            text = call_inverse_design_llm(
                {"system": "Return JSON", "user": "Propose a seed"},
                provider="gemini", api_key="mock-key",
            )
        self.assertEqual(inverse_designer._parse_x_seed(text), {"A": 2.0})
        self.assertEqual(chat_create.call_args.kwargs["messages"], [
            {"role": "system", "content": "Return JSON"},
            {"role": "user", "content": "Propose a seed"},
        ])

    def test_gemini_prediction_json_and_total_is_not_double_counted(self) -> None:
        payload = json.dumps({"predictions": [{
            "candidate_id": "c1", "predicted_metrics": {"current_error": 0.03},
        }]})
        fake, _, _, _ = _fake_openai(_gemini_response(payload, total=15))
        usage_log = []
        with patch.dict(sys.modules, {"openai": fake}):
            text = call_inverse_design_llm(
                "PREDICTION PROMPT", provider="gemini", api_key="mock-key",
                usage_log=usage_log,
            )
        self.assertEqual(
            _parse_predictions(text, ["c1"], ["current_error"]),
            {"c1": {"current_error": 0.03}},
        )
        self.assertEqual(usage_log[0]["total_tokens"], 15)
        self.assertEqual(len(usage_log), 1)

    def test_missing_total_uses_input_plus_output_once(self) -> None:
        response = _gemini_response()
        response.usage.total_tokens = None
        fake, _, _, _ = _fake_openai(response)
        usage_log = []
        with patch.dict(sys.modules, {"openai": fake}):
            call_inverse_design_llm("PROMPT", provider="gemini", api_key="mock-key", usage_log=usage_log)
        self.assertEqual(usage_log[0]["total_tokens"], 13)

    def test_missing_key_and_unknown_provider_fail_before_client_creation(self) -> None:
        fake, constructor, _, _ = _fake_openai(_gemini_response())
        with (
            patch.dict(os.environ, {"AL_LLM_PROVIDER": "gemini"}, clear=True),
            patch.dict(sys.modules, {"openai": fake}),
        ):
            with self.assertRaisesRegex(ValueError, "not been configured for gemini"):
                call_inverse_design_llm("PROMPT")
            with self.assertRaisesRegex(ValueError, "Unsupported LLM provider"):
                call_inverse_design_llm("PROMPT", provider="unknown", api_key="mock-key")
        constructor.assert_not_called()

    def test_malformed_response_is_not_logged_or_retried(self) -> None:
        response = _gemini_response()
        response.choices = []
        fake, _, _, chat_create = _fake_openai(response)
        usage_log = []
        with patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaisesRegex(RuntimeError, "did not contain text"):
                call_inverse_design_llm(
                    "PROMPT", provider="gemini", api_key="mock-key", usage_log=usage_log,
                )
        self.assertEqual(chat_create.call_count, 1)
        self.assertEqual(usage_log, [])

    def test_temporary_429_retries_same_provider_and_model(self) -> None:
        fake, _, responses_create, chat_create = _fake_openai(
            side_effect=[FakeHTTPError(429), _gemini_response()]
        )
        usage_log = []
        with (patch.dict(sys.modules, {"openai": fake}), patch(
            "ActiveLearning.inverse_design.llm_client.time.sleep"
        ) as sleep):
            text = call_inverse_design_llm(
                "PROMPT", provider="gemini", api_key="mock-key", usage_log=usage_log,
            )
        self.assertIn("x_seed", text)
        self.assertEqual(chat_create.call_count, 2)
        self.assertEqual(
            [call.kwargs["model"] for call in chat_create.call_args_list],
            ["gemini-3.5-flash-lite"] * 2,
        )
        responses_create.assert_not_called()
        sleep.assert_called_once_with(1)
        self.assertEqual(len(usage_log), 1)

    def test_503_and_timeout_have_bounded_exponential_retry(self) -> None:
        for error in (FakeHTTPError(503), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                fake, _, _, chat_create = _fake_openai(side_effect=error)
                with (patch.dict(sys.modules, {"openai": fake}), patch(
                    "ActiveLearning.inverse_design.llm_client.time.sleep"
                ) as sleep):
                    with self.assertRaises(LLMTransientError):
                        call_inverse_design_llm(
                            "PROMPT", provider="gemini", api_key="mock-key",
                            max_attempts=3,
                        )
                self.assertEqual(chat_create.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_exhausted_quota_is_not_treated_as_temporary_rate_limit(self) -> None:
        fake, _, responses_create, chat_create = _fake_openai(
            side_effect=FakeHTTPError(429, {"error": {"code": "insufficient_quota"}})
        )
        with (patch.dict(sys.modules, {"openai": fake}), patch(
            "ActiveLearning.inverse_design.llm_client.time.sleep"
        ) as sleep):
            with self.assertRaises(LLMQuotaExceededError):
                call_inverse_design_llm("PROMPT", provider="gemini", api_key="mock-key")
        self.assertEqual(chat_create.call_count, 1)
        responses_create.assert_not_called()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
