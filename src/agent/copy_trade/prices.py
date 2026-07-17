"""Price/tax lookups for the valve and shadow fills. Free keyless APIs; every
function returns None on failure — callers hold state and alert rather than guess
(spec: 'lỗi thì giữ nguyên trạng thái và alert, không đoán giá')."""
from __future__ import annotations

import time

import requests

from ..monitor.logger import get_logger

log = get_logger(__name__)

_DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"
_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses="

# Short TTL cache: the monitor calls get_price_usd once per buy event, and live
# scans routinely carry bursts of events for the SAME token (seen 2026-07-17:
# 5+ same-token events in one batch) — plus the valve re-prices every open
# position each ~45s tick. 60s of staleness is irrelevant to both users (the
# tracker only needs the price near observation time; the valve is a -70%
# catastrophe backstop), but the dedup keeps us far from DexScreener's rate
# limit. Failures are never cached. ponytail: whole-dict expiry-on-read, no
# eviction policy — bounded by distinct tokens seen per hour, fine at this scale.
_PRICE_TTL_S = 60
_price_cache: dict[str, tuple[float, float]] = {}   # addr -> (fetched_at, price)


def get_price_usd(token_address: str) -> float | None:
    key = token_address.lower()
    hit = _price_cache.get(key)
    if hit is not None and time.time() - hit[0] < _PRICE_TTL_S:
        return hit[1]
    try:
        r = requests.get(_DEXSCREENER + token_address, timeout=15)
        r.raise_for_status()
        pairs = [p for p in (r.json().get("pairs") or [])
                 if p.get("chainId") == "bsc" and p.get("priceUsd")]
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        price = float(best["priceUsd"])
        _price_cache[key] = (time.time(), price)
        return price
    except Exception as e:  # noqa: BLE001
        log.warning("dexscreener_price_failed", token=token_address,
                    error=type(e).__name__)
        return None


def get_taxes(token_address: str) -> tuple[float, float] | None:
    try:
        r = requests.get(_GOPLUS + token_address, timeout=15)
        r.raise_for_status()
        result = r.json().get("result") or {}
        info = result.get(token_address.lower()) or result.get(token_address)
        if not info:
            return None
        buy_tax, sell_tax = info.get("buy_tax"), info.get("sell_tax")
        if buy_tax in (None, "") or sell_tax in (None, ""):
            return None
        return float(buy_tax), float(sell_tax)
    except Exception as e:  # noqa: BLE001
        log.warning("goplus_taxes_failed", token=token_address, error=type(e).__name__)
        return None
