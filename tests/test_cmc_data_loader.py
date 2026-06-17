"""CMC OHLCV historical loader tests — written test-first (TDD).

This loader feeds the walk-forward backtest from CoinMarketCap (the SAME price
source the contest uses to score), so it must:
  - parse the /v2 ohlcv/historical response defensively,
  - align multiple symbols onto a common timestamp grid (no ragged series),
  - map our BSC-pegged symbols (WBNB->BNB, BTCB->BTC) to canonical CMC history,
  - never crash the whole run on one bad symbol/bar,
  - clamp request size and reject unknown intervals (no arbitrary params),
  - cache per symbol so re-runs don't re-hit the API.
"""
from __future__ import annotations

import pytest

from src.agent.backtest import cmc_data_loader as cdl


# --- helpers ---------------------------------------------------------------

def _quote(ts_iso: str, close: float) -> dict:
    return {"time_open": ts_iso, "quote": {"USD": {"close": close}}}


def _coin(quotes: list[dict]) -> list:
    # /v2 keys each symbol to a LIST of coin objects; we use the first.
    return [{"quotes": quotes}]


def _resp(mocker, data: dict):
    m = mocker.Mock()
    m.json.return_value = {"data": data}
    m.raise_for_status.return_value = None
    return m


# --- parsing & alignment ---------------------------------------------------

def test_parses_and_aligns_on_common_timestamps(mocker):
    data = {
        "CAKE": _coin([
            _quote("2024-01-01T00:00:00.000Z", 2.0),
            _quote("2024-01-01T01:00:00.000Z", 2.1),
            _quote("2024-01-01T02:00:00.000Z", 2.2),
        ]),
        "XRP": _coin([
            _quote("2024-01-01T01:00:00.000Z", 0.5),
            _quote("2024-01-01T02:00:00.000Z", 0.55),
            _quote("2024-01-01T03:00:00.000Z", 0.6),  # no CAKE bar here
        ]),
    }
    get = mocker.patch.object(cdl.requests, "get", return_value=_resp(mocker, data))

    # count=100 -> both symbols fit one chunk (100*2 <= 10000).
    series, times = cdl.load_history_cmc(["CAKE", "XRP"], count=100, use_cache=False)

    assert get.call_count == 1                     # one batched request
    assert len(times) == 2                         # intersection: 01:00, 02:00
    assert times == sorted(times)                  # chronological
    assert series["CAKE"] == [2.1, 2.2]
    assert series["XRP"] == [0.5, 0.55]


def test_maps_pegged_symbols_to_canonical_cmc_symbols(mocker):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _resp(mocker, {
            "BNB": _coin([_quote("2024-01-01T00:00:00.000Z", 300.0)]),
            "BTC": _coin([_quote("2024-01-01T00:00:00.000Z", 42000.0)]),
        })

    mocker.patch.object(cdl.requests, "get", side_effect=fake_get)

    series, _ = cdl.load_history_cmc(["WBNB", "BTCB"], count=100, use_cache=False)

    sent = captured["params"]["symbol"].split(",")
    assert "BNB" in sent and "BTC" in sent            # canonical sent to CMC
    assert "WBNB" in series and "BTCB" in series       # mapped back to our symbols
    assert series["WBNB"] == [300.0]
    assert series["BTCB"] == [42000.0]


# --- hardening: bounded inputs ---------------------------------------------

def test_rejects_unknown_interval(mocker):
    mocker.patch.object(cdl.requests, "get")
    with pytest.raises(ValueError):
        cdl.load_history_cmc(["CAKE"], interval="totally-bogus", use_cache=False)


def test_clamps_count_to_max(mocker):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _resp(mocker, {"CAKE": _coin([_quote("2024-01-01T00:00:00.000Z", 2.0)])})

    mocker.patch.object(cdl.requests, "get", side_effect=fake_get)
    cdl.load_history_cmc(["CAKE"], count=999_999, use_cache=False)
    assert captured["params"]["count"] <= cdl.MAX_DATAPOINTS_PER_REQUEST


def test_chunks_requests_under_datapoint_cap(mocker):
    """High count forces 1 symbol/request; results still merge and align."""
    def fake_get(url, headers=None, params=None, timeout=None):
        sym = params["symbol"]
        return _resp(mocker, {sym: _coin([
            _quote("2024-01-01T00:00:00.000Z", 1.0),
            _quote("2024-01-01T01:00:00.000Z", 1.1),
        ])})

    get = mocker.patch.object(cdl.requests, "get", side_effect=fake_get)
    # count=8000 -> chunk size = 10000//8000 = 1 -> one request per symbol.
    series, times = cdl.load_history_cmc(["CAKE", "XRP", "ETH"], count=8000, use_cache=False)
    assert get.call_count == 3
    assert set(series) == {"CAKE", "XRP", "ETH"}
    assert len(times) == 2


# --- hardening: defensive parsing ------------------------------------------

def test_skips_symbol_missing_from_response(mocker):
    data = {"CAKE": _coin([_quote("2024-01-01T00:00:00.000Z", 2.0)])}  # XRP absent
    mocker.patch.object(cdl.requests, "get", return_value=_resp(mocker, data))
    series, _ = cdl.load_history_cmc(["CAKE", "XRP"], count=100, use_cache=False)
    assert "CAKE" in series
    assert "XRP" not in series           # missing symbol skipped, no crash


def test_skips_bars_with_missing_or_bad_close(mocker):
    data = {"CAKE": _coin([
        _quote("2024-01-01T00:00:00.000Z", 2.0),
        {"time_open": "2024-01-01T01:00:00.000Z", "quote": {"USD": {}}},  # no close
        {"time_open": "2024-01-01T02:00:00.000Z", "quote": {"USD": {"close": None}}},
        _quote("2024-01-01T03:00:00.000Z", 2.3),
    ])}
    mocker.patch.object(cdl.requests, "get", return_value=_resp(mocker, data))
    series, times = cdl.load_history_cmc(["CAKE"], use_cache=False)
    assert series["CAKE"] == [2.0, 2.3]   # the two bad bars dropped
    assert len(times) == 2


def test_empty_response_returns_empty(mocker):
    mocker.patch.object(cdl.requests, "get", return_value=_resp(mocker, {}))
    series, times = cdl.load_history_cmc(["CAKE"], use_cache=False)
    assert series == {}
    assert times == []


# --- caching ---------------------------------------------------------------

def test_second_call_served_from_cache(mocker, tmp_path):
    mocker.patch.object(cdl, "CACHE_DIR", tmp_path)
    data = {"CAKE": _coin([
        _quote("2024-01-01T00:00:00.000Z", 2.0),
        _quote("2024-01-01T01:00:00.000Z", 2.1),
    ])}
    get = mocker.patch.object(cdl.requests, "get", return_value=_resp(mocker, data))

    s1, t1 = cdl.load_history_cmc(["CAKE"], use_cache=True)
    s2, t2 = cdl.load_history_cmc(["CAKE"], use_cache=True)

    assert get.call_count == 1            # second call hit the cache only
    assert s1 == s2 and t1 == t2


def test_api_key_never_in_cache(mocker, tmp_path):
    mocker.patch.object(cdl, "CACHE_DIR", tmp_path)
    data = {"CAKE": _coin([_quote("2024-01-01T00:00:00.000Z", 2.0)])}
    mocker.patch.object(cdl.requests, "get", return_value=_resp(mocker, data))
    cdl.load_history_cmc(["CAKE"], use_cache=True)
    for f in tmp_path.iterdir():
        assert "X-CMC" not in f.read_text() and "api_key" not in f.read_text().lower()
