"""Price-momentum signal from CMC percent-change fields.

Pure math: a weighted blend of 1h/24h/7d percent changes, normalized and clamped
to [-1, 1]. No network, no external text — this signal cannot be injection-attacked.
"""
from __future__ import annotations

# Weights favor the 24h trend, with 7d for regime and 1h for freshness.
_W_1H, _W_24H, _W_7D = 0.2, 0.5, 0.3
# Divisor mapping weighted percent move -> unit range before clamping.
_SCALE = 10.0


def _num(x: float | None) -> float:
    return float(x) if isinstance(x, (int, float)) else 0.0


def compute_momentum(pct_1h: float | None, pct_24h: float | None, pct_7d: float | None) -> float:
    """Return a momentum score in [-1, 1]. Missing inputs are treated as 0."""
    raw = _W_1H * _num(pct_1h) + _W_24H * _num(pct_24h) + _W_7D * _num(pct_7d)
    return max(-1.0, min(1.0, raw / _SCALE))
