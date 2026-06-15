"""Safeguard — the disqualification guard evaluated every tick.

Decides three protective actions from the current portfolio + risk state:
  - derisk: drawdown breaker tripped/cap breached -> convert risk assets to stable
  - halt_buys: equity near the floor -> stop spending, preserve what's left
  - needs_compliance_trade: min-trade interval elapsed -> place a tiny trade so
    the contest's minimum-activity requirement is met
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..risk.drawdown import DrawdownTracker
from ..risk.trade_counter import TradeCounter
from ..strategy.base_strategy import PortfolioState


@dataclass(frozen=True)
class SafeguardAction:
    derisk: bool
    halt_buys: bool
    needs_compliance_trade: bool
    reason: str


def evaluate(
    state: PortfolioState,
    drawdown: DrawdownTracker,
    trade_counter: TradeCounter,
    now: datetime,
    *,
    min_trade_interval_h: int,
    low_equity_usd: float,
) -> SafeguardAction:
    derisk = drawdown.breaker_tripped() or drawdown.cap_breached()
    halt_buys = state.equity_usd <= low_equity_usd
    needs_compliance_trade = trade_counter.needs_trade(now, min_trade_interval_h)

    reasons = []
    if derisk:
        reasons.append("drawdown breaker")
    if halt_buys:
        reasons.append("low equity")
    if needs_compliance_trade:
        reasons.append("min-trade due")

    return SafeguardAction(
        derisk=derisk,
        halt_buys=halt_buys,
        needs_compliance_trade=needs_compliance_trade,
        reason="; ".join(reasons) or "ok",
    )
