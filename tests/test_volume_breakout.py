"""TDD for the volume-breakout signal generator (v2 sniper primary trigger)."""
from src.agent.aegis.volume_breakout import (
    BreakoutSignal,
    decide_breakout_entries,
    scan_breakouts,
)
from src.agent.aegis.volume_anomaly_detector import MarketSnapshot
from src.agent.aegis.positions import OpenPosition, PositionBook
from src.agent.strategy.base_strategy import PortfolioState


def _snap(symbol, *, vol_5m, baseline_vol, price_now, price_5m_ago, recent_pump_pct=0.0,
          slippage_est=0.01, has_route=True, liquidity_ok=True, contract="0xabc"):
    return MarketSnapshot(
        symbol=symbol, contract=contract, vol_5m=vol_5m, baseline_vol=baseline_vol,
        price_now=price_now, price_5m_ago=price_5m_ago, recent_pump_pct=recent_pump_pct,
        slippage_est=slippage_est, has_route=has_route, liquidity_ok=liquidity_ok)


def test_clean_breakout_passes():
    snaps = {"AAA": _snap("AAA", vol_5m=350, baseline_vol=100,
                          price_now=105, price_5m_ago=100, recent_pump_pct=0.06)}
    sigs = scan_breakouts(snaps, vol_mult=3.0, breakout_max=0.10, overpump_pct=0.10)
    assert len(sigs) == 1
    s = sigs[0]
    assert isinstance(s, BreakoutSignal)
    assert s.symbol == "AAA"
    assert s.vol_multiple == 3.5
    assert abs(s.breakout_pct - 0.05) < 1e-9
    assert s.contract == "0xabc"


def test_low_volume_rejected():
    snaps = {"AAA": _snap("AAA", vol_5m=200, baseline_vol=100, price_now=105, price_5m_ago=100)}
    assert scan_breakouts(snaps, vol_mult=3.0) == []


def test_already_pumped_past_cap_rejected():
    # +15% breakout exceeds breakout_max 10% — don't chase a blow-off
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=100, price_now=115, price_5m_ago=100)}
    assert scan_breakouts(snaps, breakout_max=0.10) == []


def test_recent_pump_window_rejected():
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=100, price_now=105,
                          price_5m_ago=100, recent_pump_pct=0.12)}
    assert scan_breakouts(snaps, overpump_pct=0.10) == []


def test_bad_liquidity_rejected():
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=100, price_now=105,
                          price_5m_ago=100, liquidity_ok=False)}
    assert scan_breakouts(snaps) == []


def test_falling_price_rejected():
    # high volume but price DOWN = a dump on volume, not a breakout
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=100, price_now=95, price_5m_ago=100)}
    assert scan_breakouts(snaps) == []


def test_flat_price_rejected():
    # volume spike but price flat — no breakout (possible distribution); never enter
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=100, price_now=100, price_5m_ago=100)}
    assert scan_breakouts(snaps) == []


def test_zero_baseline_is_failsafe():
    # no real volume data (baseline 0) must never fire
    snaps = {"AAA": _snap("AAA", vol_5m=400, baseline_vol=0, price_now=105, price_5m_ago=100)}
    assert scan_breakouts(snaps) == []


def test_ranked_by_volume_multiple_desc():
    snaps = {
        "AAA": _snap("AAA", vol_5m=300, baseline_vol=100, price_now=103, price_5m_ago=100),  # 3.0x
        "BBB": _snap("BBB", vol_5m=500, baseline_vol=100, price_now=104, price_5m_ago=100),  # 5.0x
        "CCC": _snap("CCC", vol_5m=400, baseline_vol=100, price_now=102, price_5m_ago=100),  # 4.0x
    }
    sigs = scan_breakouts(snaps, vol_mult=3.0)
    assert [s.symbol for s in sigs] == ["BBB", "CCC", "AAA"]


# ----------------------------- decide_breakout_entries -----------------------------

def _sig(symbol, vm=4.0, contract="0xaaa"):
    return BreakoutSignal(symbol=symbol, contract=contract, vol_multiple=vm, breakout_pct=0.05,
                          recent_pump_pct=0.0, slippage_est=0.01, price_now=1.0, baseline_vol=100.0)


def _state(equity=30.0, stable=30.0, risk=0.0):
    return PortfolioState(equity_usd=equity, risk_value_usd=risk, stable_value_usd=stable)


def _allow_all(_contract):
    return True


def test_entry_emits_order_for_signal():
    orders = decide_breakout_entries(
        [_sig("AAA")], _state(), PositionBook(),
        position_usd=6.0, max_positions=3, floor_usd=6.0, allow=_allow_all)
    assert len(orders) == 1
    assert (orders[0].token_in, orders[0].token_out, orders[0].amount_in_usd) == ("USDT", "AAA", 6.0)


def test_risk_off_zero_size_blocks_entries():
    orders = decide_breakout_entries(
        [_sig("AAA")], _state(), PositionBook(),
        position_usd=0.0, max_positions=0, floor_usd=6.0, allow=_allow_all)
    assert orders == []


def test_breaker_blocks_entries():
    st = _state()
    st.drawdown_tripped = True
    orders = decide_breakout_entries(
        [_sig("AAA")], st, PositionBook(),
        position_usd=6.0, max_positions=3, floor_usd=6.0, allow=_allow_all)
    assert orders == []


def test_cooldown_symbol_skipped():
    orders = decide_breakout_entries(
        [_sig("AAA")], _state(), PositionBook(),
        position_usd=6.0, max_positions=3, floor_usd=6.0,
        cooldown_symbols={"AAA"}, allow=_allow_all)
    assert orders == []


def test_no_pyramiding_into_open_position():
    book = PositionBook()
    book.open(OpenPosition(symbol="AAA", contract="0xaaa", entry_price=1.0, usd_size=6.0))
    orders = decide_breakout_entries(
        [_sig("AAA")], _state(), book,
        position_usd=6.0, max_positions=3, floor_usd=6.0, allow=_allow_all)
    assert orders == []


def test_ineligible_token_skipped():
    orders = decide_breakout_entries(
        [_sig("AAA")], _state(), PositionBook(),
        position_usd=6.0, max_positions=3, floor_usd=6.0, allow=lambda c: False)
    assert orders == []


def test_slot_cap_limits_entries_to_strongest():
    sigs = [_sig("BBB", vm=5.0, contract="0xbbb"), _sig("CCC", vm=4.0, contract="0xccc"),
            _sig("AAA", vm=3.0, contract="0xaaa")]
    orders = decide_breakout_entries(
        sigs, _state(equity=60, stable=60), PositionBook(),
        position_usd=6.0, max_positions=2, floor_usd=6.0, allow=_allow_all)
    assert [o.token_out for o in orders] == ["BBB", "CCC"]


def test_stablecoin_floor_stops_entries():
    # $30 stable, floor $6, size $10 → only 2 entries fit (30-20=10 ok, third would breach)
    sigs = [_sig("BBB", vm=5.0, contract="0xbbb"), _sig("CCC", vm=4.0, contract="0xccc"),
            _sig("AAA", vm=3.0, contract="0xaaa")]
    orders = decide_breakout_entries(
        sigs, _state(equity=30, stable=30), PositionBook(),
        position_usd=10.0, max_positions=3, floor_usd=6.0, allow=_allow_all)
    assert [o.token_out for o in orders] == ["BBB", "CCC"]


def test_dust_size_blocked():
    orders = decide_breakout_entries(
        [_sig("AAA")], _state(), PositionBook(),
        position_usd=1.0, max_positions=3, floor_usd=6.0, allow=_allow_all)
    assert orders == []
