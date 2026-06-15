"""Contest-aware PnL accounting.

The contest measures return hourly and rules that if the wallet value at the
start of an hour is <= $1, that hour contributes 0% (a dead-wallet guard). These
helpers encode that rule so reporting matches how we are actually scored.
"""
from __future__ import annotations

DEAD_WALLET_FLOOR_USD = 1.0


def hourly_return(prev_equity: float, curr_equity: float) -> float:
    """Fractional return for one hour; 0.0 if the hour started at <= $1."""
    if prev_equity <= DEAD_WALLET_FLOOR_USD:
        return 0.0
    return (curr_equity - prev_equity) / prev_equity


def cumulative_return(start_equity: float, curr_equity: float) -> float:
    if start_equity <= 0:
        return 0.0
    return (curr_equity - start_equity) / start_equity
