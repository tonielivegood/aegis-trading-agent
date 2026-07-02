"""CoinGecko client — free-tier FALLBACK for CMC when CMC errors (quota/network).

Same output shape as cmc_client.get_quotes(): {symbol: {"price", "percent_change_1h",
"percent_change_24h"}} — "2 cái đó tác dụng như nhau" (2/7, user call), so either
source is fine for the regime's BTC read.

Minimal by design (YAGNI): only a curated, hand-verified symbol -> CoinGecko id map
for well-known majors — CoinGecko ids are ticker-ambiguous for anything obscure
(many coins share a ticker), and the live path today only ever calls this with "BTC"
(the beta_core majors-momentum path that would have needed the full universe is
disabled — see config.py beta_core_enabled). Extend the map if a new caller needs it.
"""
from __future__ import annotations

import requests

from ..monitor.logger import get_logger

log = get_logger(__name__)

_BASE = "https://api.coingecko.com/api/v3"
_TIMEOUT_S = 10

_IDS: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin",
    "SOL": "solana", "LTC": "litecoin", "LINK": "chainlink", "AVAX": "avalanche-2",
}


def get_quotes(symbols: list[str]) -> dict[str, dict]:
    """Best-effort quotes for symbols we have a curated id for. Unknown symbols are
    silently skipped; any HTTP/parse failure returns {} — never raises, so a caller
    using this as a fallback never trades a hiccup here for a broken tick."""
    ids = [_IDS[s.upper()] for s in symbols if s.upper() in _IDS]
    if not ids:
        return {}
    try:
        resp = requests.get(f"{_BASE}/coins/markets", params={
            "vs_currency": "usd", "ids": ",".join(ids),
            "price_change_percentage": "1h,24h",
        }, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("coingecko_quotes_failed", error=type(e).__name__)
        return {}
    by_id = {row["id"]: row for row in rows if row.get("id")}
    out: dict[str, dict] = {}
    for sym in symbols:
        cg_id = _IDS.get(sym.upper())
        row = by_id.get(cg_id) if cg_id else None
        if not row:
            continue
        out[sym.upper()] = {
            "price": row.get("current_price"),
            "percent_change_1h": row.get("price_change_percentage_1h_in_currency"),
            "percent_change_24h": row.get("price_change_percentage_24h_in_currency"),
        }
    return out
