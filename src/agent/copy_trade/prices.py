"""Price/tax lookups for the valve and shadow fills. Free keyless APIs; every
function returns None on failure — callers hold state and alert rather than guess
(spec: 'lỗi thì giữ nguyên trạng thái và alert, không đoán giá')."""
from __future__ import annotations

import time

import requests

from .net_timeout import call_with_hard_timeout
from ..monitor.logger import get_logger

log = get_logger(__name__)

_DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"
_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses="

_PRICE_TTL_S = 60
# Shared cache feeds both get_price_usd (valve/tracker) and get_pair_stats
# (gem filter) — the gem filter must not double DexScreener traffic. Live
# monitoring shows bursts of 5+ same-token buy events in one batch (2026-07-17),
# and the valve re-prices every ~45s tick. 60s TTL deduplicates these without
# stale data risk (tracker needs price near observation, valve is a -70%
# backstop). Failures never cached. ponytail: no eviction policy, bounded by
# distinct tokens per hour — fine at this scale.
_pairs_cache: dict[str, tuple[float, list]] = {}


def _fetch_pairs(token_address: str) -> list | None:
    key = token_address.lower()
    hit = _pairs_cache.get(key)
    if hit is not None and time.time() - hit[0] < _PRICE_TTL_S:
        return hit[1]
    try:
        r = call_with_hard_timeout(requests.get, _DEXSCREENER + token_address,
                                   timeout=15, hard_timeout=25)
        r.raise_for_status()
        pairs = [p for p in (r.json().get("pairs") or [])
                 if p.get("chainId") == "bsc" and p.get("priceUsd")]
        if not pairs:
            return None
        _pairs_cache[key] = (time.time(), pairs)
        return pairs
    except Exception as e:  # noqa: BLE001
        log.warning("dexscreener_fetch_failed", token=token_address,
                    error=type(e).__name__)
        return None


def _best_pair(pairs: list) -> dict:
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def get_price_usd(token_address: str) -> float | None:
    pairs = _fetch_pairs(token_address)
    if not pairs:
        return None
    return float(_best_pair(pairs)["priceUsd"])


def get_pair_stats(token_address: str) -> dict | None:
    """Gem-filter facts for a token. price/liquidity/mcap come from the
    highest-liquidity BSC pair; age is the EARLIEST pairCreatedAt across all
    pairs (DexScreener sometimes omits it on the best pair — seen live 17/7)."""
    pairs = _fetch_pairs(token_address)
    if not pairs:
        return None
    best = _best_pair(pairs)
    created = [p["pairCreatedAt"] for p in pairs if p.get("pairCreatedAt")]
    mcap = best.get("marketCap") or best.get("fdv")
    return {
        "price_usd": float(best["priceUsd"]),
        "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0.0),
        "market_cap_usd": float(mcap) if mcap else None,
        "pair_created_at_ms": min(created) if created else None,
        "pair_address": best.get("pairAddress"),
        "txns_h1_buys": int(((best.get("txns") or {}).get("h1") or {}).get("buys") or 0),
        "txns_h1_sells": int(((best.get("txns") or {}).get("h1") or {}).get("sells") or 0),
        "txns_m5_buys": int(((best.get("txns") or {}).get("m5") or {}).get("buys") or 0),
        "txns_m5_sells": int(((best.get("txns") or {}).get("m5") or {}).get("sells") or 0),
        "price_change_m5": (best.get("priceChange") or {}).get("m5"),
        "price_change_h1": (best.get("priceChange") or {}).get("h1"),
    }


def get_taxes(token_address: str) -> tuple[float, float] | None:
    try:
        r = call_with_hard_timeout(requests.get, _GOPLUS + token_address,
                                   timeout=15, hard_timeout=25)
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


_HOLDER_TTL_S = 55
_holder_cache: dict[str, tuple[float, dict]] = {}
_DEAD = "0x000000000000000000000000000000000000dead"


def get_holder_stats(token_address: str) -> dict | None:
    """Holder distribution facts for the concentration gate + phase-2 films.
    Excludes LP pools (non-empty GoPlus tag), the dead address, and locked
    holders — what's left is the supply that can actually dump on us."""
    key = token_address.lower()
    hit = _holder_cache.get(key)
    if hit is not None and time.time() - hit[0] < _HOLDER_TTL_S:
        return hit[1]
    try:
        r = call_with_hard_timeout(requests.get, _GOPLUS + token_address,
                                   timeout=15, hard_timeout=25)
        r.raise_for_status()
        result = r.json().get("result") or {}
        info = result.get(token_address.lower()) or result.get(token_address)
        if not info:
            return None
        free = [float(h.get("percent") or 0) for h in (info.get("holders") or [])
                if not h.get("tag") and (h.get("address") or "").lower() != _DEAD
                and not h.get("is_locked")]
        free.sort(reverse=True)
        out = {"holder_count": int(info.get("holder_count") or 0),
               "top_pct": free[0] if free else 0.0,
               "top5_pct": sum(free[:5])}
        _holder_cache[key] = (time.time(), out)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("goplus_holders_failed", token=token_address, error=type(e).__name__)
        return None
