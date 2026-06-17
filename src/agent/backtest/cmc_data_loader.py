"""Historical OHLCV from the CoinMarketCap Pro API (the contest's price source).

Drop-in alternative to `data_loader.load_history`: returns the SAME
`(series: {symbol: [close, ...]}, times: [epoch_ms, ...])` shape, so the
walk-forward backtest runs against CMC closes — the exact feed the contest uses
to value the portfolio hourly — instead of a Binance-pair proxy.

Requires the Professional plan (5M credits/mo, hourly history up to ~12 months,
daily back to 2013). Our BSC-pegged symbols are mapped to their canonical CMC
asset (WBNB->BNB, BTCB->BTC) which carries the deep, clean history.

Offline research tooling only — never imported by the live trading loop.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import requests

from ..config import settings
from ..data.token_list import STABLECOINS, tradable_symbols
from ..monitor.logger import get_logger

log = get_logger(__name__)

ENDPOINT = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/ohlcv/historical"
CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "historical" / "cmc"

# CMC ohlcv/historical caps a single request at 10000 TOTAL datapoints
# (symbols x count), so we chunk requests to stay under it.
MAX_DATAPOINTS_PER_REQUEST = 10_000
DEFAULT_COUNT = 8_000  # ~333 days hourly — within the Pro 12-month hourly window

# Allowed CMC `interval` values we support (guards against arbitrary params).
VALID_INTERVALS = {
    "hourly", "daily", "weekly", "monthly",
    "1h", "2h", "3h", "4h", "6h", "12h", "1d", "2d", "3d", "7d", "15d", "30d",
}

# Our symbol -> canonical CMC symbol for clean long history.
_TO_CMC = {"WBNB": "BNB", "BTCB": "BTC"}
_FROM_CMC = {v: k for k, v in _TO_CMC.items()}


def _headers() -> dict[str, str]:
    return {"X-CMC_PRO_API_KEY": settings.cmc_api_key, "Accept": "application/json"}


def _to_epoch_ms(iso: str) -> int | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def _parse_quotes(quotes: list, convert: str) -> dict[int, float]:
    """Pull {epoch_ms: close} from a coin's quote list, dropping malformed bars."""
    out: dict[int, float] = {}
    for q in quotes or []:
        ts = _to_epoch_ms(q.get("time_open", ""))
        close = (q.get("quote", {}).get(convert, {}) or {}).get("close")
        if ts is not None and isinstance(close, (int, float)):
            out[ts] = float(close)
    return out


def _fetch_ohlcv(our_symbols: list[str], interval: str, count: int,
                 convert: str) -> dict[str, dict[int, float]]:
    """One batched request for all symbols -> {our_symbol: {epoch_ms: close}}."""
    cmc_symbols = [_TO_CMC.get(s, s) for s in our_symbols]
    params = {
        "symbol": ",".join(cmc_symbols),
        "interval": interval,
        "count": count,
        "convert": convert,
    }
    resp = requests.get(ENDPOINT, headers=_headers(), params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    out: dict[str, dict[int, float]] = {}
    for cmc_sym, entries in (payload.get("data") or {}).items():
        coin = entries[0] if isinstance(entries, list) else entries
        series = _parse_quotes((coin or {}).get("quotes", []), convert)
        if series:
            out[_FROM_CMC.get(cmc_sym, cmc_sym)] = series
    return out


def load_history_cmc(
    symbols: list[str] | None = None,
    interval: str = "hourly",
    count: int = DEFAULT_COUNT,
    convert: str = "USD",
    use_cache: bool = True,
) -> tuple[dict[str, list[float]], list[int]]:
    """Aligned hourly (or daily) closes from CMC, ready for the backtest engine.

    Returns ``(series, times)`` aligned on the intersection of timestamps present
    for every fetched symbol, so the engine sees a consistent grid.
    """
    if interval not in VALID_INTERVALS:
        raise ValueError(f"Unsupported interval {interval!r}; one of {sorted(VALID_INTERVALS)}")
    count = max(1, min(int(count), MAX_DATAPOINTS_PER_REQUEST))
    symbols = symbols or [s for s in tradable_symbols() if s not in STABLECOINS]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw: dict[str, dict[int, float]] = {}
    to_fetch: list[str] = []
    for sym in symbols:
        cache = CACHE_DIR / f"{sym}_{interval}_{count}.json"
        if use_cache and cache.exists():
            raw[sym] = {int(k): v for k, v in json.loads(cache.read_text()).items()}
        else:
            to_fetch.append(sym)

    # Chunk so (symbols x count) stays under the per-request datapoint cap.
    chunk_size = max(1, MAX_DATAPOINTS_PER_REQUEST // count)
    for i in range(0, len(to_fetch), chunk_size):
        chunk = to_fetch[i:i + chunk_size]
        try:
            fetched = _fetch_ohlcv(chunk, interval, count, convert)
        except Exception as e:  # noqa: BLE001 — one failed fetch must not kill the run
            log.warning("cmc_ohlcv_fetch_failed", error=str(e), symbols=len(chunk))
            continue
        for sym, series in fetched.items():
            (CACHE_DIR / f"{sym}_{interval}_{count}.json").write_text(
                json.dumps(series), encoding="utf-8"
            )
            raw[sym] = series

    if not raw:
        return {}, []

    common = set.intersection(*(set(d.keys()) for d in raw.values()))
    times = sorted(common)
    series = {sym: [raw[sym][t] for t in times] for sym in raw}
    log.debug("cmc_history_loaded", symbols=len(series), bars=len(times))
    return series, times
