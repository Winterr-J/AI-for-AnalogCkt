"""Stage A optimization-variable identification package.

Milestones 1-3 contain static parsing, Circuit IR, canonical physical targets,
binding facts, safe equality relations, and candidate validity/scope rules.
Semantic decision modules are intentionally absent.
"""

from .parser.pyspice_parser import PySpiceParser, parse_pyspice_file, parse_pyspice_source
from .binding.binding_resolver import BindingResolver, resolve_bindings
from .validity.candidate_validator import CandidateValidator, validate_candidates

__all__ = [
    "BindingResolver",
    "CandidateValidator",
    "PySpiceParser",
    "parse_pyspice_file",
    "parse_pyspice_source",
    "resolve_bindings",
    "validate_candidates",
]
