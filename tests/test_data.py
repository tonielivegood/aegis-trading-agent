"""Tests for the data layer. Network calls are mocked except opt-in smoke tests."""
from __future__ import annotations

import os

import pytest

from src.agent.data import cmc_client, price_feed, token_list


# ---------- token_list ----------

def test_curated_core_loads():
    toks = token_list.tradable_tokens()
    assert len(toks) >= 15
    syms = token_list.tradable_symbols()
    assert "WBNB" in syms and "USDT" in syms and "CAKE" in syms


def test_token_addresses_checksummed():
    for t in token_list.tradable_tokens():
        # .address must be valid checksum (raises if not)
        assert t.address.startswith("0x") and len(t.address) == 42


def test_doge_decimals():
    assert token_list.get_token("DOGE").decimals == 8
    assert token_list.get_token("WBNB").decimals == 18


def test_stablecoins_flagged():
    stables = {t.symbol for t in token_list.stablecoins()}
    assert {"USDT", "USDC"} <= stables
    assert token_list.get_token("USDT").is_stable
    assert not token_list.get_token("CAKE").is_stable


def test_unknown_token_raises():
    with pytest.raises(KeyError):
        token_list.get_token("NOTAREALTOKEN")


def test_eligibility_check():
    # Eligibility is strict to the official allowlist (matched by address).
    # TWT is in the official 149; a random address is not.
    twt = token_list.get_token("TWT").contract
    assert token_list.is_eligible(twt)
    assert not token_list.is_eligible("0x000000000000000000000000000000000000dead")


# ---------- cmc_client (mocked) ----------

def test_get_quotes_parses_and_caches(mocker):
    fake = {
        "data": {
            "CAKE": [{"quote": {"USD": {
                "price": 2.5, "volume_24h": 1e7,
                "percent_change_1h": 0.5, "percent_change_24h": -1.2, "percent_change_7d": 3.0,
            }}}]
        }
    }
    resp = mocker.Mock()
    resp.json.return_value = fake
    resp.raise_for_status.return_value = None
    get = mocker.patch("src.agent.data.cmc_client.requests.get", return_value=resp)

    cmc_client._CACHE.clear()
    q1 = cmc_client.get_quotes(["CAKE"])
    cmc_client.get_quotes(["CAKE"])  # served from cache — second call must not hit network

    assert q1["CAKE"]["price"] == 2.5
    assert q1["CAKE"]["percent_change_24h"] == -1.2
    assert get.call_count == 1  # cached second time


# ---------- price_feed (mocked) ----------

def test_get_prices_uses_onchain_first(mocker):
    # On-chain is the primary source for valuation.
    mocker.patch("src.agent.data.price_feed.onchain_price_usd",
                 side_effect=lambda s: {"CAKE": 2.5, "BTCB": 51000.0}[s])
    cmc = mocker.patch("src.agent.data.price_feed.cmc_client.get_quotes")
    prices = price_feed.get_prices(["CAKE", "BTCB"])
    assert prices["CAKE"] == 2.5
    assert prices["BTCB"] == 51000.0
    cmc.assert_not_called()  # no need to hit CMC when on-chain succeeds


def test_get_prices_falls_back_to_cmc(mocker):
    # No on-chain route → fall back to CMC.
    mocker.patch("src.agent.data.price_feed.onchain_price_usd", return_value=None)
    mocker.patch("src.agent.data.price_feed.cmc_client.get_quotes",
                 return_value={"CAKE": {"price": 2.42}})
    prices = price_feed.get_prices(["CAKE"])
    assert prices["CAKE"] == 2.42


def test_stablecoin_onchain_price_is_one():
    # No network needed: stablecoins short-circuit to 1.0
    assert price_feed.onchain_price_usd("USDT") == 1.0


def test_onchain_price_none_when_no_route(mocker):
    # polish: a token with no PancakeSwap route must return None, not crash.
    fake = mocker.Mock()
    fake.functions.getAmountsOut.return_value.call.side_effect = Exception("no route")
    mocker.patch("src.agent.data.price_feed._router", return_value=fake)
    assert price_feed.onchain_price_usd("CAKE") is None


def test_bnb_aliases_to_wbnb_price(mocker):
    # review fix #2: native "BNB" must price via WBNB, not KeyError/None.
    fake_router = mocker.Mock()
    fake_router.functions.getAmountsOut.return_value.call.return_value = [10**18, 600 * 10**18]
    mocker.patch("src.agent.data.price_feed._router", return_value=fake_router)
    # Without the alias, get_token("BNB") would raise KeyError.
    price = price_feed.onchain_price_usd("BNB")
    assert price == pytest.approx(600.0)


# ---------- live smoke tests (opt-in) ----------

@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 for live smoke tests")
def test_live_onchain_price():
    p = price_feed.onchain_price_usd("WBNB")
    assert p and p > 0


@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 for live smoke tests")
def test_live_cmc_price():
    prices = price_feed.get_prices(["CAKE", "BTCB"])
    assert prices.get("CAKE", 0) > 0
