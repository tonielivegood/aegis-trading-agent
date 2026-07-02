"""TDD for the CoinGecko fallback client — mocked HTTP, no network."""
from __future__ import annotations

from src.agent.data import coingecko_client as cg


def _resp(mocker, body):
    r = mocker.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = body
    return r


def test_get_quotes_parses_known_symbol(mocker):
    fake = [{
        "id": "bitcoin", "current_price": 65000.0,
        "price_change_percentage_1h_in_currency": 0.4,
        "price_change_percentage_24h_in_currency": -2.1,
    }]
    get = mocker.patch("src.agent.data.coingecko_client.requests.get",
                       return_value=_resp(mocker, fake))
    q = cg.get_quotes(["BTC"])
    assert q["BTC"]["price"] == 65000.0
    assert q["BTC"]["percent_change_24h"] == -2.1
    assert get.call_args.kwargs["params"]["ids"] == "bitcoin"


def test_unknown_symbol_never_calls_network(mocker):
    get = mocker.patch("src.agent.data.coingecko_client.requests.get")
    assert cg.get_quotes(["SOME_OBSCURE_MEME"]) == {}
    get.assert_not_called()


def test_network_failure_returns_empty_never_raises(mocker):
    import requests
    mocker.patch("src.agent.data.coingecko_client.requests.get",
                side_effect=requests.ConnectionError("boom"))
    assert cg.get_quotes(["BTC"]) == {}


def test_mixed_known_and_unknown_symbols(mocker):
    fake = [{"id": "ethereum", "current_price": 3000.0,
            "price_change_percentage_1h_in_currency": 0.1,
            "price_change_percentage_24h_in_currency": 1.0}]
    mocker.patch("src.agent.data.coingecko_client.requests.get",
                return_value=_resp(mocker, fake))
    q = cg.get_quotes(["ETH", "SOME_OBSCURE_MEME"])
    assert "ETH" in q and "SOME_OBSCURE_MEME" not in q
