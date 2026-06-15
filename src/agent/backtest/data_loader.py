"""Historical hourly price data from Binance public market-data API (no key).

Uses data-api.binance.vision (public, no geo-block). Maps our curated symbols to
their Binance USDT spot pairs; stablecoins are constant 1.0 and need no history.
Series are aligned on common open-times so the backtest sees a consistent grid.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from ..data.token_list import STABLECOINS, tradable_symbols

BASE = "https://data-api.binance.vision/api/v3/klines"
CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "historical"

# Our symbol -> Binance pair. WBNB trades as BNB, BTCB as BTC.
PAIR = {
    "WBNB": "BNBUSDT", "CAKE": "CAKEUSDT", "BTCB": "BTCUSDT", "ETH": "ETHUSDT",
    "XRP": "XRPUSDT", "ADA": "ADAUSDT", "DOGE": "DOGEUSDT", "DOT": "DOTUSDT",
    "LTC": "LTCUSDT", "LINK": "LINKUSDT", "UNI": "UNIUSDT", "AVAX": "AVAXUSDT",
    "TWT": "TWTUSDT", "ATOM": "ATOMUSDT", "INJ": "INJUSDT", "FIL": "FILUSDT",
}


def _fetch_page(pair: str, interval: str, end_time: int | None) -> list:
    url = f"{BASE}?symbol={pair}&interval={interval}&limit=1000"
    if end_time is not None:
        url += f"&endTime={end_time}"
    req = urllib.request.Request(url, headers={"User-Agent": "bnbhack-backtest"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def _fetch_klines(pair: str, interval: str = "1h", limit: int = 1000) -> dict[int, float]:
    """Fetch up to `limit` closes, paginating backward in 1000-bar pages."""
    out: dict[int, float] = {}
    end_time: int | None = None
    while len(out) < limit:
        rows = _fetch_page(pair, interval, end_time)
        if not rows:
            break
        for row in rows:  # row: [openTime, open, high, low, close, volume, ...]
            out[int(row[0])] = float(row[4])
        end_time = min(int(r[0]) for r in rows) - 1  # step back before earliest bar
        if len(rows) < 1000:
            break
        time.sleep(0.15)
    return out


def load_history(symbols: list[str] | None = None, interval: str = "1h",
                 limit: int = 1000, use_cache: bool = True) -> tuple[dict[str, list[float]], list[int]]:
    symbols = symbols or [s for s in tradable_symbols() if s not in STABLECOINS]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    raw: dict[str, dict[int, float]] = {}
    for sym in symbols:
        pair = PAIR.get(sym)
        if not pair:
            continue
        cache = CACHE_DIR / f"{pair}_{interval}_{limit}.json"
        if use_cache and cache.exists():
            raw[sym] = {int(k): v for k, v in json.loads(cache.read_text()).items()}
            continue
        try:
            data = _fetch_klines(pair, interval, limit)
            cache.write_text(json.dumps(data), encoding="utf-8")
            raw[sym] = data
            time.sleep(0.2)  # be polite to the public endpoint
        except Exception as e:  # noqa: BLE001
            print(f"  ! skip {sym} ({pair}): {type(e).__name__}")

    # Align on the intersection of open-times present for every fetched symbol.
    if not raw:
        return {}, []
    common = set.intersection(*(set(d.keys()) for d in raw.values()))
    times = sorted(common)
    series = {sym: [raw[sym][t] for t in times] for sym in raw}
    return series, times
