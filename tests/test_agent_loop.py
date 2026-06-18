"""Edge-case tests for the orchestrator's helper logic (added in the polish pass).

These cover the pure glue functions that were integration-verified but not yet
unit-tested: USD->token conversion, price assembly (BNB alias, stable defaults),
and compliance-trade selection.
"""
from __future__ import annotations

from src.agent import agent_loop as al
from src.agent.strategy.base_strategy import PortfolioState, TradeOrder


def _state(equity, risk, stable, holdings=None):
    return PortfolioState(equity_usd=equity, risk_value_usd=risk, stable_value_usd=stable,
                          token_values_usd=holdings or {})


# --- _make_executor (backend selection) ---

def test_make_executor_defaults_to_pancake(mocker):
    mocker.patch.object(al.settings, "execution_backend", "pancake")
    ps = mocker.patch.object(al, "PancakeSwap")
    al._make_executor(dry_run=True)
    ps.assert_called_once()


def test_make_executor_selects_twak(mocker):
    mocker.patch.object(al.settings, "execution_backend", "twak")
    tw = mocker.patch("src.agent.execution.twak_executor.TwakExecutor")
    al._make_executor(dry_run=True)
    tw.assert_called_once()


# --- _amount_in_tokens ---

def test_amount_in_tokens_converts_usd_to_token():
    assert al._amount_in_tokens(TradeOrder("USDT", "CAKE", 10.0), {"USDT": 1.0}) == 10.0
    assert al._amount_in_tokens(TradeOrder("CAKE", "USDT", 6.0), {"CAKE": 2.0}) == 3.0


def test_amount_in_tokens_zero_when_price_missing_or_zero():
    o = TradeOrder("USDT", "CAKE", 10.0)
    assert al._amount_in_tokens(o, {}) == 0.0
    assert al._amount_in_tokens(o, {"USDT": 0.0}) == 0.0


# --- _build_prices ---

def test_build_prices_defaults_stables_to_one(mocker):
    mocker.patch("src.agent.agent_loop.price_feed.onchain_price_usd", return_value=600.0)
    prices = al._build_prices(["CAKE", "WBNB"], {"CAKE": {"price": 2.0}, "WBNB": {"price": 600.0}},
                             {"USDT": 5.0, "CAKE": 1.0})
    assert prices["CAKE"] == 2.0
    assert prices["USDT"] == 1.0  # stable injected even if not in quotes


def test_build_prices_bnb_uses_wbnb_quote():
    prices = al._build_prices(["WBNB"], {"WBNB": {"price": 600.0}}, {"BNB": 0.1})
    assert prices["BNB"] == 600.0  # native BNB priced from WBNB quote


def test_build_prices_bnb_falls_back_to_onchain(mocker):
    spy = mocker.patch("src.agent.agent_loop.price_feed.onchain_price_usd", return_value=590.0)
    prices = al._build_prices(["CAKE"], {"CAKE": {"price": 2.0}}, {"BNB": 0.1})
    assert prices["BNB"] == 590.0  # WBNB absent from quotes -> on-chain
    spy.assert_called_once()


# --- _compliance_orders ---

def test_compliance_trade_prefers_stable():
    orders = al._compliance_orders(_state(30, 0, 29, {}))
    assert len(orders) == 1
    assert orders[0].token_in == "USDT" and orders[0].token_out == "WBNB"


def test_compliance_trade_uses_risk_token_when_no_stable():
    orders = al._compliance_orders(_state(30, 10, 0, {"CAKE": 10.0}))
    assert orders[0].token_in == "CAKE" and orders[0].token_out == "USDT"


def test_compliance_trade_none_when_wallet_too_small():
    assert al._compliance_orders(_state(1.0, 0, 1.0, {})) == []
