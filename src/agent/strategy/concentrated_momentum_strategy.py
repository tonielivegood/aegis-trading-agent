"""Concentrated-momentum strategy — the aggressive contest instrument.

Where `adaptive_hold_strategy` spreads capital thinly across a diversified
basket (survival-first), this concentrates ~80% of equity into the top-N
strongest-momentum tokens and rotates as the ranking turns over. The thesis:
a 7-day contest scored on RAW total return rewards conviction, and a middling
diversified finish wins nothing in a top-takes-most field.

The aggression is bounded by three rails so concentration never becomes a DQ:
  - the drawdown breaker still exits everything to cash (30% DD = disqualified),
  - a per-token cap (`per_token_cap`) limits any single name's share of equity,
  - only BUY-direction signals are eligible, so a down market parks us in cash
    rather than concentrating into a falling knife.

Liquidity gating happens UPSTREAM (the caller passes signals only for liquid
majors), so the "strongest mover" can never be an illiquid pump that round-trips
to dust. This keeps backtest/live parity: the same SignalBundle list the live
agent builds is what the walk-forward adapter reconstructs.
"""
from __future__ import annotations

from ..data.token_list import STABLECOINS
from ..signal.signal_schema import SignalBundle
from . import rebalance_strategy
from .base_strategy import PortfolioState, TradeOrder

TOP_N = 2
DEPLOY_FRAC = 0.80
PER_TOKEN_CAP = 0.50
MIN_ORDER_USD = 2.0
STABLE = "USDT"


def decide(
    signals: list[SignalBundle],
    state: PortfolioState,
    *,
    top_n: int = TOP_N,
    deploy_frac: float = DEPLOY_FRAC,
    per_token_cap: float = PER_TOKEN_CAP,
) -> list[TradeOrder]:
    # DQ insurance: tripped/breached -> exit all risk to stable, same as survival.
    if state.drawdown_tripped or state.cap_breached:
        return rebalance_strategy.derisk_orders(state)

    # Rank only positive-momentum (BUY) names; strongest first.
    buys = sorted(
        (s for s in signals if s.direction == "BUY"),
        key=lambda s: s.combined_score,
        reverse=True,
    )
    buy_order = [s.symbol for s in buys]
    buy_set = set(buy_order)

    held = {
        sym: val for sym, val in state.token_values_usd.items()
        if sym not in STABLECOINS and val >= MIN_ORDER_USD
    }

    orders: list[TradeOrder] = []

    # Hysteresis: HOLD a concentrated name while its trend is intact (still BUY);
    # rotate OUT only when the trend breaks (no longer a BUY signal). This avoids
    # the hourly-churn death spiral that pure top-N re-ranking produces.
    kept = [sym for sym in held if sym in buy_set]
    for sym, val in held.items():
        if sym not in buy_set:
            orders.append(TradeOrder(sym, STABLE, val, "trend broke"))

    # Fill remaining slots with the strongest BUY names we don't already hold.
    chosen = list(kept[:top_n])
    for sym in buy_order:
        if len(chosen) >= top_n:
            break
        if sym not in chosen:
            chosen.append(sym)

    # No qualifying uptrend -> stay/return to cash (preservation).
    if not chosen:
        return orders

    target_each = state.equity_usd * deploy_frac / len(chosen)
    target_each = min(target_each, state.equity_usd * per_token_cap)

    # Rotate IN / top up the chosen names toward their concentrated target.
    for sym in chosen:
        cur = state.token_values_usd.get(sym, 0.0)
        gap = target_each - cur
        if gap >= MIN_ORDER_USD:
            orders.append(TradeOrder(STABLE, sym, gap, f"concentrate {sym}"))

    return orders
