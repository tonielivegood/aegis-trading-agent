"""Risk layer tests — written test-first (TDD).

The risk layer is the contest safety gate; these tests encode the abuse cases
from the threat model:
  - drawdown breaker MUST trip at the threshold (disqualification guard)
  - position sizer MUST never exceed caps, even with adversarial inputs
  - stablecoin floor MUST be preserved (wallet must never drain)
  - NaN/negative inputs MUST fail safe (no trade), never crash
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.agent.risk.drawdown import DrawdownTracker
from src.agent.risk.position_sizer import PositionSizer
from src.agent.risk.portfolio import Portfolio
from src.agent.risk.trade_counter import TradeCounter


# ============================== DrawdownTracker ==============================

def test_drawdown_zero_at_start():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    assert dt.peak == 100.0
    assert dt.current_drawdown() == 0.0


def test_drawdown_tracks_rolling_peak():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    dt.update(110.0)
    dt.update(99.0)
    assert dt.peak == 110.0
    assert dt.current_drawdown() == pytest.approx((110 - 99) / 110)


def test_breaker_trips_exactly_at_alert_threshold():
    # Boundary test: at exactly -20%, the breaker MUST be tripped.
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    dt.update(80.0)  # exactly -20%
    assert dt.current_drawdown() == pytest.approx(0.20)
    assert dt.breaker_tripped() is True


def test_breaker_not_tripped_just_below_threshold():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    dt.update(80.5)  # -19.5%
    assert dt.breaker_tripped() is False


def test_breaker_stays_tripped_after_recovery_within_session():
    # Once tripped, do not silently un-trip on a tiny bounce — latch it.
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    dt.update(78.0)  # tripped
    dt.update(95.0)  # bounce
    assert dt.breaker_tripped() is True


def test_cap_breached_detection():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    dt.update(69.0)  # -31%
    assert dt.cap_breached() is True


def test_drawdown_rejects_nan_and_negative():
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    with pytest.raises(ValueError):
        dt.update(float("nan"))
    with pytest.raises(ValueError):
        dt.update(-5.0)


# --- breaker DEBOUNCE: a lone glitch tick must not latch the contest-killer ---

def test_breaker_debounce_ignores_single_glitch_tick():
    # One tick at -25% (e.g. a transient price-read valuing a held token at $0)
    # must NOT latch when a debounce streak is required.
    dt = DrawdownTracker(alert=0.20, cap=0.30, latch_ticks=3)
    dt.update(100.0)
    dt.update(75.0)            # glitch tick 1 (-25%)
    assert dt.breaker_tripped() is False
    dt.update(100.0)           # equity recovers next tick → streak resets
    assert dt.breaker_tripped() is False


def test_breaker_debounce_latches_on_sustained_breach():
    dt = DrawdownTracker(alert=0.20, cap=0.30, latch_ticks=3)
    dt.update(100.0)
    dt.update(75.0)            # breach 1
    dt.update(74.0)            # breach 2
    assert dt.breaker_tripped() is False
    dt.update(76.0)            # breach 3 (still ≤ -20%) → latch
    assert dt.breaker_tripped() is True
    dt.update(99.0)            # bounce → stays latched
    assert dt.breaker_tripped() is True


def test_breaker_debounce_streak_resets_between_nonconsecutive_glitches():
    dt = DrawdownTracker(alert=0.20, cap=0.30, latch_ticks=3)
    dt.update(100.0)
    for _ in range(3):
        dt.update(75.0)        # a breach...
        dt.update(100.0)       # ...immediately recovered → never 3 in a row
    assert dt.breaker_tripped() is False


def test_breaker_default_latch_is_immediate():
    # Back-compat: with the default latch_ticks=1 the breaker trips on the tick.
    dt = DrawdownTracker(alert=0.20, cap=0.30)
    dt.update(100.0)
    dt.update(80.0)
    assert dt.breaker_tripped() is True


# ============================== PositionSizer ==============================

def test_max_position_is_pct_of_equity():
    ps = PositionSizer(equity=100.0, max_position_pct=0.10, stablecoin_floor_pct=0.20)
    assert ps.max_position_usd() == pytest.approx(10.0)


def test_deployable_respects_stablecoin_floor():
    ps = PositionSizer(equity=100.0, max_position_pct=0.10, stablecoin_floor_pct=0.20)
    # Must keep 20% in stables → at most 80% deployable to risk assets.
    assert ps.deployable_usd(current_risk_usd=0.0) == pytest.approx(80.0)
    assert ps.deployable_usd(current_risk_usd=80.0) == pytest.approx(0.0)
    assert ps.deployable_usd(current_risk_usd=100.0) == 0.0  # never negative


def test_size_for_capped_by_max_position():
    ps = PositionSizer(equity=100.0, max_position_pct=0.10, stablecoin_floor_pct=0.20)
    # Plenty deployable, fresh token → capped at the 10% per-token max.
    assert ps.size_for(current_token_usd=0.0, current_risk_usd=0.0) == pytest.approx(10.0)


def test_size_for_zero_when_token_at_cap():
    ps = PositionSizer(equity=100.0, max_position_pct=0.10, stablecoin_floor_pct=0.20)
    assert ps.size_for(current_token_usd=10.0, current_risk_usd=10.0) == 0.0


def test_size_for_limited_by_remaining_deployable():
    ps = PositionSizer(equity=100.0, max_position_pct=0.10, stablecoin_floor_pct=0.20)
    # Only $5 of deployable headroom left → size is 5, not the 10 max.
    assert ps.size_for(current_token_usd=0.0, current_risk_usd=75.0) == pytest.approx(5.0)


def test_size_for_zero_equity():
    ps = PositionSizer(equity=0.0, max_position_pct=0.10, stablecoin_floor_pct=0.20)
    assert ps.size_for(current_token_usd=0.0, current_risk_usd=0.0) == 0.0


def test_sizer_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        PositionSizer(equity=float("nan"), max_position_pct=0.10, stablecoin_floor_pct=0.20)
    with pytest.raises(ValueError):
        PositionSizer(equity=-100.0, max_position_pct=0.10, stablecoin_floor_pct=0.20)


def test_sizer_fails_safe_on_bad_size_inputs():
    ps = PositionSizer(equity=100.0, max_position_pct=0.10, stablecoin_floor_pct=0.20)
    # Adversarial/garbage inputs must yield 0, never a position.
    assert ps.size_for(current_token_usd=float("nan"), current_risk_usd=0.0) == 0.0
    assert ps.size_for(current_token_usd=-5.0, current_risk_usd=0.0) == 0.0


# ============================== TradeCounter ==============================

def _t(base: datetime, **kw) -> datetime:
    return base + timedelta(**kw)


def test_needs_trade_when_no_history():
    tc = TradeCounter(timestamps=[])
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    assert tc.needs_trade(now, interval_h=4) is True


def test_no_trade_needed_within_interval():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    tc = TradeCounter(timestamps=[_t(now, hours=-3)])
    assert tc.needs_trade(now, interval_h=4) is False


def test_needs_trade_after_interval_elapsed():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    tc = TradeCounter(timestamps=[_t(now, hours=-5)])
    assert tc.needs_trade(now, interval_h=4) is True


def test_trades_in_last_24h():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    tc = TradeCounter(timestamps=[
        _t(now, hours=-1), _t(now, hours=-10), _t(now, hours=-25),  # last one is >24h
    ])
    assert tc.trades_in_last_24h(now) == 2


def test_record_trade_appends():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    tc = TradeCounter(timestamps=[])
    tc.record_trade(now)
    assert tc.trades_in_last_24h(now) == 1
    assert tc.needs_trade(now, interval_h=4) is False


# ============================== Portfolio ==============================

def test_equity_sums_holdings_value():
    pf = Portfolio()
    equity = pf.equity(balances={"CAKE": 10.0, "USDT": 50.0}, prices={"CAKE": 2.0, "USDT": 1.0})
    assert equity == pytest.approx(70.0)


def test_equity_ignores_missing_prices_safely():
    pf = Portfolio()
    # A token with no price must not crash valuation; it contributes 0.
    equity = pf.equity(balances={"CAKE": 10.0, "WEIRD": 5.0}, prices={"CAKE": 2.0})
    assert equity == pytest.approx(20.0)


def test_stable_and_risk_value_split():
    pf = Portfolio()
    balances = {"USDT": 30.0, "CAKE": 10.0}
    prices = {"USDT": 1.0, "CAKE": 2.0}
    assert pf.stable_value(balances, prices) == pytest.approx(30.0)
    assert pf.risk_value(balances, prices) == pytest.approx(20.0)


def test_unrealized_pnl_from_cost_basis():
    pf = Portfolio()
    pf.record_fill("CAKE", amount=10.0, price=2.0)   # cost basis 2.0
    # price rose to 3.0 → unrealized pnl = (3-2)*10 = 10
    assert pf.unrealized_pnl("CAKE", current_price=3.0) == pytest.approx(10.0)


def test_cost_basis_averages_across_fills():
    pf = Portfolio()
    pf.record_fill("CAKE", amount=10.0, price=2.0)
    pf.record_fill("CAKE", amount=10.0, price=4.0)  # avg cost = 3.0, total 20
    assert pf.avg_cost("CAKE") == pytest.approx(3.0)
    assert pf.unrealized_pnl("CAKE", current_price=3.0) == pytest.approx(0.0)


def test_equity_rejects_negative_balance():
    pf = Portfolio()
    with pytest.raises(ValueError):
        pf.equity(balances={"CAKE": -1.0}, prices={"CAKE": 2.0})


# --- review fix #1: sells must adjust cost basis and realize PnL ---

def test_sell_realizes_pnl_and_reduces_holding():
    pf = Portfolio()
    pf.record_fill("CAKE", amount=10.0, price=2.0)
    realized = pf.record_sell("CAKE", amount=5.0, price=3.0)
    assert realized == pytest.approx(5.0)          # (3-2)*5
    assert pf.amount_held("CAKE") == pytest.approx(5.0)
    assert pf.avg_cost("CAKE") == pytest.approx(2.0)  # avg cost unchanged by a sale


def test_cannot_sell_more_than_held():
    pf = Portfolio()
    pf.record_fill("CAKE", amount=10.0, price=2.0)
    with pytest.raises(ValueError):
        pf.record_sell("CAKE", amount=11.0, price=3.0)


# --- review fix #3: TradeCounter must tolerate naive/aware datetime mixing ---

def test_trade_counter_handles_naive_timestamps():
    aware_now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    naive_past = datetime(2026, 6, 22, 6, 0)  # no tzinfo, from an old ledger
    tc = TradeCounter(timestamps=[naive_past])
    # Must not raise despite the naive/aware mix.
    assert tc.needs_trade(aware_now, interval_h=4) is True
    assert tc.trades_in_last_24h(aware_now) == 1


# ----------------------------- multicall balance reader (polish/optimization) -----------------------------

def test_multicall_balances_decode(mocker):
    from src.agent.risk import portfolio
    from src.agent.data.token_list import Token

    mocker.patch("src.agent.risk.portfolio.token_list.valuation_tokens", return_value=[
        Token(symbol="CAKE", contract="0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", decimals=18),
        Token(symbol="DOGE", contract="0xbA2aE424d960c26247Dd6c32edC70B295c744C43", decimals=8),
    ])
    fake_mc = mocker.Mock()
    fake_mc.functions.aggregate3.return_value.call.return_value = [
        (True, (5 * 10**17).to_bytes(32, "big")),   # native 0.5 BNB
        (True, (2 * 10**18).to_bytes(32, "big")),    # CAKE 2.0 (18 dp)
        (True, (10 * 10**8).to_bytes(32, "big")),    # DOGE 10.0 (8 dp)
    ]
    fake_w3 = mocker.Mock()
    fake_w3.eth.contract.return_value = fake_mc
    mocker.patch("src.agent.data.rpc.get_web3", return_value=fake_w3)

    out = portfolio._read_balances_multicall("0x0000000000000000000000000000000000000001", None)
    assert out["BNB"] == pytest.approx(0.5)
    assert out["CAKE"] == pytest.approx(2.0)
    assert out["DOGE"] == pytest.approx(10.0)   # honors 8-decimals


def test_multicall_omits_zero_and_failed(mocker):
    from src.agent.risk import portfolio
    from src.agent.data.token_list import Token

    mocker.patch("src.agent.risk.portfolio.token_list.valuation_tokens", return_value=[
        Token(symbol="CAKE", contract="0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", decimals=18),
        Token(symbol="ETH", contract="0x2170Ed0880ac9A755fd29B2688956BD959F933F8", decimals=18),
    ])
    fake_mc = mocker.Mock()
    fake_mc.functions.aggregate3.return_value.call.return_value = [
        (True, (0).to_bytes(32, "big")),            # native zero -> omitted
        (True, (3 * 10**18).to_bytes(32, "big")),    # CAKE 3.0
        (False, b""),                                # ETH call failed -> omitted
    ]
    fake_w3 = mocker.Mock()
    fake_w3.eth.contract.return_value = fake_mc
    mocker.patch("src.agent.data.rpc.get_web3", return_value=fake_w3)

    out = portfolio._read_balances_multicall("0x0000000000000000000000000000000000000001", None)
    assert out == {"CAKE": pytest.approx(3.0)}


def test_balance_read_values_alpha_holdings_not_just_core(mocker):
    """Regression for the phantom-drawdown false trip: a wallet holding an alpha
    token outside the trading core (LUNC) must be read, so equity == real wallet."""
    from src.agent.data.token_list import Token
    from src.agent.risk import portfolio

    lunc = Token(symbol="LUNC", contract="0x156ab3346823B651294766e23e6Cf87254d68962", decimals=6)
    mocker.patch("src.agent.risk.portfolio.token_list.valuation_tokens", return_value=[lunc])

    fake_mc = mocker.Mock()
    fake_mc.functions.aggregate3.return_value.call.return_value = [
        (True, (0).to_bytes(32, "big")),                 # native zero
        (True, (106973 * 10**6).to_bytes(32, "big")),    # LUNC 106,973 (6 dp)
    ]
    fake_w3 = mocker.Mock()
    fake_w3.eth.contract.return_value = fake_mc
    mocker.patch("src.agent.data.rpc.get_web3", return_value=fake_w3)

    out = portfolio._read_balances_multicall("0x0000000000000000000000000000000000000001", None)
    assert out == {"LUNC": pytest.approx(106973.0)}
