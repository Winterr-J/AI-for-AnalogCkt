"""Runtime semantic-role classification over Stage A Circuit IR.

This package is intentionally independent of Development/Held-out Gold.
"""

from .semantic_classifier import SemanticClassifier, classify_semantics
from .semantic_result import SemanticPrediction

__all__ = ["SemanticClassifier", "SemanticPrediction", "classify_semantics"]
