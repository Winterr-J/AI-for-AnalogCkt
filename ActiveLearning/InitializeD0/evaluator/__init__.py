"""SPICE evaluators used by Active Learning D0 initialization."""

from .current_mirror_evaluator import evaluate_current_mirror

__all__ = [
    "OpAmpEvaluationResult",
    "OpAmpEvaluator",
    "OpAmpMetrics",
    "evaluate_current_mirror",
    "evaluate_opamp_mode2",
]


def __getattr__(name):
    """Load OpAmp evaluator symbols lazily to avoid onboarding import cycles."""

    if name in {
        "OpAmpEvaluationResult",
        "OpAmpEvaluator",
        "OpAmpMetrics",
        "evaluate_opamp_mode2",
    }:
        from . import opamp_evaluator

        return getattr(opamp_evaluator, name)
    raise AttributeError(name)
