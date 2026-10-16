"""Generic Stage B selection, domain, and canonicalization logic."""

from .canonicalizer import canonicalize_variables
from .domain_classifier import DomainClassification, classify_domain
from .variable_selector import SelectionResult, select_optimization_targets

__all__ = [
    "DomainClassification",
    "SelectionResult",
    "canonicalize_variables",
    "classify_domain",
    "select_optimization_targets",
]
