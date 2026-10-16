"""Explicit, topology-backed relation extraction."""

from .explicit_relations import (
    ParsedExplicitRelation,
    analyze_explicit_relation,
    extract_relation_evidence,
)

__all__ = [
    "ParsedExplicitRelation",
    "analyze_explicit_relation",
    "extract_relation_evidence",
]
