"""Monitor layer tests — written test-first (TDD).

Covers the safeguard decision logic (the disqualification guard) and the
contest-specific PnL accounting where a start-of-hour wallet <= $1 scores 0%.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.agent.monitor import pnl
from src.agent.monitor.safeguard import SafeguardAction, evaluate
from src.agent.risk.drawdown import DrawdownTracker
from src.agent.risk.trade_counter import TradeCounter
from src.agent.strategy.base_strategy import PortfolioState

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def _state(equity):
    return PortfolioState(equity_usd=equity, risk_value_usd=0.0, stable_value_usd=equity)


def _eval(state, dt, tc):
    return evaluate(state, dt, tc, NOW, min_trade_interval_h=4, low_equity_usd=5.0)


# ----------------------------- safeguard -----------------------------

def test_derisk_when_breaker_tripped():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    dt.update(75.0)  # -25% -> tripped
    tc = TradeCounter([NOW])  # recent trade, so compliance not the trigger
    action = _eval(_state(75.0), dt, tc)
    assert action.derisk is True


def test_halt_buys_when_equity_near_floor():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(4.0)  # peak == current, no drawdown
    tc = TradeCounter([NOW])
    action = _eval(_state(4.0), dt, tc)
    assert action.halt_buys is True
    assert action.derisk is False


def test_compliance_trade_flagged_when_due():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    tc = TradeCounter([])  # never traded -> due
    action = _eval(_state(100.0), dt, tc)
    assert action.needs_compliance_trade is True


def test_all_clear_under_normal_conditions():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    tc = TradeCounter([NOW])
    action = _eval(_state(100.0), dt, tc)
    assert action == SafeguardAction(derisk=False, halt_buys=False,
                                     needs_compliance_trade=False, reason="ok")


# ----------------------------- contest PnL accounting -----------------------------

def test_hourly_return_zero_when_start_wallet_at_or_below_one_dollar():
    # Contest rule: start-of-hour value <= $1 -> that hour counts as 0%.
    assert pnl.hourly_return(prev_equity=0.5, curr_equity=2.0) == 0.0
    assert pnl.hourly_return(prev_equity=1.0, curr_equity=5.0) == 0.0


def test_hourly_return_normal():
    assert pnl.hourly_return(prev_equity=100.0, curr_equity=110.0) == pytest.approx(0.10)
    assert pnl.hourly_return(prev_equity=100.0, curr_equity=90.0) == pytest.approx(-0.10)


def test_cumulative_return():
    assert pnl.cumulative_return(start_equity=100.0, curr_equity=120.0) == pytest.approx(0.20)
    assert pnl.cumulative_return(start_equity=0.0, curr_equity=50.0) == 0.0  # guard div-by-zero
