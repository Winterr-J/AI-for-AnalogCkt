"""Small enums shared by Stage B v1 modules."""

from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class DomainType(StringEnum):
    CONTINUOUS = "continuous"
    INTEGER = "integer"
    CATEGORICAL = "categorical"
    UNSUPPORTED = "unsupported"


class RangeStatus(StringEnum):
    VALID = "VALID"
    UNCERTAIN = "UNCERTAIN"
    CONFLICT = "CONFLICT"


class RelationStatus(StringEnum):
    VALID = "VALID"
    UNCERTAIN = "UNCERTAIN"
    CONFLICT = "CONFLICT"


class Severity(StringEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
