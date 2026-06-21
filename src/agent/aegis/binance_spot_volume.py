"""Real 1m/5m volume from Binance SPOT klines — the volume source for MAJORS.

Majors (CAKE/ETH/LINK/UNI...) trade on regular Binance spot, NOT the Alpha venue,
so the Alpha klines provider has no data for them. This provider hits the standard
spot klines endpoint (api.binance.com/api/v3/klines) for SYMBOL+quote (e.g. CAKE ->
CAKEUSDT) and computes the same volume_multiple + price-change the breakout scan
needs. Read-only; no wallet, no signing.

Reuses the Alpha provider's kline parsing, robust median baseline, and AlphaVolume
shape, so the two volume sources are interchangeable behind MarketFeed. Fails safe:
missing pair / fetch error / empty / stale / zero baseline -> (0, 0).
"""
from __future__ import annotations

import time

import requests

from ..config import settings
from ..monitor.logger import get_logger
from .binance_alpha_volume import AlphaVolume, _unavailable, baseline_volume, parse_klines

log = get_logger(__name__)

_TIMEOUT_S = 15
_KLINES_PATH = "/api/v3/klines"


class BinanceSpotKlinesVolumeProvider:
    def __init__(self, *, api_base: str | None = None, interval: str | None = None,
                 baseline_candles: int | None = None, freshness_s: int | None = None,
                 quote: str = "USDT", symbols: set[str] | None = None,
                 session: requests.Session | None = None) -> None:
        # data-api.binance.vision serves public /api/v3/klines and is NOT geo-blocked
        # (api.binance.com returns HTTP 451 from many regions / our VPS).
        self.api_base = (api_base or settings.binance_spot_api_base).rstrip("/")
        self.interval = interval or settings.alpha_kline_interval
        self.baseline_candles = baseline_candles or settings.alpha_baseline_candles
        self.freshness_s = freshness_s or settings.alpha_freshness_seconds
        self.quote = quote.upper()
        # Optional allowlist of base symbols we serve (the major universe). If set,
        # anything outside it is reported unavailable rather than fetched.
        self.symbols = {s.upper() for s in symbols} if symbols else None
        self._session = session or requests

    def _pair(self, symbol: str) -> str:
        return f"{symbol.upper()}{self.quote}"

    def _fetch(self, pair: str) -> list:
        params = {"symbol": pair, "interval": self.interval, "limit": self.baseline_candles + 1}
        resp = self._session.get(self.api_base + _KLINES_PATH, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, list) else (payload.get("data") or [])

    def get(self, symbol: str) -> AlphaVolume:
        base = symbol.upper()
        if base == self.quote or (self.symbols is not None and base not in self.symbols):
            return _unavailable(symbol, "", "", "not_a_major")
        pair = self._pair(base)
        try:
            rows = self._fetch(pair)
        except Exception as e:  # noqa: BLE001 — network/HTTP failure must fail safe
            log.debug("spot_klines_fetch_failed", pair=pair, error=type(e).__name__)
            return _unavailable(symbol, "", pair, "fetch_failed")

        candles = parse_klines(rows)
        if len(candles) < 2:
            return _unavailable(symbol, "", pair, "insufficient_klines")
        candles.sort(key=lambda c: c["open_time"])
        current, prior = candles[-1], candles[:-1]

        age_s = max(0.0, time.time() - current["close_time"] / 1000.0)
        if age_s > self.freshness_s:
            return _unavailable(symbol, "", pair, "stale_candle")

        quote_baseline = baseline_volume([c["quote_volume"] for c in prior])
        if quote_baseline <= 0:
            return _unavailable(symbol, "", pair, "zero_baseline")
        price_5m_ago = prior[-1]["close"] if prior else current["open"]
        change = ((current["close"] - price_5m_ago) / price_5m_ago) if price_5m_ago > 0 else 0.0

        return AlphaVolume(
            symbol=base, bsc_contract="", alpha_symbol=pair, timestamp=time.time(),
            current_volume_5m=current["base_volume"], current_quote_volume_5m=current["quote_volume"],
            baseline_volume=baseline_volume([c["base_volume"] for c in prior]),
            baseline_quote_volume=quote_baseline,
            volume_multiple=(current["quote_volume"] / quote_baseline),
            price_now=current["close"], price_5m_ago=price_5m_ago, price_change_5m_pct=change,
            trade_count_5m=current["trades"], source="binance_spot_klines", confidence=1.0,
            freshness_seconds=age_s,
        )

    def volume_tuple(self, symbol: str) -> tuple[float, float]:
        v = self.get(symbol)
        if not v.available:
            return 0.0, 0.0
        return v.current_quote_volume_5m, v.baseline_quote_volume

    # Same-source variant: (vol, baseline, 5m move) from one fetch, or (0, 0, None).
    def volume_and_move(self, symbol: str) -> tuple[float, float, float | None]:
        v = self.get(symbol)
        if not v.available:
            return 0.0, 0.0, None
        return v.current_quote_volume_5m, v.baseline_quote_volume, v.price_change_5m_pct
