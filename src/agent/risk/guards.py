"""Numeric input guards shared by the risk layer.

Prices and balances originate from external sources (on-chain, CMC) and are
therefore untrusted. The risk layer fails safe on garbage input rather than
producing a position from a NaN/negative value.
"""
from __future__ import annotations

import math


def is_bad_number(x: object) -> bool:
    """True if x is None, not a real number, NaN, infinite, or negative."""
    if not isinstance(x, (int, float)):
        return True
    return math.isnan(x) or math.isinf(x) or x < 0


def require_finite_nonneg(x: object, name: str) -> float:
    """Return x as float, or raise ValueError if it is not finite and >= 0."""
    if is_bad_number(x):
        raise ValueError(f"{name} must be a finite, non-negative number (got {x!r})")
    return float(x)  # type: ignore[arg-type]
