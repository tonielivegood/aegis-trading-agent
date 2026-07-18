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


@patch("src.agent.copy_trade.prices.requests.get")
def test_pair_stats_includes_txns_and_change(get_mock):
    p = _pair(liq=100, price="1.0", created=1)
    p["txns"] = {"m5": {"buys": 7, "sells": 3}, "h1": {"buys": 70, "sells": 30}}
    p["priceChange"] = {"m5": -2.5, "h1": 36.4}
    get_mock.return_value = _resp([p])
    s = prices.get_pair_stats(TOKEN)
    assert s["txns_h1_buys"] == 70 and s["txns_h1_sells"] == 30
    assert s["txns_m5_buys"] == 7 and s["txns_m5_sells"] == 3
    assert s["price_change_m5"] == -2.5 and s["price_change_h1"] == 36.4
    prices._pairs_cache.clear()
    get_mock.return_value = _resp([_pair(liq=100)])       # fields absent
    s = prices.get_pair_stats(TOKEN)
    assert s["txns_h1_buys"] == 0 and s["price_change_m5"] is None


@patch("src.agent.copy_trade.prices.requests.get")
def test_holder_stats_excludes_lp_dead_locked(get_mock):
    r = MagicMock(); r.raise_for_status.return_value = None
    r.json.return_value = {"result": {TOKEN: {"holder_count": "105", "holders": [
        {"address": "0xpool", "tag": "PancakeV2", "percent": "0.93", "is_locked": 0},
        {"address": "0x000000000000000000000000000000000000dead", "tag": "",
         "percent": "0.05", "is_locked": 1},
        {"address": "0xwhale", "tag": "", "percent": "0.205", "is_locked": 0},
        {"address": "0xsmall1", "tag": "", "percent": "0.03", "is_locked": 0},
        {"address": "0xsmall2", "tag": "", "percent": "0.02", "is_locked": 0},
    ]}}}
    get_mock.return_value = r
    prices._holder_cache.clear()
    h = prices.get_holder_stats(TOKEN)
    assert h["holder_count"] == 105
    assert h["top_pct"] == 0.205                          # whale, not the LP pool
    assert abs(h["top5_pct"] - 0.255) < 1e-9
    assert prices.get_holder_stats(TOKEN) == h and get_mock.call_count == 1  # cached


@patch("src.agent.copy_trade.prices.requests.get", side_effect=RuntimeError("down"))
def test_holder_stats_failure_returns_none_never_cached(get_mock):
    prices._holder_cache.clear()
    assert prices.get_holder_stats(TOKEN) is None
    assert prices._holder_cache == {}
