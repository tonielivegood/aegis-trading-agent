"""Concentrated-momentum strategy tests — written test-first (TDD).

This is the aggressive contest strategy: instead of spreading capital across a
6-token basket, it concentrates ~80% of equity into the top-N (default 2)
strongest-momentum tokens and rotates as the ranking changes. It MUST keep the
same DQ insurance as the survival strategy: when the drawdown breaker trips it
exits to cash. Concentration without that seatbelt would risk the 30% DQ gate.

Invariants under test:
  - breaker tripped / cap breached  -> derisk to stable (no fresh risk)
  - selects the top-N by combined_score among BUY-direction signals only
  - deploys deploy_frac of equity, split across the chosen, with a per-token cap
  - rotates OUT held non-stable tokens that fall out of the chosen set
  - no BUY signals -> hold/return to cash (preservation instinct preserved)
  - never emits dust orders below MIN_ORDER_USD
"""
from __future__ import annotations

from src.agent.signal.signal_schema import SignalBundle
from src.agent.strategy import concentrated_momentum_strategy as cms
from src.agent.strategy.base_strategy import PortfolioState


def _sig(symbol: str, score: float, direction: str = "BUY") -> SignalBundle:
    return SignalBundle(
        symbol=symbol, momentum_score=score, sentiment_score=0.0,
        confidence=abs(score), combined_score=score, direction=direction,
    )


def _state(equity: float, holdings: dict[str, float] | None = None, **kw) -> PortfolioState:
    holdings = holdings or {}
    risk = sum(v for s, v in holdings.items() if s != "USDT")
    stable = holdings.get("USDT", equity - risk)
    return PortfolioState(
        equity_usd=equity, risk_value_usd=risk, stable_value_usd=stable,
        token_values_usd=holdings, **kw,
    )


# ----------------------------- DQ insurance -----------------------------

def test_breaker_tripped_derisks_to_stable():
    state = _state(100.0, {"BTCB": 40.0}, drawdown_tripped=True)
    orders = cms.decide([_sig("BTCB", 0.9)], state)
    assert orders and all(o.token_out == "USDT" for o in orders)
    assert any(o.token_in == "BTCB" for o in orders)


def test_cap_breached_derisks_to_stable():
    state = _state(100.0, {"ETH": 30.0}, cap_breached=True)
    orders = cms.decide([_sig("ETH", 0.9)], state)
    assert all(o.token_out == "USDT" for o in orders)


# ----------------------------- selection -----------------------------

def test_selects_top_two_by_combined_score():
    signals = [_sig("A", 0.2), _sig("B", 0.9), _sig("C", 0.5), _sig("D", 0.7)]
    orders = cms.decide(signals, _state(100.0), top_n=2)
    bought = {o.token_out for o in orders if o.token_in == "USDT"}
    assert bought == {"B", "D"}  # the two strongest, not A/C


def test_only_buy_direction_signals_are_eligible():
    signals = [_sig("A", 0.9, "SELL"), _sig("B", 0.3, "BUY"), _sig("C", 0.8, "HOLD")]
    orders = cms.decide(signals, _state(100.0), top_n=2)
    bought = {o.token_out for o in orders if o.token_in == "USDT"}
    assert bought == {"B"}  # only the BUY-direction token qualifies


# ----------------------------- concentration -----------------------------

def test_concentrates_deploy_frac_split_across_chosen():
    signals = [_sig("B", 0.9), _sig("D", 0.7)]
    orders = cms.decide(signals, _state(100.0), top_n=2, deploy_frac=0.8, per_token_cap=0.5)
    buys = {o.token_out: o.amount_in_usd for o in orders if o.token_in == "USDT"}
    # 80% of 100 split across 2 -> 40 each (under the 50% cap)
    assert buys["B"] == 40.0 and buys["D"] == 40.0


def test_per_token_cap_limits_single_position():
    signals = [_sig("B", 0.9)]
    orders = cms.decide(signals, _state(100.0), top_n=1, deploy_frac=0.8, per_token_cap=0.5)
    buys = {o.token_out: o.amount_in_usd for o in orders if o.token_in == "USDT"}
    # one token, 80% wanted but capped at 50% of equity
    assert buys["B"] == 50.0


def test_no_rebuy_when_already_at_target():
    signals = [_sig("B", 0.9), _sig("D", 0.7)]
    state = _state(100.0, {"B": 40.0, "D": 40.0})
    orders = cms.decide(signals, state, top_n=2, deploy_frac=0.8)
    assert not [o for o in orders if o.token_in == "USDT"]  # no fresh buys; already deployed


# ----------------------------- rotation -----------------------------

def test_rotates_out_when_trend_breaks():
    # A held name whose signal is no longer BUY (trend broke) is fully exited.
    signals = [_sig("B", 0.9), _sig("D", 0.7), _sig("OLD", 0.1, "HOLD")]
    state = _state(100.0, {"OLD": 40.0, "USDT": 60.0})
    orders = cms.decide(signals, state, top_n=2)
    sells = [o for o in orders if o.token_in == "OLD"]
    assert sells and sells[0].token_out == "USDT"
    assert sells[0].amount_in_usd == 40.0  # fully rotated out


def test_holds_winner_no_churn_when_still_buy():
    # Hysteresis: a held name that is still BUY is kept even if a stronger name
    # exists and the slots are full — no churn out of an intact trend.
    signals = [_sig("NEW", 0.95), _sig("HELD", 0.30)]
    state = _state(100.0, {"HELD": 40.0, "USDT": 60.0})
    orders = cms.decide(signals, state, top_n=1)
    assert not [o for o in orders if o.token_in == "HELD"]  # not sold
    assert not [o for o in orders if o.token_out == "NEW"]  # no churn into NEW


def test_no_buy_signals_goes_to_cash():
    signals = [_sig("B", 0.9, "SELL"), _sig("D", 0.7, "HOLD")]
    state = _state(100.0, {"B": 40.0})
    orders = cms.decide(signals, state)
    # nothing trending up -> only the exit, no new risk
    assert all(o.token_out == "USDT" for o in orders)
    assert not [o for o in orders if o.token_in == "USDT"]


# ----------------------------- dust guard -----------------------------

def test_no_dust_orders_below_min():
    signals = [_sig("B", 0.9), _sig("D", 0.7)]
    # already nearly at target; remaining gap is sub-$2 dust
    state = _state(100.0, {"B": 39.5, "D": 39.5})
    orders = cms.decide(signals, state, top_n=2, deploy_frac=0.8)
    assert not [o for o in orders if o.token_in == "USDT"]
