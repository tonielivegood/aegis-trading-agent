"""Shared strategy types: the order the strategy emits and the portfolio
snapshot it reads. Both are plain data — no behavior, no execution coupling.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TradeOrder:
    token_in: str           # symbol we spend
    token_out: str          # symbol we receive
    amount_in_usd: float    # USD value of token_in to spend
    reason: str = ""


@dataclass
class PortfolioState:
    equity_usd: float
    risk_value_usd: float                       # non-stable holdings, USD
    stable_value_usd: float
    token_values_usd: dict[str, float] = field(default_factory=dict)  # symbol -> USD held
    drawdown_tripped: bool = False
    cap_breached: bool = False
