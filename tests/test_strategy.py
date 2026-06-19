"""Strategy layer tests — written test-first (TDD).

Core safety invariant: every order the strategy emits must respect the risk
caps (per-token max, stablecoin floor) and no new buys may be issued once the
drawdown breaker has tripped.
"""
from __future__ import annotations

import pytest

from src.agent.strategy.base_strategy import PortfolioState
from src.agent.strategy import momentum_strategy, rebalance_strategy
from src.agent.signal.signal_schema import SignalBundle


def _sig(symbol, combined, direction):
    return SignalBundle(symbol=symbol, momentum_score=combined, sentiment_score=0.0,
                        confidence=abs(combined), combined_score=combined, direction=direction)


def _state(equity=100.0, risk=0.0, stable=100.0, holdings=None, tripped=False, cap=False):
    return PortfolioState(
        equity_usd=equity, risk_value_usd=risk, stable_value_usd=stable,
        token_values_usd=holdings or {}, drawdown_tripped=tripped, cap_breached=cap,
    )


# ----------------------------- buys respect caps -----------------------------

def test_buy_order_capped_at_per_token_max():
    signals = [_sig("CAKE", 0.8, "BUY")]
    orders = momentum_strategy.decide(signals, _state(equity=100.0))
    assert len(orders) == 1
    # 10% of $100 equity = $10 max per token.
    assert orders[0].amount_in_usd == pytest.approx(10.0)
    assert orders[0].token_in == "USDT"
    assert orders[0].token_out == "CAKE"


def test_buys_stop_at_stablecoin_floor():
    # Deployable budget = equity*(1 - floor); with risk already near it, only the
    # remainder may be bought. Derive the floor from config so the test stays
    # correct regardless of the configured STABLECOIN_FLOOR_PCT.
    from src.agent.config import settings
    equity, risk = 100.0, 75.0
    remaining = max(0.0, equity * (1.0 - settings.stablecoin_floor_pct) - risk)
    signals = [_sig(s, 0.9, "BUY") for s in ("CAKE", "ETH", "BTCB", "ADA", "DOT")]
    orders = momentum_strategy.decide(signals, _state(equity=equity, risk=risk))
    assert sum(o.amount_in_usd for o in orders) <= remaining + 1e-9


def test_no_buys_when_breaker_tripped():
    signals = [_sig("CAKE", 0.9, "BUY")]
    orders = momentum_strategy.decide(signals, _state(tripped=True))
    assert orders == []


def test_no_buys_when_cap_breached():
    signals = [_sig("CAKE", 0.9, "BUY")]
    orders = momentum_strategy.decide(signals, _state(cap=True))
    assert orders == []


def test_buys_limited_per_tick():
    signals = [_sig(s, 0.9, "BUY") for s in ("CAKE", "ETH", "BTCB", "ADA", "DOT", "LINK")]
    orders = momentum_strategy.decide(signals, _state(equity=1000.0))
    assert len(orders) <= momentum_strategy.MAX_NEW_POSITIONS


def test_buy_skips_token_already_at_cap():
    signals = [_sig("CAKE", 0.9, "BUY")]
    # already holding $10 of CAKE = the per-token cap at equity 100
    orders = momentum_strategy.decide(signals, _state(equity=100.0, holdings={"CAKE": 10.0}))
    assert orders == []


def test_dust_orders_skipped():
    signals = [_sig("CAKE", 0.9, "BUY")]
    # tiny equity -> max position below MIN_ORDER_USD -> no order
    orders = momentum_strategy.decide(signals, _state(equity=5.0))
    assert orders == []


# ----------------------------- sells -----------------------------

def test_sell_order_for_held_token():
    signals = [_sig("CAKE", -0.8, "SELL")]
    orders = momentum_strategy.decide(signals, _state(holdings={"CAKE": 8.0}))
    sells = [o for o in orders if o.token_in == "CAKE"]
    assert len(sells) == 1
    assert sells[0].token_out == "USDT"
    assert sells[0].amount_in_usd == pytest.approx(8.0)


def test_no_sell_when_not_held():
    signals = [_sig("CAKE", -0.8, "SELL")]
    orders = momentum_strategy.decide(signals, _state(holdings={}))
    assert orders == []


# ----------------------------- derisking -----------------------------

def test_derisk_sells_all_risk_assets_to_stable():
    state = _state(holdings={"CAKE": 8.0, "ETH": 12.0, "USDT": 30.0})
    orders = rebalance_strategy.derisk_orders(state)
    sold = {o.token_in for o in orders}
    assert sold == {"CAKE", "ETH"}        # stablecoins are not sold
    assert all(o.token_out == "USDT" for o in orders)


def test_derisk_skips_dust():
    state = _state(holdings={"CAKE": 0.5, "ETH": 12.0})
    orders = rebalance_strategy.derisk_orders(state)
    assert {o.token_in for o in orders} == {"ETH"}


# ----------------------------- the risk-gate guarantee -----------------------------

def test_all_buy_orders_pass_risk_gate():
    from src.agent.risk.position_sizer import PositionSizer
    from src.agent.config import settings

    signals = [_sig(s, 0.9, "BUY") for s in ("CAKE", "ETH", "BTCB", "ADA")]
    state = _state(equity=100.0, risk=0.0)
    orders = momentum_strategy.decide(signals, state)

    sizer = PositionSizer(state.equity_usd, settings.max_position_pct, settings.stablecoin_floor_pct)
    running_risk = state.risk_value_usd
    for o in (o for o in orders if o.token_in == "USDT"):
        held = state.token_values_usd.get(o.token_out, 0.0)
        allowed = sizer.size_for(held, running_risk)
        assert o.amount_in_usd <= allowed + 1e-9
        running_risk += o.amount_in_usd


# ----------------------------- adaptive hold (validated production strategy) -----------------------------

def test_adaptive_hold_deploys_toward_target():
    from src.agent.strategy import adaptive_hold_strategy as ah
    # 8 tokens so the per-token cap (10% = $10) doesn't bind on a 50% ($50) deploy.
    basket = ["CAKE", "ETH", "BTCB", "ADA", "DOT", "LINK", "UNI", "ATOM"]
    orders = ah.decide(_state(equity=100.0, risk=0.0), basket, deploy_frac=0.5)
    assert all(o.token_in == "USDT" for o in orders)
    assert sum(o.amount_in_usd for o in orders) == pytest.approx(50.0, abs=1.0)


def test_adaptive_hold_no_orders_when_at_target():
    from src.agent.strategy import adaptive_hold_strategy as ah
    orders = ah.decide(_state(equity=100.0, risk=50.0), ["CAKE", "ETH"], deploy_frac=0.5)
    assert orders == []


def test_adaptive_hold_derisks_when_breaker_tripped():
    from src.agent.strategy import adaptive_hold_strategy as ah
    state = _state(equity=80.0, holdings={"CAKE": 20.0, "USDT": 60.0}, tripped=True)
    orders = ah.decide(state, ["CAKE"], deploy_frac=0.5)
    assert any(o.token_in == "CAKE" and o.token_out == "USDT" for o in orders)


def test_adaptive_hold_respects_per_token_cap():
    from src.agent.strategy import adaptive_hold_strategy as ah
    from src.agent.config import settings
    # one token, 50% deploy would be $50 but per-token cap is 10% = $10
    orders = ah.decide(_state(equity=100.0, risk=0.0), ["CAKE"], deploy_frac=0.5)
    assert orders[0].amount_in_usd <= settings.max_position_pct * 100.0 + 1e-9
