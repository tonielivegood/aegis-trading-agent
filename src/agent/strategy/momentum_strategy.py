"""Momentum rotation strategy.

Buys the strongest-momentum core tokens with small, risk-capped sizes; sells
held tokens that flip to a SELL signal. Every buy is sized through PositionSizer
so the per-token cap and stablecoin floor are structurally guaranteed. Emits no
new buys once the drawdown breaker has tripped — capital preservation first.
"""
from __future__ import annotations

from ..config import settings
from ..risk.position_sizer import PositionSizer
from ..signal.signal_schema import SignalBundle
from .base_strategy import PortfolioState, TradeOrder

MAX_NEW_POSITIONS = 3
MIN_ORDER_USD = 2.0
STABLE = "USDT"


def decide(signals: list[SignalBundle], state: PortfolioState) -> list[TradeOrder]:
    # Capital-preservation gate: no fresh risk while tripped/breached.
    if state.drawdown_tripped or state.cap_breached:
        return []

    orders: list[TradeOrder] = []
    sizer = PositionSizer(state.equity_usd, settings.max_position_pct, settings.stablecoin_floor_pct)
    running_risk = state.risk_value_usd

    buys = sorted([s for s in signals if s.direction == "BUY"], key=lambda s: -s.combined_score)
    for s in buys[:MAX_NEW_POSITIONS]:
        held = state.token_values_usd.get(s.symbol, 0.0)
        size = sizer.size_for(held, running_risk)
        if size >= MIN_ORDER_USD:
            orders.append(TradeOrder(STABLE, s.symbol, size, f"momentum buy {s.combined_score:.2f}"))
            running_risk += size

    for s in signals:
        if s.direction == "SELL":
            held = state.token_values_usd.get(s.symbol, 0.0)
            if held >= MIN_ORDER_USD:
                orders.append(TradeOrder(s.symbol, STABLE, held, f"momentum sell {s.combined_score:.2f}"))

    return orders
