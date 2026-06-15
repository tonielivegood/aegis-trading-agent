"""Defensive rebalancing — converts risk assets to stablecoin.

Invoked by the safeguard when the drawdown breaker trips or equity nears the
floor. Selling to USDT (rather than to zero) keeps capital deployed so the
hourly PnL never reads as a dead wallet.
"""
from __future__ import annotations

from ..data.token_list import STABLECOINS
from .base_strategy import PortfolioState, TradeOrder

MIN_ORDER_USD = 2.0
STABLE = "USDT"


def derisk_orders(state: PortfolioState) -> list[TradeOrder]:
    orders: list[TradeOrder] = []
    for symbol, value_usd in state.token_values_usd.items():
        if symbol in STABLECOINS:
            continue
        if value_usd >= MIN_ORDER_USD:
            orders.append(TradeOrder(symbol, STABLE, value_usd, "derisk to stable"))
    return orders
