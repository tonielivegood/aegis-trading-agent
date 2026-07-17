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

_PRICE_TTL_S = 60
# addr -> (fetched_at, bsc_pairs_list). One cached HTTP fetch feeds BOTH
# get_price_usd (valve/tracker) and get_pair_stats (gem filter) — the gem
# filter must not double DexScreener traffic. Failures are never cached.
_pairs_cache: dict[str, tuple[float, list]] = {}


def _fetch_pairs(token_address: str) -> list | None:
    key = token_address.lower()
    hit = _pairs_cache.get(key)
    if hit is not None and time.time() - hit[0] < _PRICE_TTL_S:
        return hit[1]
    try:
        r = requests.get(_DEXSCREENER + token_address, timeout=15)
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
    }


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
