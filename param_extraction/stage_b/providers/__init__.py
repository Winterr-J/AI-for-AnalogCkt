"""Range-provider interfaces for Stage B."""

from .base import RangeProvider
from .ideal import IdealRangeProvider

__all__ = ["IdealRangeProvider", "RangeProvider"]
