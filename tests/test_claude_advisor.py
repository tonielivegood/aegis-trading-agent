"""TDD for the Claude regime advisor — tightening-only, fail-safe, mocked SDK."""
from __future__ import annotations

import pytest

from src.agent.aegis import claude_advisor as ca
from src.agent.aegis.regime import Regime

BTC = {"percent_change_1h": 0.2, "percent_change_24h": 1.5}


def _mock_reply(mocker, text):
    block = mocker.Mock()
    block.type = "text"
    block.text = text
    resp = mocker.Mock()
    resp.content = [block]
    client = mocker.Mock()
    client.messages.create.return_value = resp
    mocker.patch("src.agent.aegis.claude_advisor.anthropic.Anthropic", return_value=client)


@pytest.fixture(autouse=True)
def _enable(mocker):
    mocker.patch.object(ca.settings, "claude_advisor_enabled", True)
    mocker.patch.object(ca.settings, "anthropic_api_key", "test-key")


def test_claude_can_tighten_risk_on_to_cautious(mocker):
    _mock_reply(mocker, "CAUTIOUS\nBTC momentum is fading into resistance.")
    eff, rec, reason = ca.advise_regime(Regime.RISK_ON, btc_quote=BTC, fear_greed={"value": 40})
    assert eff == Regime.CAUTIOUS and rec == "cautious" and "BTC" in reason


def test_claude_can_never_loosen(mocker):
    # Base is CAUTIOUS; Claude says RISK_ON — the code must NOT upgrade.
    _mock_reply(mocker, "RISK_ON\nLooks bullish.")
    eff, rec, _ = ca.advise_regime(Regime.CAUTIOUS, btc_quote=BTC, fear_greed={"value": 70})
    assert eff == Regime.CAUTIOUS          # stays defensive
    assert rec == "risk_on"                # the raw recommendation is surfaced, but not applied


def test_claude_can_tighten_to_risk_off(mocker):
    _mock_reply(mocker, "RISK_OFF\nMarket is dumping.")
    eff, _, _ = ca.advise_regime(Regime.RISK_ON, btc_quote=BTC, fear_greed={"value": 10})
    assert eff == Regime.RISK_OFF


def test_fail_safe_on_api_error(mocker):
    mocker.patch("src.agent.aegis.claude_advisor.anthropic.Anthropic",
                 side_effect=RuntimeError("boom"))
    eff, rec, reason = ca.advise_regime(Regime.RISK_ON, btc_quote=BTC, fear_greed={"value": 40})
    assert eff == Regime.RISK_ON and rec == "" and reason == ""


def test_fail_safe_on_garbage_reply(mocker):
    _mock_reply(mocker, "I'm not sure, the market is complicated.")
    eff, rec, _ = ca.advise_regime(Regime.RISK_ON, btc_quote=BTC, fear_greed={"value": 40})
    assert eff == Regime.RISK_ON and rec == ""


def test_disabled_or_no_key_is_noop(mocker):
    mocker.patch.object(ca.settings, "claude_advisor_enabled", False)
    spy = mocker.patch("src.agent.aegis.claude_advisor.anthropic.Anthropic")
    eff, _, _ = ca.advise_regime(Regime.RISK_ON, btc_quote=BTC, fear_greed=None)
    assert eff == Regime.RISK_ON
    spy.assert_not_called()
