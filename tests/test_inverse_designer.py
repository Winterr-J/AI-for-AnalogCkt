from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ActiveLearning.inverse_design import call_inverse_design_llm
from ActiveLearning.inverse_design import inverse_designer
from ActiveLearning.inverse_design import propose_inverse_design_seed


_TARGETS = {"error": {"operator": "<=", "value": 0.1}}
_CIRCUIT_INFO = {"circuit_type": "CurrentMirror", "circuit_id": "test"}
_PARAMETER_TEMPLATE = {
    "optimization_space": {"variables": ["A", "B"]},
    "parameters": [
        {
            "name": name,
            "variable_type": "continuous",
            "nominal_value": 1.0,
            "admissible_range": {"lower": 0.0, "upper": 10.0},
        }
        for name in ("A", "B")
    ],
    "bindings": [],
    "constraints": [],
}
_ANCHOR = {
    "sample_id": "selected",
    "params": {"A": 1.0, "B": 2.0},
    "measured_metrics": {"error": 0.05},
    "joint_violation": 0.0,
}


class InverseDesignerTests(unittest.TestCase):
    def test_full_pipeline_calls_existing_modules_and_returns_validation(self) -> None:
        records = [{"sample_id": "historical"}]
        selected = [{"sample_id": "selected"}]
        context_diagnostics = [
            {
                "sample_id": "selected",
                "params": {"A": 1.0, "B": 2.0},
                "metrics": {"error": 0.05},
                "relevance": 1.0,
            }
        ]
        inverse_input = {"structured": "input"}
        x_seed = {"A": 2.0, "B": 3.0}
        validation = {"parameter_valid": True, "violations": []}

        with (
            patch.object(
                inverse_designer,
                "select_inverse_design_context",
                return_value=(selected, context_diagnostics),
            ) as selector,
            patch.object(
                inverse_designer,
                "select_best_known_anchor",
                return_value=_ANCHOR,
            ) as anchor_selector,
            patch.object(
                inverse_designer,
                "build_inverse_design_input",
                return_value=inverse_input,
            ) as input_builder,
            patch.object(
                inverse_designer,
                "build_inverse_design_prompt",
                return_value="PROMPT",
            ) as prompt_builder,
            patch.object(
                inverse_designer,
                "call_inverse_design_llm",
                return_value=json.dumps({"x_seed": x_seed}),
            ) as llm,
            patch.object(
                inverse_designer,
                "validate_parameters",
                return_value=validation,
            ) as validator,
        ):
            result = propose_inverse_design_seed(
                records,
                _CIRCUIT_INFO,
                _PARAMETER_TEMPLATE,
                _TARGETS,
                context_k=4,
                mmr_lambda=0.55,
                api_key="configured-key",
                model="configured-model",
            )

        expected = {
                "x_seed": x_seed,
                "inverse_context": context_diagnostics,
                "best_known_anchor": _ANCHOR,
                "x_seed_vs_anchor": {
                    "A": {
                        "seed_value": 2.0,
                        "anchor_value": 1.0,
                        "magnitude_ratio_or_relative_ratio": 2.0,
                    },
                    "B": {
                        "seed_value": 3.0,
                        "anchor_value": 2.0,
                        "magnitude_ratio_or_relative_ratio": 1.5,
                    },
                },
                "x_seed_anchor_distance": (0.1 ** 2 + 0.1 ** 2) ** 0.5,
                **validation,
            }
        expected_distance = expected.pop("x_seed_anchor_distance")
        actual_distance = result.pop("x_seed_anchor_distance")
        self.assertEqual(result, expected)
        self.assertAlmostEqual(actual_distance, expected_distance)
        selector.assert_called_once_with(
            records,
            _TARGETS,
            k=4,
            lambda_weight=0.55,
            parameter_template=_PARAMETER_TEMPLATE,
            return_diagnostics=True,
        )
        anchor_selector.assert_called_once_with(records, _TARGETS)
        input_builder.assert_called_once_with(
            context_samples=selected,
            circuit_info=_CIRCUIT_INFO,
            parameter_template=_PARAMETER_TEMPLATE,
            target_specs=_TARGETS,
            best_known_anchor=_ANCHOR,
        )
        prompt_builder.assert_called_once_with(inverse_input)
        llm.assert_called_once_with(
            "PROMPT",
            api_key="configured-key",
            model="configured-model",
        )
        validator.assert_called_once_with(x_seed, _PARAMETER_TEMPLATE)

    def test_target_specs_are_required_before_selection_or_llm(self) -> None:
        for target_specs in (None, {}):
            with self.subTest(target_specs=target_specs):
                with (
                    patch.object(inverse_designer, "select_inverse_design_context") as selector,
                    patch.object(inverse_designer, "call_inverse_design_llm") as llm,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "target_specs are required for inverse design",
                    ):
                        propose_inverse_design_seed(
                            [],
                            _CIRCUIT_INFO,
                            _PARAMETER_TEMPLATE,
                            target_specs,
                        )
                selector.assert_not_called()
                llm.assert_not_called()

    def test_json_parser_accepts_simple_markdown_fence(self) -> None:
        response = '```json\n{"x_seed": {"A": 2.0, "B": 3.0}}\n```'

        self.assertEqual(
            inverse_designer._parse_x_seed(response),
            {"A": 2.0, "B": 3.0},
        )

    def test_json_parser_rejects_malformed_or_wrong_shapes(self) -> None:
        cases = (
            ("not json", json.JSONDecodeError),
            ("[]", ValueError),
            ('{"candidate": {}}', ValueError),
            ('{"x_seed": 1}', ValueError),
        )
        for response, error_type in cases:
            with self.subTest(response=response):
                with self.assertRaises(error_type):
                    inverse_designer._parse_x_seed(response)

    def test_parser_does_not_clamp_fill_or_change_values(self) -> None:
        response = '{"x_seed": {"A": -99.0}}'

        self.assertEqual(inverse_designer._parse_x_seed(response), {"A": -99.0})

    def test_invalid_seed_is_returned_with_validator_violations(self) -> None:
        selected = [{"sample_id": "selected"}]
        raw_seed = {"A": -99.0}
        validation = {
            "parameter_valid": False,
            "violations": ["A=-99.0 is outside admissible range"],
        }
        with (
            patch.object(
                inverse_designer,
                "select_inverse_design_context",
                return_value=(selected, [{"relevance": 0.5}]),
            ),
            patch.object(
                inverse_designer,
                "select_best_known_anchor",
                return_value=_ANCHOR,
            ),
            patch.object(
                inverse_designer,
                "build_inverse_design_input",
                return_value={"input": True},
            ),
            patch.object(
                inverse_designer,
                "build_inverse_design_prompt",
                return_value="PROMPT",
            ),
            patch.object(
                inverse_designer,
                "call_inverse_design_llm",
                return_value=json.dumps({"x_seed": raw_seed}),
            ),
            patch.object(
                inverse_designer,
                "validate_parameters",
                return_value=validation,
            ),
        ):
            result = propose_inverse_design_seed(
                [],
                _CIRCUIT_INFO,
                _PARAMETER_TEMPLATE,
                _TARGETS,
            )

        self.assertEqual(result["x_seed"], raw_seed)
        self.assertFalse(result["parameter_valid"])
        self.assertTrue(result["violations"])

    def test_placeholder_client_fails_before_any_real_api_call(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "LLM API configuration has not been configured",
        ):
            call_inverse_design_llm(
                "PROMPT",
                api_key="YOUR_API_KEY_HERE",
                model="YOUR_MODEL_NAME_HERE",
            )

    def test_injected_client_is_used_without_default_api_client(self) -> None:
        injected = MagicMock(return_value='{"x_seed": {"A": 2.0, "B": 3.0}}')
        with (
            patch.object(
                inverse_designer,
                "select_inverse_design_context",
                return_value=([{"sample_id": "selected"}], [{"relevance": 0.5}]),
            ),
            patch.object(
                inverse_designer,
                "select_best_known_anchor",
                return_value=_ANCHOR,
            ),
            patch.object(
                inverse_designer,
                "build_inverse_design_input",
                return_value={"input": True},
            ),
            patch.object(
                inverse_designer,
                "build_inverse_design_prompt",
                return_value="PROMPT",
            ),
            patch.object(inverse_designer, "call_inverse_design_llm") as default_client,
            patch.object(
                inverse_designer,
                "validate_parameters",
                return_value={"parameter_valid": True, "violations": []},
            ),
        ):
            result = propose_inverse_design_seed(
                [],
                _CIRCUIT_INFO,
                _PARAMETER_TEMPLATE,
                _TARGETS,
                llm_client=injected,
            )

        self.assertEqual(result["x_seed"], {"A": 2.0, "B": 3.0})
        injected.assert_called_once_with("PROMPT")
        default_client.assert_not_called()

    def test_configured_client_uses_mocked_responses_api(self) -> None:
        create = MagicMock(
            return_value=SimpleNamespace(
                output_text='{"x_seed": {"A": 1.0}}',
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=120,
                ),
            )
        )
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        openai_class = MagicMock(return_value=client)
        fake_openai = SimpleNamespace(OpenAI=openai_class)

        usage_log = []
        with patch.dict(sys.modules, {"openai": fake_openai}):
            result = call_inverse_design_llm(
                "PROMPT",
                api_key="configured-key",
                model="configured-model",
                base_url="https://example.test/v1",
                usage_log=usage_log,
                usage_context={
                    "iteration": 1,
                    "stage": "inverse_design",
                    "round": 1,
                },
            )

        self.assertEqual(result, '{"x_seed": {"A": 1.0}}')
        openai_class.assert_called_once_with(
            api_key="configured-key",
            base_url="https://example.test/v1",
            timeout=30.0,
            max_retries=0,
        )
        create.assert_called_once_with(
            model="configured-model",
            input="PROMPT",
            reasoning={"effort": "none"},
        )
        self.assertEqual(
            usage_log,
            [
                {
                    "iteration": 1,
                    "stage": "inverse_design",
                    "round": 1,
                    "provider": "openai",
                    "model": "configured-model",
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
