"""TDD for the `signals` CLI command — demonstrates the CMC AI Agent Hub integration."""
from __future__ import annotations

from src.agent import __main__ as cli


def test_signals_prints_agent_hub_data_and_regime(mocker, capsys):
    mocker.patch("src.agent.data.cmc_agent_hub.get_fear_greed",
                 return_value={"value": 12, "classification": "Extreme fear"})
    mocker.patch("src.agent.data.cmc_agent_hub.get_trending_symbols",
                 return_value=frozenset({"SOL", "BTC"}))
    # Calm BTC alone would be RISK_ON; extreme fear must tighten it to CAUTIOUS.
    mocker.patch("src.agent.data.cmc_client.get_quotes",
                 return_value={"BTC": {"percent_change_1h": 0.2, "percent_change_24h": 1.0}})
    mocker.patch("src.agent.data.cmc_skill_hub.list_skills",
                 return_value=["trending_crypto_narratives", "get_crypto_metrics"])
    mocker.patch("src.agent.data.cmc_skill_hub.trending_narratives",
                 return_value=[{"rank": 1, "name": "AI", "market_cap": "100 B",
                                "change_24h": "+3%", "top_coins": ["FET", "TAO"], "social_keywords": ["AI"]}])

    cli.cmd_signals()
    out = capsys.readouterr().out

    assert "Agent Hub" in out
    assert "12" in out and "Extreme fear" in out         # Fear & Greed surfaced
    assert "BTC" in out and "SOL" in out                  # trending surfaced
    assert "cautious" in out.lower()                       # tightening applied + shown
    assert "Skill Hub" in out and "trending_crypto_narratives" in out  # MCP skill showcased


def test_signals_fails_safe_when_hub_unavailable(mocker, capsys):
    mocker.patch("src.agent.data.cmc_agent_hub.get_fear_greed", return_value=None)
    mocker.patch("src.agent.data.cmc_agent_hub.get_trending_symbols", return_value=frozenset())
    mocker.patch("src.agent.data.cmc_client.get_quotes",
                 return_value={"BTC": {"percent_change_1h": 0.2, "percent_change_24h": 1.0}})
    mocker.patch("src.agent.data.cmc_skill_hub.list_skills", return_value=[])
    mocker.patch("src.agent.data.cmc_skill_hub.trending_narratives", return_value=[])

    cli.cmd_signals()  # must not raise when the Agent Hub is unreachable
    out = capsys.readouterr().out
    assert "unavailable" in out.lower()
    assert "risk_on" in out.lower()  # no sentiment read => BTC-only regime stands
