"""Layer A — baseline eligible-basket fallback.

Used only when the event radar finds no high-confidence catalyst. Spreads a
small, capped slice of equity equally across the most-liquid eligible tokens,
keeps a configurable stablecoin floor, and defers to the derisk path when the
drawdown breaker trips. Diversification is the whole point: when we can't pick a
winner, don't concentrate — and never let one rug breach the DQ gate.

On small capital, the basket is trimmed to as many names as can each clear the
minimum order size, so orders are never dust.
"""
from __future__ import annotations

from ..config import settings
from ..data.token_list import STABLECOINS
from . import rebalance_strategy
from .base_strategy import PortfolioState, TradeOrder

MIN_ORDER_USD = 2.0
STABLE = "USDT"


def decide(state: PortfolioState, symbols: list[str], *,
           per_token_pct: float | None = None,
           stable_floor: float | None = None) -> list[TradeOrder]:
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)

    per_token_pct = settings.basket_max_position_pct if per_token_pct is None else per_token_pct
    stable_floor = settings.stablecoin_floor_pct if stable_floor is None else stable_floor

    basket = [s for s in symbols if s not in STABLECOINS]
    if not basket:
        return []

    deployable = max(0.0, state.equity_usd * (1.0 - stable_floor))
    # Cap the basket width to what capital can fund without dust orders.
    n_max = int(deployable // MIN_ORDER_USD)
    if n_max < 1:
        return []
    basket = basket[:n_max]

    target_each = min(deployable / len(basket), state.equity_usd * per_token_pct)
    if target_each < MIN_ORDER_USD:
        return []

    orders: list[TradeOrder] = []
    for sym in basket:
        gap = target_each - state.token_values_usd.get(sym, 0.0)
        if gap >= MIN_ORDER_USD:
            orders.append(TradeOrder(STABLE, sym, gap, "eligible-basket"))
    return orders
