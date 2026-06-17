"""CoinMarketCap Data API client — quotes for the tradable universe.

Thin, cached, rate-aware wrapper. Plan = Professional (5M credits/month, 1200
req/min), so credits are not a constraint; we still cache for a short TTL and
batch all symbols into one request to avoid redundant calls within a tick.
"""
from __future__ import annotations

import time

import requests

from ..config import settings
from ..monitor.logger import get_logger

log = get_logger(__name__)

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 15.0  # seconds — fresher quotes; Pro plan has ample credit headroom


def _headers() -> dict[str, str]:
    return {"X-CMC_PRO_API_KEY": settings.cmc_api_key, "Accept": "application/json"}


def get_quotes(symbols: list[str], convert: str = "USD") -> dict[str, dict]:
    """Return {symbol: quote_dict} with price, volume_24h, percent_change_*.

    One CMC call for all symbols. Cached for _CACHE_TTL seconds.
    """
    key = ",".join(sorted(symbols)) + "|" + convert
    now = time.time()
    if key in _CACHE and now - _CACHE[key][0] < _CACHE_TTL:
        return _CACHE[key][1]

    url = f"{settings.cmc_api_base}/v2/cryptocurrency/quotes/latest"
    params = {"symbol": ",".join(symbols), "convert": convert}
    resp = requests.get(url, headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    out: dict[str, dict] = {}
    for sym, entries in payload.get("data", {}).items():
        entry = entries[0] if isinstance(entries, list) else entries
        q = entry["quote"][convert]
        out[sym] = {
            "price": q.get("price"),
            "volume_24h": q.get("volume_24h"),
            "percent_change_1h": q.get("percent_change_1h"),
            "percent_change_24h": q.get("percent_change_24h"),
            "percent_change_7d": q.get("percent_change_7d"),
        }
    _CACHE[key] = (now, out)
    log.debug("cmc_quotes_fetched", symbols=len(out))
    return out
