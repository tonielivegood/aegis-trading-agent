"""Price/tax lookups for the valve and shadow fills. Free keyless APIs; every
function returns None on failure — callers hold state and alert rather than guess
(spec: 'lỗi thì giữ nguyên trạng thái và alert, không đoán giá')."""
from __future__ import annotations

import requests

from ..monitor.logger import get_logger

log = get_logger(__name__)

_DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"
_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses="


def get_price_usd(token_address: str) -> float | None:
    try:
        r = requests.get(_DEXSCREENER + token_address, timeout=15)
        r.raise_for_status()
        pairs = [p for p in (r.json().get("pairs") or [])
                 if p.get("chainId") == "bsc" and p.get("priceUsd")]
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        return float(best["priceUsd"])
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
