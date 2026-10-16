"""Render structured inverse-design inputs into deterministic prompts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_PROMPT_DIR = Path(__file__).parent / "prompts"
_GENERIC_PROMPT_PATH = _PROMPT_DIR / "generic_inverse_design_v1.txt"
_CIRCUIT_TEMPLATE_FILES = {
    "currentmirror": "current_mirror_v1.txt",
    "opamp": "opamp_v1.txt",
}
_STRUCTURED_SECTIONS = (
    "circuit_info",
    "parameter_space",
    "target_specs",
    "best_known_anchor",
    "context_samples",
)


def _load_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _get_circuit_template(circuit_type: Any) -> str:
    normalized = "".join(
        character for character in str(circuit_type).casefold() if character.isalnum()
    )
    filename = _CIRCUIT_TEMPLATE_FILES.get(normalized)
    if filename is None:
        raise ValueError(
            f"Unsupported circuit type for inverse-design prompt: {circuit_type!r}"
        )
    return _load_template(_PROMPT_DIR / "circuit_types" / filename)


def build_inverse_design_prompt(inverse_input: Mapping[str, Any]) -> str:
    """Serialize structured input and append its circuit-specific guidance."""

    generic_prompt = _load_template(_GENERIC_PROMPT_PATH)
    for section in _STRUCTURED_SECTIONS:
        serialized = json.dumps(
            inverse_input[section],
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        generic_prompt = generic_prompt.replace(f"{{{section}}}", serialized)

    circuit_type = inverse_input["circuit_info"]["circuit_type"]
    circuit_prompt = _get_circuit_template(circuit_type)
    return generic_prompt.rstrip() + "\n\n" + circuit_prompt.strip() + "\n"
