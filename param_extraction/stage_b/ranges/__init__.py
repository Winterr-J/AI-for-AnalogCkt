"""Range validation, propagation, and intersection for Stage B."""

from .propagation import PropagationResult, construct_and_propagate_ranges

__all__ = ["PropagationResult", "construct_and_propagate_ranges"]
