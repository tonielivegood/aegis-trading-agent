"""get_pair_stats: the gem filter's data source. Critical: earliest pairCreatedAt
across pairs (DexScreener omits it on the best pair sometimes), marketCap→fdv
fallback, one cached HTTP fetch shared with get_price_usd."""
from unittest.mock import MagicMock, patch

import src.agent.copy_trade.prices as prices

TOKEN = "0x" + "a" * 40


def _resp(pairs):
    r = MagicMock()
    r.json.return_value = {"pairs": pairs}
    r.raise_for_status.return_value = None
    return r


def _pair(liq, price="1.0", created=None, mcap=None, fdv=None, addr="0x" + "b" * 40):
    p = {"chainId": "bsc", "priceUsd": price, "liquidity": {"usd": liq},
         "pairAddress": addr}
    if created is not None:
        p["pairCreatedAt"] = created
    if mcap is not None:
        p["marketCap"] = mcap
    if fdv is not None:
        p["fdv"] = fdv
    return p


def setup_function(_):
    prices._pairs_cache.clear()


@patch("src.agent.copy_trade.prices.requests.get")
def test_stats_uses_best_pair_but_earliest_created_at(get_mock):
    get_mock.return_value = _resp([
        _pair(liq=900_000, price="2.0", created=None, mcap=1_000_000),  # best liq, NO createdAt
        _pair(liq=10_000, price="2.1", created=1752000000000),          # small pair HAS it
    ])
    s = prices.get_pair_stats(TOKEN)
    assert s["price_usd"] == 2.0                    # from best-liquidity pair
    assert s["market_cap_usd"] == 1_000_000.0
    assert s["pair_created_at_ms"] == 1752000000000  # min across ALL pairs
    assert s["liquidity_usd"] == 900_000.0


@patch("src.agent.copy_trade.prices.requests.get")
def test_stats_mcap_falls_back_to_fdv_then_none(get_mock):
    get_mock.return_value = _resp([_pair(liq=100, fdv=555.0)])
    assert prices.get_pair_stats(TOKEN)["market_cap_usd"] == 555.0
    prices._pairs_cache.clear()
    get_mock.return_value = _resp([_pair(liq=100)])
    assert prices.get_pair_stats(TOKEN)["market_cap_usd"] is None


@patch("src.agent.copy_trade.prices.requests.get")
def test_stats_and_price_share_one_cached_fetch(get_mock):
    get_mock.return_value = _resp([_pair(liq=100, price="3.0", created=1)])
    assert prices.get_price_usd(TOKEN) == 3.0
    assert prices.get_pair_stats(TOKEN)["price_usd"] == 3.0
    assert get_mock.call_count == 1                 # second call served from cache


@patch("src.agent.copy_trade.prices.requests.get", side_effect=RuntimeError("down"))
def test_stats_returns_none_on_network_failure_and_never_caches(get_mock):
    assert prices.get_pair_stats(TOKEN) is None
    assert prices.get_price_usd(TOKEN) is None
    assert prices._pairs_cache == {}                # failures never cached


@patch("src.agent.copy_trade.prices.requests.get")
def test_no_bsc_pairs_returns_none(get_mock):
    get_mock.return_value = _resp([{"chainId": "ethereum", "priceUsd": "1"}])
    assert prices.get_pair_stats(TOKEN) is None
