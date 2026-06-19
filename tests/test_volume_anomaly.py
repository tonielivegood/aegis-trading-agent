"""5-minute market-confirmation tests. Pure; snapshots supplied directly."""
from __future__ import annotations

from src.agent.aegis.volume_anomaly_detector import (
    P_BAD_LIQUIDITY,
    P_OVERPUMPED,
    W_BREAKOUT,
    W_LIQUIDITY,
    W_VOL_SPIKE,
    MarketSnapshot,
    assess,
)


def _snap(**kw):
    base = dict(symbol="FOO", contract="0x1", vol_5m=0.0, baseline_vol=0.0,
                price_now=1.0, price_5m_ago=1.0, recent_pump_pct=0.0,
                slippage_est=0.0, has_route=True, liquidity_ok=True)
    base.update(kw)
    return MarketSnapshot(**base)


def test_full_confirmation_when_spike_breakout_and_liquid():
    snap = _snap(vol_5m=400, baseline_vol=100, price_now=1.03, price_5m_ago=1.0)
    c = assess(snap, vol_spike_mult=3.0, breakout_pct=0.015, max_slippage=0.05)
    assert c.confirmation_score == W_VOL_SPIKE + W_BREAKOUT + W_LIQUIDITY
    assert c.risk_penalty == 0


def test_liquidity_only_when_no_spike_no_breakout():
    snap = _snap(vol_5m=100, baseline_vol=100, price_now=1.0, price_5m_ago=1.0)
    c = assess(snap, max_slippage=0.05)
    assert c.confirmation_score == W_LIQUIDITY and c.risk_penalty == 0


def test_overpumped_adds_risk_penalty():
    snap = _snap(vol_5m=400, baseline_vol=100, price_now=1.03, price_5m_ago=1.0,
                 recent_pump_pct=0.25)
    c = assess(snap, overpump_pct=0.15, max_slippage=0.05)
    assert c.risk_penalty == P_OVERPUMPED


def test_bad_liquidity_is_disqualifying():
    snap = _snap(has_route=False, vol_5m=999, baseline_vol=1, price_now=2.0, price_5m_ago=1.0)
    c = assess(snap, max_slippage=0.05)
    assert c.confirmation_score == 0 and c.risk_penalty == P_BAD_LIQUIDITY


def test_high_slippage_is_disqualifying():
    snap = _snap(slippage_est=0.20)
    c = assess(snap, max_slippage=0.05)
    assert c.risk_penalty == P_BAD_LIQUIDITY


def test_net_property():
    snap = _snap(vol_5m=400, baseline_vol=100, price_now=1.03, price_5m_ago=1.0,
                 recent_pump_pct=0.25)
    c = assess(snap, overpump_pct=0.15, max_slippage=0.05)
    assert c.net == c.confirmation_score - c.risk_penalty
