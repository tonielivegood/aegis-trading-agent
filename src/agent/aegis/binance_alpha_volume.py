"""Real 5-minute volume from Binance Alpha klines (interval=5m).

This is the PRIMARY volume-confirmation source for the event radar. It is read-
only market data (no wallet, no signing, no broadcast). The provider:

  - maps an eligible BSC contract -> Binance Alpha symbol (ALPHA_<id>USDT) via a
    locally-generated mapping file (scripts/build_alpha_symbol_map.py). Mappings
    are matched by contract address + chainId 56; if a contract has no mapping,
    volume is reported UNAVAILABLE (we never invent a symbol or a number).
  - fetches recent 5m klines, parses them (standard Binance kline array),
    computes a robust baseline (median of prior quote volumes), and the current
    candle's quote volume, giving volume_multiple = current / baseline.
  - FAILS SAFE: missing mapping, missing/empty data, stale candles, or a
    zero/invalid baseline all yield unavailable=True with a clear reason and
    zero volume — which makes the strategy's volume-spike entry and 5x volume
    exit simply not fire (never falsely).

Prefer QUOTE asset volume (USDT-denominated) for confirmation; fall back to base
volume only when quote volume is absent, and label it in `source`.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median

import requests

from ..config import settings
from ..monitor.logger import get_logger

log = get_logger(__name__)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_MAP_PATH = DATA_DIR / "alpha_symbol_map.json"
RUNTIME = Path(__file__).resolve().parents[2].parent / "data" / "runtime"
DEFAULT_CACHE = RUNTIME / "alpha_volume_cache.json"

_TIMEOUT_S = 15
_FIVE_MIN_MS = 5 * 60 * 1000

# Default Binance Alpha klines path (configurable). The agg-klines endpoint
# returns standard Binance kline arrays for an Alpha symbol.
_KLINES_PATH = "/bapi/defi/v1/public/alpha-trade/klines"


@dataclass(frozen=True)
class AlphaVolume:
    symbol: str
    bsc_contract: str
    alpha_symbol: str
    timestamp: float
    current_volume_5m: float          # base volume
    current_quote_volume_5m: float    # quote (USDT) volume — preferred
    baseline_volume: float
    baseline_quote_volume: float
    volume_multiple: float
    price_now: float
    price_5m_ago: float
    price_change_5m_pct: float
    trade_count_5m: int
    source: str
    confidence: float
    freshness_seconds: float
    unavailable_reason: str = ""

    @property
    def available(self) -> bool:
        return not self.unavailable_reason


def _unavailable(symbol: str, contract: str, alpha_symbol: str, reason: str) -> AlphaVolume:
    return AlphaVolume(
        symbol=symbol, bsc_contract=contract, alpha_symbol=alpha_symbol, timestamp=time.time(),
        current_volume_5m=0.0, current_quote_volume_5m=0.0, baseline_volume=0.0,
        baseline_quote_volume=0.0, volume_multiple=0.0, price_now=0.0, price_5m_ago=0.0,
        price_change_5m_pct=0.0, trade_count_5m=0, source="binance_alpha_klines_5m",
        confidence=0.0, freshness_seconds=float("inf"), unavailable_reason=reason,
    )


def parse_klines(rows: list) -> list[dict]:
    """Parse standard Binance kline arrays into typed candle dicts.
    [openTime, open, high, low, close, baseVol, closeTime, quoteVol, nTrades,
     takerBuyBase, takerBuyQuote, ignore]"""
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 9:
            continue
        try:
            out.append({
                "open_time": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "base_volume": float(r[5]),
                "close_time": int(r[6]),
                "quote_volume": float(r[7]),
                "trades": int(r[8]),
                "taker_buy_base": float(r[9]) if len(r) > 9 else 0.0,
                "taker_buy_quote": float(r[10]) if len(r) > 10 else 0.0,
            })
        except (TypeError, ValueError):
            continue
    return out


def baseline_volume(values: list[float]) -> float:
    """Robust baseline = median of prior candle volumes (outlier-resistant)."""
    vals = [v for v in values if v > 0]
    if not vals:
        return 0.0
    return float(median(vals))


def load_alpha_map(path: Path | None = None) -> dict[str, dict]:
    """Return {contract.lower() -> {symbol, alpha_symbol, alpha_id, base_asset}}."""
    path = path or DEFAULT_MAP_PATH
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for r in raw:
        c = (r.get("bsc_contract") or "").lower()
        if c and r.get("alpha_symbol"):
            out[c] = r
    return out


class BinanceAlphaKlinesVolumeProvider:
    def __init__(self, *, map_path: Path | None = None, api_base: str | None = None,
                 interval: str | None = None, baseline_candles: int | None = None,
                 freshness_s: int | None = None, cache_path: Path | None = None,
                 session: requests.Session | None = None) -> None:
        self.map = load_alpha_map(map_path)
        self.api_base = (api_base or settings.binance_alpha_api_base).rstrip("/")
        self.interval = interval or settings.alpha_kline_interval
        self.baseline_candles = baseline_candles or settings.alpha_baseline_candles
        self.freshness_s = freshness_s or settings.alpha_freshness_seconds
        self.cache_path = cache_path or DEFAULT_CACHE
        self._session = session or requests

    # --- HTTP (read-only) ---
    def _fetch_klines(self, alpha_symbol: str) -> list:
        params = {"symbol": alpha_symbol, "interval": self.interval,
                  "limit": self.baseline_candles + 1}
        resp = self._session.get(self.api_base + _KLINES_PATH, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
        # Endpoint may return a bare array or {data: [...]}.
        if isinstance(payload, dict):
            return payload.get("data") or []
        return payload or []

    def _resolve(self, symbol_or_contract: str) -> dict | None:
        key = symbol_or_contract.lower()
        if key in self.map:
            return self.map[key]
        for entry in self.map.values():     # allow lookup by our symbol too
            if entry.get("symbol", "").upper() == symbol_or_contract.upper():
                return entry
        return None

    def get(self, symbol_or_contract: str) -> AlphaVolume:
        entry = self._resolve(symbol_or_contract)
        if not entry:
            return _unavailable(symbol_or_contract, "", "", "no_alpha_mapping")
        sym, contract, alpha_symbol = entry.get("symbol", ""), entry.get("bsc_contract", ""), entry["alpha_symbol"]

        try:
            rows = self._fetch_klines(alpha_symbol)
        except Exception as e:  # noqa: BLE001 — network/HTTP failure must fail safe
            log.debug("alpha_klines_fetch_failed", alpha_symbol=alpha_symbol, error=type(e).__name__)
            return _unavailable(sym, contract, alpha_symbol, "fetch_failed")

        candles = parse_klines(rows)
        if len(candles) < 2:
            return _unavailable(sym, contract, alpha_symbol, "insufficient_klines")

        candles.sort(key=lambda c: c["open_time"])
        current, prior = candles[-1], candles[:-1]

        age_s = max(0.0, time.time() - current["close_time"] / 1000.0)
        if age_s > self.freshness_s:
            return _unavailable(sym, contract, alpha_symbol, "stale_candle")

        base_baseline = baseline_volume([c["base_volume"] for c in prior])
        quote_baseline = baseline_volume([c["quote_volume"] for c in prior])
        use_quote = quote_baseline > 0 and current["quote_volume"] > 0
        cur_metric = current["quote_volume"] if use_quote else current["base_volume"]
        base_metric = quote_baseline if use_quote else base_baseline
        if base_metric <= 0:
            return _unavailable(sym, contract, alpha_symbol, "zero_baseline")

        price_5m_ago = prior[-1]["close"] if prior else current["open"]
        change_pct = ((current["close"] - price_5m_ago) / price_5m_ago) if price_5m_ago > 0 else 0.0
        source = "binance_alpha_klines_5m" if use_quote else "binance_alpha_klines_5m_basevol"
        confidence = 1.0 if (use_quote and age_s <= _FIVE_MIN_MS / 1000) else 0.6

        vol = AlphaVolume(
            symbol=sym, bsc_contract=contract, alpha_symbol=alpha_symbol, timestamp=time.time(),
            current_volume_5m=current["base_volume"], current_quote_volume_5m=current["quote_volume"],
            baseline_volume=base_baseline, baseline_quote_volume=quote_baseline,
            volume_multiple=(cur_metric / base_metric), price_now=current["close"],
            price_5m_ago=price_5m_ago, price_change_5m_pct=change_pct,
            trade_count_5m=current["trades"], source=source, confidence=confidence,
            freshness_seconds=age_s,
        )
        self._write_cache(vol)
        return vol

    # MarketFeed adapter: (current_quote_volume, baseline_quote_volume), (0,0) if unavailable.
    def volume_tuple(self, symbol_or_contract: str) -> tuple[float, float]:
        v = self.get(symbol_or_contract)
        if not v.available:
            return 0.0, 0.0
        if v.baseline_quote_volume > 0 and v.current_quote_volume_5m > 0:
            return v.current_quote_volume_5m, v.baseline_quote_volume
        return v.current_volume_5m, v.baseline_volume

    def _write_cache(self, v: AlphaVolume) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache = {}
            if self.cache_path.exists():
                cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            cache[v.alpha_symbol] = {
                "alpha_symbol": v.alpha_symbol, "symbol": v.symbol,
                "price_now": v.price_now, "price_5m_ago": v.price_5m_ago,
                "current_quote_volume_5m": v.current_quote_volume_5m,
                "baseline_quote_volume": v.baseline_quote_volume,
                "volume_multiple": v.volume_multiple, "freshness_seconds": v.freshness_seconds,
                "source": v.source, "confidence": v.confidence, "ts": v.timestamp,
            }
            self.cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — cache is best-effort
            log.debug("alpha_volume_cache_write_failed", error=type(e).__name__)


def to_dict(v: AlphaVolume) -> dict:
    return asdict(v)
