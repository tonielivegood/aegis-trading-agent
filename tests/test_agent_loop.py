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


def test_make_executor_selects_openocean(mocker):
    mocker.patch.object(al.settings, "execution_backend", "openocean")
    oo = mocker.patch("src.agent.execution.openocean.OpenOcean")
    al._make_executor(dry_run=True)
    oo.assert_called_once()


def test_make_executor_selects_1inch(mocker):
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    oi = mocker.patch("src.agent.execution.oneinch.OneInch")
    al._make_executor(dry_run=True)
    oi.assert_called_once()


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


# --- execution failover + failed-exit alert (#2 hardening) ---

class _FakeDex:
    """Stub backend: optionally raises, else returns a SwapResult."""
    def __init__(self, fail=False, simulated=False, tx="0xdead"):
        self.fail, self.simulated, self.tx, self.calls = fail, simulated, tx, []

    def swap(self, token_in, token_out, amount_in):
        self.calls.append((token_in, token_out, amount_in))
        if self.fail:
            raise RuntimeError("backend down")
        from src.agent.execution.pancakeswap import SwapResult
        return SwapResult(token_in, token_out, 0, 0, 0, simulated=self.simulated, tx_hash=self.tx)


def _patch_backends(mocker, mapping):
    """Route _make_executor_for(backend, dry_run) to a per-name stub."""
    mocker.patch.object(al, "_make_executor_for",
                        side_effect=lambda backend, dry_run: mapping[backend])


def _exit_order(sym="BAS"):
    return TradeOrder(sym, "USDT", 5.0, "stop")           # sells token -> stable = EXIT


def _entry_order(sym="BAS"):
    return TradeOrder("USDT", sym, 5.0, "breakout")       # buys token = ENTRY


_PRICES = {"BAS": 0.03, "USDT": 1.0}


def test_exit_fails_over_to_backup_backend(mocker):
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    primary, oo = _FakeDex(fail=True), _FakeDex(tx="0xfeed")
    _patch_backends(mocker, {"1inch": primary, "openocean": oo, "pancake": _FakeDex()})
    tc = mocker.Mock()
    out = al._execute([_exit_order()], _PRICES, dry_run=False, trade_counter=tc, now=0)
    assert out[0].get("error") is None and out[0]["tx"] == "0xfeed"
    assert out[0]["failover_backend"] == "openocean"
    assert len(oo.calls) == 1 and tc.record_trade.called


def test_entry_does_not_failover(mocker):
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    primary, oo = _FakeDex(fail=True), _FakeDex()
    _patch_backends(mocker, {"1inch": primary, "openocean": oo, "pancake": _FakeDex()})
    out = al._execute([_entry_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(), now=0)
    assert "error" in out[0]            # failed, dropped this tick
    assert oo.calls == []               # no failover attempted for an entry


def test_exit_all_backends_fail_alerts(mocker):
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    _patch_backends(mocker, {"1inch": _FakeDex(fail=True),
                             "openocean": _FakeDex(fail=True), "pancake": _FakeDex(fail=True)})
    send = mocker.patch.object(al.notifier, "send")
    out = al._execute([_exit_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(), now=0)
    assert "error" in out[0]
    assert send.called                  # operator paged that the exit is stuck


def test_exit_primary_success_no_alert_no_failover(mocker):
    mocker.patch.object(al.settings, "execution_backend", "1inch")
    primary, oo = _FakeDex(tx="0xok"), _FakeDex()
    _patch_backends(mocker, {"1inch": primary, "openocean": oo, "pancake": _FakeDex()})
    send = mocker.patch.object(al.notifier, "send")
    out = al._execute([_exit_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(), now=0)
    assert out[0]["tx"] == "0xok" and "failover_backend" not in out[0]
    assert oo.calls == [] and not send.called


def test_twak_primary_does_not_failover(mocker):
    mocker.patch.object(al.settings, "execution_backend", "twak")
    twak, oo = _FakeDex(fail=True), _FakeDex()
    _patch_backends(mocker, {"twak": twak, "openocean": oo, "pancake": _FakeDex()})
    out = al._execute([_exit_order()], _PRICES, dry_run=False, trade_counter=mocker.Mock(), now=0)
    assert "error" in out[0] and oo.calls == []   # separate wallet → never cross over


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


# --- Binance W3W universe: pricing, discovery, just-in-time safety check ---

def test_w3w_universe_prices_resolves_via_price_info(mocker):
    from src.agent.data import token_list
    token_list.register_discovered("WTEST", "0x1111111111111111111111111111111111111a")
    try:
        mocker.patch("src.agent.execution.binance_web3.price_info",
                    return_value={"0x1111111111111111111111111111111111111a": {"price": "2.5"}})
        prices = al._w3w_universe_prices({"WTEST"})
        assert prices == {"WTEST": 2.5}
    finally:
        token_list._discovered.pop("WTEST", None)
        token_list._discovered_classes.pop("WTEST", None)


def test_w3w_universe_prices_skips_unresolvable_symbol(mocker):
    price_info = mocker.patch("src.agent.execution.binance_web3.price_info", return_value={})
    prices = al._w3w_universe_prices({"NOT_A_REAL_TOKEN_XYZ"})
    assert prices == {}
    price_info.assert_not_called()   # nothing resolvable -> never even calls the API


def test_w3w_universe_prices_network_error_never_raises(mocker):
    from src.agent.data import token_list
    token_list.register_discovered("WTEST2", "0x2222222222222222222222222222222222222b")
    try:
        mocker.patch("src.agent.execution.binance_web3.price_info", side_effect=RuntimeError("boom"))
        assert al._w3w_universe_prices({"WTEST2"}) == {}
    finally:
        token_list._discovered.pop("WTEST2", None)
        token_list._discovered_classes.pop("WTEST2", None)


def test_w3w_hot_token_items_returns_none_when_flag_off(mocker):
    mocker.patch.object(al.settings, "binance_w3w_universe_enabled", False)
    assert al._w3w_hot_token_items() is None


def test_w3w_hot_token_items_returns_none_on_network_error(mocker):
    mocker.patch.object(al.settings, "binance_w3w_universe_enabled", True)
    mocker.patch("src.agent.execution.binance_web3.hot_token", side_effect=RuntimeError("boom"))
    assert al._w3w_hot_token_items() is None


def test_w3w_hot_token_items_passes_meme_breakout_min(mocker):
    from src.agent.aegis import token_class as tc
    mocker.patch.object(al.settings, "binance_w3w_universe_enabled", True)
    hot = mocker.patch("src.agent.execution.binance_web3.hot_token", return_value=[{"a": 1}])
    out = al._w3w_hot_token_items()
    assert out == [{"a": 1}]
    assert hot.call_args.kwargs["price_change_percent_min"] == tc.params(tc.MEME).breakout_min * 100


def test_w3w_safety_check_registers_token_on_pass(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    from src.agent.data import token_list
    mocker.patch("src.agent.execution.binance_web3.quote", return_value=[
        {"isBest": True, "toToken": {"isHoneyPot": False, "taxRate": "0.01", "decimal": "9"}},
    ])
    sig = BreakoutSignal(symbol="NEWMEME", contract="0x3333333333333333333333333333333333333c",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    try:
        check = al._w3w_safety_check(40.0)
        assert check(sig) is True
        tok = token_list.get_token("NEWMEME")
        assert tok.contract.lower() == sig.contract and tok.decimals == 9
    finally:
        token_list._discovered.pop("NEWMEME", None)
        token_list._discovered_classes.pop("NEWMEME", None)


def test_w3w_safety_check_blocks_honeypot(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    mocker.patch("src.agent.execution.binance_web3.quote", return_value=[
        {"isBest": True, "toToken": {"isHoneyPot": True, "taxRate": "0", "decimal": "18"}},
    ])
    sig = BreakoutSignal(symbol="SCAM", contract="0x4444444444444444444444444444444444444d",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    check = al._w3w_safety_check(40.0)
    assert check(sig) is False


def test_w3w_safety_check_blocks_high_tax(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    mocker.patch("src.agent.execution.binance_web3.quote", return_value=[
        {"isBest": True, "toToken": {"isHoneyPot": False, "taxRate": "0.25", "decimal": "18"}},
    ])
    sig = BreakoutSignal(symbol="TAXED", contract="0x5555555555555555555555555555555555555e",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    check = al._w3w_safety_check(40.0)
    assert check(sig) is False


def test_w3w_safety_check_no_routes_fails_closed(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    mocker.patch("src.agent.execution.binance_web3.quote", return_value=[])
    sig = BreakoutSignal(symbol="NOROUTE", contract="0x6666666666666666666666666666666666666f",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    check = al._w3w_safety_check(40.0)
    assert check(sig) is False


def test_w3w_safety_check_network_error_fails_closed(mocker):
    from src.agent.aegis.volume_breakout import BreakoutSignal
    mocker.patch("src.agent.execution.binance_web3.quote", side_effect=RuntimeError("boom"))
    sig = BreakoutSignal(symbol="ERR", contract="0x7777777777777777777777777777777777777a",
                         vol_multiple=0.0, breakout_pct=0.08, recent_pump_pct=0.0,
                         slippage_est=0.0, price_now=1.0, baseline_vol=1000.0)
    check = al._w3w_safety_check(40.0)
    assert check(sig) is False
