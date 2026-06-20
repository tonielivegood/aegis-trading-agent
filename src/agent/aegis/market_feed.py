"""Live MarketSnapshot feed for the liquid eligible subset (read-only, DRY_RUN-safe).

Computes, per token, the fields the radar needs:
  - price (on-chain PancakeSwap, the executable price)
  - route availability + liquidity status + estimated slippage at our order size
  - 5-minute price change & recent pump %, from a rolling price cache persisted
    across ticks under data/runtime/
  - 5-minute volume & baseline, from an OPTIONAL pluggable volume provider

NOTE on volume: a true 5-minute volume series for thin Alpha/meme tokens needs a
dedicated market-data source (Binance Web3 market endpoint or CMC OHLCV). We do
NOT scrape. Until that source is wired, the volume channel returns 0 (unknown),
which is fail-safe: volume-based entry boosts and volume-exits simply don't fire
on missing data — they never fire falsely. Plug a provider in here later.

This module is read-only: it never signs or broadcasts.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from web3 import Web3

from ..config import settings
from ..data import price_feed, token_list
from ..monitor.logger import get_logger
from .volume_anomaly_detector import MarketSnapshot

log = get_logger(__name__)

FIVE_MIN_S = 300
DEFAULT_WINDOW_S = 1800          # keep 30 min of price samples per token
RUNTIME = Path(__file__).resolve().parents[2].parent / "data" / "runtime"
DEFAULT_CACHE = RUNTIME / "market_cache.json"

# (symbol) -> (vol_5m, baseline_vol). Returns (0, 0) when volume is unknown.
VolumeProvider = Callable[[str], tuple[float, float]]


def _price_5m_and_min(samples: list[tuple[float, float]], now: float) -> tuple[float, float]:
    """From cached (ts, price) samples: the price ~5 min ago and the recent min."""
    if not samples:
        return 0.0, 0.0
    older = [p for t, p in samples if t <= now - FIVE_MIN_S]
    p5 = older[-1] if older else samples[0][1]
    recent_min = min(p for _, p in samples)
    return p5, recent_min


class MarketFeed:
    def __init__(self, order_usd: float | None = None, *, max_slippage: float | None = None,
                 cache_path: Path | None = None, window_s: int = DEFAULT_WINDOW_S,
                 volume_provider: VolumeProvider | None = None) -> None:
        self.order_usd = settings.default_order_usd if order_usd is None else order_usd
        self.max_slippage = settings.slippage_fraction if max_slippage is None else max_slippage
        self.window_s = window_s
        self.volume_provider = volume_provider
        self.cache_path = cache_path or DEFAULT_CACHE
        self.cache: dict[str, list[tuple[float, float]]] = self._load()
        self._lock = threading.Lock()

    # --- rolling price cache (persisted) ---
    def _load(self) -> dict[str, list[tuple[float, float]]]:
        if not self.cache_path.exists():
            return {}
        raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        return {s: [(float(t), float(p)) for t, p in v] for s, v in raw.items()}

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")

    def _record(self, symbol: str, ts: float, price: float) -> None:
        with self._lock:                       # thread-safe: build_snapshots fans out
            series = self.cache.setdefault(symbol, [])
            series.append((ts, price))
            cutoff = ts - self.window_s
            self.cache[symbol] = [(t, p) for t, p in series if t >= cutoff]

    # --- slippage estimate (read-only on-chain quote at our order size) ---
    def _estimate_slippage(self, token, spot: float, order_usd: float | None = None) -> float:
        order_usd = self.order_usd if order_usd is None else order_usd
        try:
            router = price_feed._router()
            usdt = Web3.to_checksum_address(settings.usdt_address)
            wbnb = Web3.to_checksum_address(settings.wbnb_address)
            amount_tokens = order_usd / spot
            amount_in = int(amount_tokens * 10 ** token.decimals)
            paths = [[token.address, usdt], [token.address, wbnb, usdt]]
            for path in paths:
                try:
                    out = router.functions.getAmountsOut(amount_in, path).call()[-1] / 1e18
                    eff = out / amount_tokens
                    if eff > 0:
                        return max(0.0, (spot - eff) / spot)
                except Exception:  # noqa: BLE001 — try next route
                    continue
        except Exception as e:  # noqa: BLE001
            log.debug("slippage_estimate_failed", symbol=token.symbol, error=str(e))
        return 1.0

    def snapshot(self, symbol: str, price: float | None = None) -> MarketSnapshot:
        try:
            tok = token_list.get_token(symbol)
        except KeyError:
            return MarketSnapshot(symbol=symbol, has_route=False, liquidity_ok=False, slippage_est=1.0)

        if price is None:
            try:
                price = price_feed.onchain_price_usd(symbol)
            except Exception:  # noqa: BLE001
                price = None
        if not price or price <= 0:
            return MarketSnapshot(symbol=symbol, contract=tok.address, has_route=False,
                                  liquidity_ok=False, slippage_est=1.0)

        # Thin memes trade as SMALL "lottery" positions, so gate them at their small
        # order size and a looser slippage ceiling (a meme ride targets +100%, not +5%);
        # deep majors keep the full size + tight gate.
        if token_list.token_class(symbol) == "meme":
            order_usd, max_slip = settings.meme_order_usd, settings.meme_slippage_bps / 10_000
        else:
            order_usd, max_slip = self.order_usd, self.max_slippage
        slippage = self._estimate_slippage(tok, price, order_usd)
        now = time.time()
        self._record(symbol, now, price)
        p5, recent_min = _price_5m_and_min(self.cache.get(symbol, []), now)
        recent_pump = (price - recent_min) / recent_min if recent_min > 0 else 0.0
        vol5, basevol = self.volume_provider(symbol) if self.volume_provider else (0.0, 0.0)

        return MarketSnapshot(
            symbol=symbol, contract=tok.address, vol_5m=vol5, baseline_vol=basevol,
            price_now=price, price_5m_ago=p5, recent_pump_pct=max(0.0, recent_pump),
            slippage_est=slippage, has_route=True, liquidity_ok=slippage <= max_slip,
        )

    def build_snapshots(self, symbols: list[str],
                        prices: dict[str, float] | None = None) -> dict[str, MarketSnapshot]:
        prices = prices or {}
        # Each snapshot does an independent on-chain slippage quote + volume fetch;
        # fan them out so a whole-universe scan stays well under the 60s tick.
        out: dict[str, MarketSnapshot] = {}
        if symbols:
            with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as ex:
                results = ex.map(lambda s: (s, self.snapshot(s, price=prices.get(s))), symbols)
                out = dict(results)
        self.save()
        return out
