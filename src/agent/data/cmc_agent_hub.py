"""CoinMarketCap **AI Agent Hub** skills — market sentiment + community trending.

These are two REST "skills" from CMC's AI Agent Hub catalog
(https://coinmarketcap.com/api/documentation/ai-agent-hub), called with the same
Pro key we already hold:

    GET /v3/fear-and-greed/latest      → market-wide Fear & Greed score (0-100)
    GET /v1/community/trending/token   → tokens trending by community activity

Both feed the agent OUT of the 60s hot path: the hourly regime updater calls these,
caches the result, and the mechanical rails consume the cached value. Every call
**fails safe** — a network/parse hiccup returns ``None`` / an empty set, so the agent
simply keeps its prior momentum-only behaviour. By design these signals can only
*tighten* the regime (Fear) or *re-rank* already-qualified breakouts (Trending);
neither can open a position on its own or break a tick.
"""
from __future__ import annotations

import time

import requests

from ..config import settings
from ..monitor.logger import get_logger

log = get_logger(__name__)

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 300.0  # 5 min — Agent Hub signals move slowly; the hourly updater refreshes anyway
_TIMEOUT = 20.0


def _headers() -> dict[str, str]:
    return {"X-CMC_PRO_API_KEY": settings.cmc_api_key, "Accept": "application/json"}


def get_fear_greed() -> dict | None:
    """Market Fear & Greed index → ``{"value": int 0-100, "classification": str}``.

    Returns ``None`` on any error or a malformed payload (caller treats that as
    "no sentiment read"). Cached for ``_CACHE_TTL`` seconds.
    """
    now = time.time()
    hit = _CACHE.get("fng")
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]  # type: ignore[return-value]
    try:
        r = requests.get(f"{settings.cmc_api_base}/v3/fear-and-greed/latest",
                         headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        d = r.json().get("data") or {}
        val = d.get("value")
        if val is None:
            return None
        out = {"value": int(val), "classification": d.get("value_classification", "")}
        _CACHE["fng"] = (now, out)
        return out
    except Exception as e:  # noqa: BLE001 — fail safe, never break the regime updater
        log.info("cmc_fear_greed_failed", error=type(e).__name__)
        return None


def get_trending_symbols(limit: int = 5) -> frozenset[str]:  # endpoint caps limit at 5
    """Uppercase symbols trending by CMC community activity (empty set on error).

    Used only to RE-RANK breakout signals that already passed every entry gate, so
    an empty/stale set is harmless (the agent ranks by raw money-flow, as before).
    """
    now = time.time()
    hit = _CACHE.get("trending")
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]  # type: ignore[return-value]
    try:
        r = requests.get(f"{settings.cmc_api_base}/v1/community/trending/token",
                         headers=_headers(), params={"limit": limit}, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json().get("data") or []
        syms = frozenset(str(t["symbol"]).upper() for t in data if t.get("symbol"))
        _CACHE["trending"] = (now, syms)
        return syms
    except Exception as e:  # noqa: BLE001 — fail safe
        log.info("cmc_trending_failed", error=type(e).__name__)
        return frozenset()
