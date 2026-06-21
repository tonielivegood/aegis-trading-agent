"""TDD for the regime layer = the deployment valve (DQ control)."""
import json

import pytest

from src.agent.aegis.regime import (
    SENTIMENT_FEAR_FLOOR,
    Regime,
    RegimeState,
    apply_sentiment_floor,
    classify_btc,
    current_regime,
    decide_regime,
    params,
    position_usd,
)


def test_decide_regime_from_cmc_quote():
    # CMC reports percent (e.g. -9.0 == -9%)
    flag, reason = decide_regime({"percent_change_1h": -1.0, "percent_change_24h": -9.0})
    assert flag == Regime.RISK_OFF
    assert "24h" in reason
    flag, _ = decide_regime({"percent_change_1h": 0.3, "percent_change_24h": 1.5})
    assert flag == Regime.RISK_ON
    flag, _ = decide_regime({"percent_change_1h": None, "percent_change_24h": -4.0})
    assert flag == Regime.CAUTIOUS


def test_sentiment_floor_only_tightens_risk_on():
    # CMC Agent Hub Fear & Greed: extreme fear caps aggression (RISK_ON -> CAUTIOUS).
    flag, why = apply_sentiment_floor(Regime.RISK_ON, SENTIMENT_FEAR_FLOOR - 1)
    assert flag == Regime.CAUTIOUS and "F&G" in why


def test_sentiment_floor_never_loosens():
    # It must NEVER upgrade a regime — greed cannot make us aggressive (overpump risk),
    # and a non-RISK_ON regime is left untouched.
    assert apply_sentiment_floor(Regime.RISK_ON, 95)[0] == Regime.RISK_ON
    assert apply_sentiment_floor(Regime.CAUTIOUS, 5)[0] == Regime.CAUTIOUS
    assert apply_sentiment_floor(Regime.RISK_OFF, 5)[0] == Regime.RISK_OFF


def test_sentiment_floor_noop_without_data():
    # No Agent Hub read available => behaviour is identical to BTC-only classification.
    flag, why = apply_sentiment_floor(Regime.RISK_ON, None)
    assert flag == Regime.RISK_ON and why == ""


def test_decide_regime_applies_fear_greed_overlay():
    # BTC calm => RISK_ON, but extreme market fear tightens it to CAUTIOUS.
    quote = {"percent_change_1h": 0.3, "percent_change_24h": 1.5}
    flag, reason = decide_regime(quote, fear_greed={"value": 10})
    assert flag == Regime.CAUTIOUS and "F&G" in reason
    # Calm BTC + neutral sentiment stays RISK_ON.
    flag, _ = decide_regime(quote, fear_greed={"value": 55})
    assert flag == Regime.RISK_ON


def test_params_per_regime():
    # Concentrated sizing: few, heavy positions (winners must move the needle).
    assert params(Regime.RISK_ON).size_pct == 0.35
    assert params(Regime.RISK_ON).max_slots == 2
    assert params(Regime.RISK_ON).allow_new is True
    assert params(Regime.CAUTIOUS).size_pct == 0.20
    assert params(Regime.CAUTIOUS).max_slots == 1
    assert params(Regime.RISK_OFF).size_pct == 0.0
    assert params(Regime.RISK_OFF).max_slots == 0
    assert params(Regime.RISK_OFF).allow_new is False
    # RISK_ON total deployment stays under 100% NAV (DQ cushion).
    assert params(Regime.RISK_ON).size_pct * params(Regime.RISK_ON).max_slots <= 0.75
    # Regime throttles EXPOSURE (size/slots), not signal quality: the entry bar is
    # full (1.0) in every regime — the old RISK_ON beta-valve was removed after it
    # over-fired live (churn bleed). RISK_ON still risks more via bigger size + 2 slots.
    assert params(Regime.RISK_ON).entry_vol_factor == 1.0
    assert params(Regime.CAUTIOUS).entry_vol_factor == 1.0


def test_position_usd_scales_with_nav():
    assert position_usd(30.0, Regime.RISK_ON) == pytest.approx(10.5)    # 35%
    assert position_usd(30.0, Regime.CAUTIOUS) == pytest.approx(6.0)    # 20%
    assert position_usd(30.0, Regime.RISK_OFF) == 0.0                   # halt


def test_classify_dump_is_risk_off():
    assert classify_btc(change_1h=-0.05, change_24h=-0.02) == Regime.RISK_OFF   # 1h crash
    assert classify_btc(change_1h=-0.01, change_24h=-0.10) == Regime.RISK_OFF   # 24h crash


def test_classify_choppy_is_cautious():
    assert classify_btc(change_1h=0.0, change_24h=-0.04) == Regime.CAUTIOUS     # mild down
    assert classify_btc(change_1h=0.03, change_24h=0.01) == Regime.CAUTIOUS     # choppy 1h


def test_classify_calm_up_is_risk_on():
    assert classify_btc(change_1h=0.005, change_24h=0.02) == Regime.RISK_ON


def test_regime_state_round_trip(tmp_path):
    p = tmp_path / "regime.json"
    RegimeState(flag=Regime.RISK_ON.value, updated_at=123.0, reason="calm").save(p)
    loaded = RegimeState.load(p)
    assert loaded.flag == Regime.RISK_ON.value
    assert loaded.updated_at == 123.0
    assert json.loads(p.read_text())["reason"] == "calm"


def test_stale_regime_falls_back_to_cautious():
    # a fresh RISK_ON read is honoured...
    st = RegimeState(flag=Regime.RISK_ON.value, updated_at=1000.0)
    assert current_regime(st, max_age_s=3600, now=1500.0) == Regime.RISK_ON
    # ...but a stale read (updater dead) must NOT keep us aggressive → CAUTIOUS
    assert current_regime(st, max_age_s=3600, now=1000.0 + 4000) == Regime.CAUTIOUS


def test_cold_start_defaults_cautious():
    assert RegimeState().flag == Regime.CAUTIOUS.value
