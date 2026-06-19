"""Track 1 min-trade compliance tests. Pure; no network, no broadcast."""
from __future__ import annotations

from src.agent.agent_loop import _compliance_orders
from src.agent.aegis import compliance as comp
from src.agent.aegis.compliance import (
    SAFE_SKIP_REASON,
    ComplianceTracker,
    pick_compliance_trade,
)
from src.agent.aegis.volume_anomaly_detector import MarketSnapshot
from src.agent.data import token_list
from src.agent.strategy.base_strategy import PortfolioState


def test_fallback_compliance_buys_eligible_never_wbnb():
    # all-cash → must buy an ELIGIBLE token, never WBNB (the old bug)
    st = PortfolioState(equity_usd=30, risk_value_usd=0, stable_value_usd=30, token_values_usd={})
    orders = _compliance_orders(st)
    assert orders and orders[0].token_out != "WBNB"
    assert token_list.is_eligible(token_list.get_token(orders[0].token_out).contract)


def test_fallback_compliance_sells_held_eligible_token():
    alpha = token_list.alpha_symbols()[0]
    st = PortfolioState(equity_usd=30, risk_value_usd=10, stable_value_usd=5,
                        token_values_usd={alpha: 10.0})
    orders = _compliance_orders(st)
    assert orders and orders[0].token_in == alpha and orders[0].token_out == "USDT"

TWT = token_list.get_token("TWT").contract           # in the official allowlist
DEAD = "0x000000000000000000000000000000000000dead"  # not eligible

DAY = 86400.0


def _tracker():
    return ComplianceTracker()


def _state(equity=100.0, stable=100.0, **kw):
    return PortfolioState(equity_usd=equity, risk_value_usd=0.0,
                          stable_value_usd=stable, token_values_usd=kw.pop("holdings", {}), **kw)


def _rec(tr, contract, ts):
    return tr.record_executed(symbol="X", contract=contract, notional_usd=10.0,
                              side="buy", source="event", reason="r", now_ts=ts)


# ----------------------------- valid-trade counting -----------------------------

def test_eligible_trade_counts():
    tr = _tracker()
    assert _rec(tr, TWT, 1000.0) is True
    assert tr.valid_total() == 1


def test_non_eligible_trade_does_not_count():
    tr = _tracker()
    assert _rec(tr, DEAD, 1000.0) is False
    assert tr.valid_total() == 0 and tr.invalid_ignored == 1


def test_symbol_only_without_contract_does_not_count():
    # No contract => cannot be matched to the allowlist => not counted.
    tr = _tracker()
    assert tr.record_executed(symbol="TWT", contract="", notional_usd=10.0,
                              side="buy", source="event", reason="r", now_ts=1000.0) is False
    assert tr.valid_total() == 0


def test_one_valid_trade_satisfies_daily_requirement():
    tr = _tracker()
    _rec(tr, TWT, 1000.0)
    assert tr.valid_today(1000.0) >= 1


def test_trades_on_different_utc_days_counted_per_day():
    tr = _tracker()
    _rec(tr, TWT, 1000.0)
    _rec(tr, TWT, 1000.0 + DAY)
    assert tr.valid_today(1000.0) == 1
    assert tr.valid_today(1000.0 + DAY) == 1
    assert tr.valid_total() == 2


def test_seven_valid_trades_satisfy_weekly_requirement():
    tr = _tracker()
    for i in range(7):
        _rec(tr, TWT, 1000.0 + i * DAY)
    assert tr.valid_total() >= 7


def test_report_fields():
    tr = _tracker()
    _rec(tr, TWT, 1000.0)
    _rec(tr, DEAD, 1000.0)
    rep = tr.report(1000.0)
    assert rep.valid_trades_today == 1 and rep.valid_trades_total == 1
    assert rep.invalid_trades_ignored == 1
    assert rep.last_valid_trade is not None


# ----------------------------- compliance trade selection -----------------------------

class _Feed:
    def __init__(self, liquidity_ok=True, slippage=0.001):
        self._ok, self._slip = liquidity_ok, slippage

    def snapshot(self, symbol, price=None):
        return MarketSnapshot(symbol=symbol, contract="0x", price_now=1.0,
                              has_route=True, liquidity_ok=self._ok, slippage_est=self._slip)


def test_compliance_picks_safe_eligible_trade():
    order, reason = pick_compliance_trade(_state(), {}, _Feed(liquidity_ok=True), order_usd=10)
    assert order is not None and reason == comp.COMPLIANCE_REASON
    assert order.token_in == "USDT" and order.amount_in_usd == 10.0
    assert token_list.is_eligible(token_list.get_token(order.token_out).contract)


def test_compliance_safe_skips_when_no_liquidity():
    order, reason = pick_compliance_trade(_state(), {}, _Feed(liquidity_ok=False))
    assert order is None and reason == SAFE_SKIP_REASON


def test_compliance_never_bypasses_breaker():
    order, reason = pick_compliance_trade(_state(drawdown_tripped=True), {}, _Feed(), order_usd=10)
    assert order is None and reason == SAFE_SKIP_REASON


def test_compliance_respects_stablecoin_floor():
    # equity 100 -> floor max(6, 15)=15; stable 20 -> 20-10=10 < 15 -> safe skip
    order, reason = pick_compliance_trade(_state(stable=20.0), {}, _Feed(), order_usd=10)
    assert order is None and reason == SAFE_SKIP_REASON


# ----------------------------- scoring honesty -----------------------------

def test_scoring_mode_unconfirmed_and_no_nav_hardcode():
    from src.agent.config import settings
    assert settings.track1_scoring_mode == "unconfirmed"
    assert settings.track1_score_nav_assumption == "unknown_do_not_hardcode"
    # the tracker counts trade ACTIVITY, never assumes a stablecoin-NAV score
    tr = _tracker()
    _rec(tr, TWT, 1000.0)
    assert tr.valid_total() == 1  # purely activity-based, no NAV assumption
