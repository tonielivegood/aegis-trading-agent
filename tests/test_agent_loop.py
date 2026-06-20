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


# --- _apply_price_fallback (robust valuation: transient miss must not = $0) ---

def test_price_fallback_fills_held_token_missing_this_tick(tmp_path, mocker):
    mocker.patch.object(al, "PRICECACHE_FILE", tmp_path / "last_prices.json")
    # tick 1: LUNC priced → remembered
    out1 = al._apply_price_fallback({"LUNC": 6e-05, "USDT": 1.0}, {"LUNC": 100.0, "USDT": 5.0})
    assert out1["LUNC"] == 6e-05
    # tick 2: LUNC read FAILS (absent) but is still HELD → fall back to last known
    out2 = al._apply_price_fallback({"USDT": 1.0}, {"LUNC": 100.0, "USDT": 5.0})
    assert out2["LUNC"] == 6e-05


def test_price_fallback_real_price_always_wins(tmp_path, mocker):
    mocker.patch.object(al, "PRICECACHE_FILE", tmp_path / "last_prices.json")
    al._apply_price_fallback({"LUNC": 6e-05}, {"LUNC": 100.0})       # cache a good price
    # a real (much lower) read = a real crash → must NOT be masked by the cache
    out = al._apply_price_fallback({"LUNC": 3e-05}, {"LUNC": 100.0})
    assert out["LUNC"] == 3e-05


def test_price_fallback_only_for_held_tokens(tmp_path, mocker):
    mocker.patch.object(al, "PRICECACHE_FILE", tmp_path / "last_prices.json")
    al._apply_price_fallback({"FOO": 2.0}, {"FOO": 10.0})            # cache FOO
    # next tick we no longer hold FOO and it isn't priced → it stays absent
    out = al._apply_price_fallback({}, {"USDT": 5.0})
    assert "FOO" not in out


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

def test_compliance_trade_buys_eligible_not_wbnb():
    # all-cash → buy the safest ELIGIBLE token (WBNB is not in the 149 — old bug)
    from src.agent.data import token_list
    orders = al._compliance_orders(_state(30, 0, 29, {}))
    assert len(orders) == 1
    assert orders[0].token_in == "USDT" and orders[0].token_out != "WBNB"
    assert token_list.is_eligible(token_list.get_token(orders[0].token_out).contract)


def test_compliance_trade_sells_held_eligible_token():
    from src.agent.data import token_list
    alpha = token_list.alpha_symbols()[0]
    orders = al._compliance_orders(_state(30, 10, 0, {alpha: 10.0}))
    assert orders[0].token_in == alpha and orders[0].token_out == "USDT"


def test_compliance_trade_none_when_wallet_too_small():
    assert al._compliance_orders(_state(1.0, 0, 1.0, {})) == []


# --- kill-switch (flatten_to_cash) ---

def test_flatten_to_cash_sells_all_nonstable_and_clears_books(mocker):
    alpha = al.token_list.alpha_symbols()[0]
    mocker.patch.object(al, "read_onchain_balances",
                        return_value={alpha: 100.0, "USDT": 5.0, "BNB": 0.02})
    mocker.patch.object(al, "_event_prices",
                        return_value={alpha: 0.1, "USDT": 1.0, "BNB": 600.0})
    executed = mocker.patch.object(al, "_execute", return_value=[])
    cleared = mocker.patch.object(al, "_clear_position_book")
    res = al.flatten_to_cash(dry_run=True)
    orders = executed.call_args.args[0]
    assert [(o.token_in, o.token_out) for o in orders] == [(alpha, "USDT")]  # sells the held token
    assert res["dry_run"] is True
    cleared.assert_called_once()
