"""Binance Alpha 5m volume provider tests — parsing, baseline, mapping, fail-safe.

No network: a fake session returns canned kline payloads. We never assert on real
Binance data and never fake a volume number into the strategy.
"""
from __future__ import annotations

import time

import pytest
import requests

from src.agent.aegis import binance_alpha_volume as bav
from src.agent.aegis.binance_alpha_volume import (
    BinanceAlphaKlinesVolumeProvider,
    baseline_volume,
    parse_klines,
)

CONTRACT = "0x4b0f1812e5df2a09796481ff14017e6005508003"
ENTRY = {"symbol": "TWT", "bsc_contract": CONTRACT, "alpha_symbol": "ALPHA_101USDT",
         "alpha_id": "ALPHA_101", "base_asset": "TWT"}


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc = payload, exc

    def get(self, url, params=None, timeout=None):
        if self.exc:
            raise self.exc
        return _Resp(self.payload)


def _kline(close_time_ms, close, base_vol, quote_vol, trades=10, open_price=None):
    ot = close_time_ms - bav._FIVE_MIN_MS
    return [ot, open_price if open_price is not None else close, close, close, close,
            base_vol, close_time_ms, quote_vol, trades, 0.0, 0.0, 0]


def _rows(current_quote=600.0, baseline_quote=100.0, n=24, fresh=True):
    # When not fresh, shift the WHOLE series back 1h so the latest candle is stale.
    end_ms = int(time.time() * 1000) - (0 if fresh else 3_600_000)
    rows = []
    for i in range(n, 0, -1):
        ct = end_ms - i * bav._FIVE_MIN_MS
        rows.append(_kline(ct, 1.0, baseline_quote, baseline_quote, trades=10))
    rows.append(_kline(end_ms, 1.05, current_quote, current_quote, trades=50, open_price=1.0))
    return rows


def _provider(payload=None, exc=None, mapping=None) -> BinanceAlphaKlinesVolumeProvider:
    p = BinanceAlphaKlinesVolumeProvider(session=_Session(payload, exc), freshness_s=600)
    p.map = {} if mapping is None else mapping
    return p


# ----------------------------- parsing -----------------------------

def test_parse_klines_extracts_all_fields():
    rows = [_kline(1_000_000, 2.0, 50.0, 120.0, trades=7, open_price=1.5)]
    c = parse_klines(rows)[0]
    assert c["open"] == 1.5 and c["close"] == 2.0
    assert c["base_volume"] == 50.0 and c["quote_volume"] == 120.0
    assert c["trades"] == 7


def test_parse_klines_skips_malformed_rows():
    assert parse_klines([[1, 2, 3], None, "bad"]) == []


# ----------------------------- baseline -----------------------------

def test_baseline_is_median_outlier_resistant():
    assert baseline_volume([100, 100, 100, 100000]) == 100  # median ignores the spike


def test_baseline_zero_when_empty():
    assert baseline_volume([]) == 0.0 and baseline_volume([0, 0]) == 0.0


# ----------------------------- mapping -----------------------------

def test_resolve_by_contract_and_symbol():
    p = _provider(mapping={CONTRACT: ENTRY})
    assert p._resolve(CONTRACT) == ENTRY
    assert p._resolve("TWT") == ENTRY
    assert p._resolve("0xdeadbeef") is None


def test_missing_mapping_fails_safe():
    v = _provider(mapping={}).get(CONTRACT)
    assert not v.available and v.unavailable_reason == "no_alpha_mapping"


# ----------------------------- happy path -----------------------------

def test_valid_klines_compute_quote_volume_and_multiple(tmp_path):
    p = _provider(payload=_rows(current_quote=600, baseline_quote=100), mapping={CONTRACT: ENTRY})
    p.cache_path = tmp_path / "vol.json"
    v = p.get(CONTRACT)
    assert v.available and v.source == "binance_alpha_klines_5m"
    assert v.current_quote_volume_5m == 600.0
    assert v.baseline_quote_volume == 100.0
    assert v.volume_multiple == pytest.approx(6.0)
    assert v.price_now == 1.05 and v.price_5m_ago == 1.0
    assert v.trade_count_5m == 50


def test_volume_tuple_adapter_returns_quote_volumes(tmp_path):
    p = _provider(payload=_rows(600, 100), mapping={CONTRACT: ENTRY})
    p.cache_path = tmp_path / "vol.json"
    assert p.volume_tuple(CONTRACT) == (600.0, 100.0)   # feeds a 6x spike to the radar


# ----------------------------- fail-safe -----------------------------

def test_fetch_failure_fails_safe():
    v = _provider(exc=requests.ConnectionError("boom"), mapping={CONTRACT: ENTRY}).get(CONTRACT)
    assert not v.available and v.unavailable_reason == "fetch_failed"
    assert v.volume_multiple == 0.0


def test_empty_klines_fail_safe():
    v = _provider(payload=[], mapping={CONTRACT: ENTRY}).get(CONTRACT)
    assert not v.available and v.unavailable_reason == "insufficient_klines"


def test_stale_candle_fails_safe():
    v = _provider(payload=_rows(fresh=False), mapping={CONTRACT: ENTRY}).get(CONTRACT)
    assert not v.available and v.unavailable_reason == "stale_candle"


def test_zero_baseline_fails_safe():
    v = _provider(payload=_rows(current_quote=0, baseline_quote=0), mapping={CONTRACT: ENTRY}).get(CONTRACT)
    assert not v.available and v.unavailable_reason == "zero_baseline"


def test_unavailable_volume_tuple_is_zero():
    assert _provider(exc=requests.Timeout(), mapping={CONTRACT: ENTRY}).volume_tuple(CONTRACT) == (0.0, 0.0)
