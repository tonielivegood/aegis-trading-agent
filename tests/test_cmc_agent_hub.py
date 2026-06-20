"""TDD for the CMC AI Agent Hub client — sentiment + community trending (mocked HTTP)."""
from __future__ import annotations

import pytest

from src.agent.data import cmc_agent_hub as hub


@pytest.fixture(autouse=True)
def _clear_cache():
    hub._CACHE.clear()
    yield
    hub._CACHE.clear()


def _resp(mocker, body):
    r = mocker.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = body
    return r


# --- Fear & Greed (market sentiment) ---

def test_fear_greed_parses_value_and_classification(mocker):
    mocker.patch("src.agent.data.cmc_agent_hub.requests.get", return_value=_resp(
        mocker, {"data": {"value": 22, "value_classification": "Fear",
                          "update_time": "2026-06-20T22:38:10.030Z"}}))
    fng = hub.get_fear_greed()
    assert fng == {"value": 22, "classification": "Fear"}


def test_fear_greed_is_cached(mocker):
    get = mocker.patch("src.agent.data.cmc_agent_hub.requests.get", return_value=_resp(
        mocker, {"data": {"value": 50, "value_classification": "Neutral"}}))
    hub.get_fear_greed()
    hub.get_fear_greed()
    assert get.call_count == 1  # second read served from cache


def test_fear_greed_fails_safe_on_error(mocker):
    mocker.patch("src.agent.data.cmc_agent_hub.requests.get",
                 side_effect=RuntimeError("boom"))
    assert hub.get_fear_greed() is None  # never raises into the updater


def test_fear_greed_missing_value_returns_none(mocker):
    mocker.patch("src.agent.data.cmc_agent_hub.requests.get",
                 return_value=_resp(mocker, {"data": {}}))
    assert hub.get_fear_greed() is None


# --- Community trending (token selection bias) ---

def test_trending_returns_uppercase_symbol_set(mocker):
    mocker.patch("src.agent.data.cmc_agent_hub.requests.get", return_value=_resp(
        mocker, {"data": [{"symbol": "siren"}, {"symbol": "DOGE"}, {"name": "no symbol"}]}))
    syms = hub.get_trending_symbols()
    assert syms == frozenset({"SIREN", "DOGE"})


def test_trending_fails_safe_to_empty(mocker):
    mocker.patch("src.agent.data.cmc_agent_hub.requests.get",
                 side_effect=RuntimeError("boom"))
    assert hub.get_trending_symbols() == frozenset()
