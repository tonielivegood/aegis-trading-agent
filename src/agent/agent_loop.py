"""Orchestrator — one trading tick wires every layer together.

    data (balances + quotes)
      -> portfolio valuation (risk)
      -> drawdown update + safeguard evaluation (monitor)
      -> derisk OR momentum strategy (strategy)
      -> execution (PancakeSwap, DRY_RUN-gated)

Runtime state (drawdown peak, trade ledger) persists under data/runtime/ so the
agent survives restarts during the live window.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from .config import settings
from .data import cmc_agent_hub, cmc_client, price_feed, token_list
from .data.token_list import STABLECOINS
from .execution.pancakeswap import PancakeSwap
from .monitor import notifier, pnl
from .monitor.logger import get_logger
from .monitor.safeguard import evaluate
from .risk.daily_breaker import DailyBreaker
from .risk.drawdown import DrawdownTracker
from .risk.portfolio import Portfolio, read_onchain_balances
from .risk.trade_counter import TradeCounter, utcnow
from .strategy import adaptive_hold_strategy, rebalance_strategy
from .strategy.base_strategy import PortfolioState, TradeOrder

log = get_logger(__name__)

RUNTIME = Path(__file__).resolve().parents[2] / "data" / "runtime"
DRAWDOWN_FILE = RUNTIME / "drawdown.json"
TRADES_FILE = RUNTIME / "trades.json"
BASELINE_FILE = RUNTIME / "baseline.json"
POSITIONS_FILE = RUNTIME / "aegis_positions.json"
COMPLIANCE_FILE = RUNTIME / "track1_compliance.json"
COOLDOWN_FILE = RUNTIME / "aegis_cooldown.json"
REGIME_FILE = RUNTIME / "regime.json"
DAYSTATE_FILE = RUNTIME / "daily_breaker.json"
PRICECACHE_FILE = RUNTIME / "last_prices.json"
CMC_SIGNAL_FILE = RUNTIME / "cmc_signal.json"   # cached CMC Agent Hub trending set (hourly)
MACRO_FILE = RUNTIME / "cmc_macro.json"         # cached CMC Agent Hub macro calendar (hourly)
CLAUDE_FILE = RUNTIME / "claude_regime.json"    # cached Claude regime advisory (hourly, for dashboard)
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
STATUS_FILE = WEB_DIR / "status.json"           # public, MASKED snapshot for the live dashboard (no secrets)
LOW_EQUITY_USD = max(5.0, settings.min_portfolio_value_usd * 2)
COMPLIANCE_ORDER_USD = 2.0


def _eligible_token_of(token_in: str, token_out: str) -> tuple[str, str, str] | None:
    """For a USDT<->eligible swap, return (symbol, contract, side) if the traded
    token is in the official allowlist by contract, else None."""
    sym = token_out if token_in in STABLECOINS else token_in
    side = "buy" if token_in in STABLECOINS else "sell"
    if sym in STABLECOINS:
        return None
    try:
        contract = token_list.get_token(sym).contract
    except KeyError:
        return None
    return (sym, contract, side) if token_list.is_eligible(contract) else None


def _apply_min_trade_compliance(orders, state, prices, now_ts):
    """If Track-1 compliance is unmet for today and the strategy is idle late in
    the day, append ONE fully risk-gated minimum trade in the safest eligible
    token. Never bypasses gates; safe-skips if nothing is safe. Additive only."""
    if not settings.track1_compliance_enabled:
        return orders
    from datetime import datetime, timezone

    from .aegis.compliance import ComplianceTracker, pick_compliance_trade
    from .aegis.market_feed import MarketFeed
    from .aegis.positions import PositionBook
    # Already have an eligible trade queued this tick? then today's minimum is in hand.
    if any(_eligible_token_of(o.token_in, o.token_out) for o in orders):
        return orders
    tracker = ComplianceTracker.load(COMPLIANCE_FILE)
    if tracker.valid_today(now_ts) >= settings.track1_min_trades_per_day:
        return orders
    hour = datetime.fromtimestamp(now_ts, tz=timezone.utc).hour
    if hour < settings.track1_compliance_after_hour_utc:
        return orders                              # let real signals trade first
    held = set(PositionBook.load(POSITIONS_FILE).positions)
    order, reason = pick_compliance_trade(state, prices, MarketFeed(volume_provider=None), held=held)
    if order is not None:
        log.info("min_trade_compliance_order", symbol=order.token_out, reason=reason)
        return [*orders, order]
    log.info("compliance_unmet_safe_skip", reason=reason)
    return orders


def _reconcile_failed_entries(results) -> None:
    """Remove phantom positions left by a reverted BUY.

    The position book is opened OPTIMISTICALLY at decision time (in sniper/beta, before the
    swap), so a stable->token entry that reverts on-chain leaves a book entry for a token we
    don't actually hold. The phantom is valued correctly at $0 (balance read), but it occupies
    a sleeve slot until a price-triggered exit self-heals it. Here we close any position whose
    ENTRY (stable->token) swap FAILED this tick — precise: a real prior position is never in
    this tick's failed results, so a good position can't be touched."""
    from .aegis.positions import PositionBook
    phantoms = [r.get("token_out") for r in results
                if "error" in r and r.get("token_in") in STABLECOINS and r.get("token_out")]
    if not phantoms:
        return
    book = PositionBook.load(POSITIONS_FILE)
    changed = False
    for sym in phantoms:
        if book.is_open(sym):
            book.close(sym)
            changed = True
            log.warning("phantom_entry_removed", symbol=sym)
    if changed:
        book.save(POSITIONS_FILE)


def _record_valid_trades(results, now_ts) -> None:
    """Record executed eligible-by-contract trades into the compliance tracker."""
    if not settings.track1_compliance_enabled:
        return
    from .aegis.compliance import ComplianceTracker
    tracker = ComplianceTracker.load(COMPLIANCE_FILE)
    changed = False
    for r in results:
        if "error" in r:
            continue
        info = _eligible_token_of(r.get("token_in", ""), r.get("token_out", ""))
        if not info:
            continue
        sym, contract, side = info
        source = "compliance" if r.get("order") == "MIN_TRADE_COMPLIANCE" else "event"
        tracker.record_executed(symbol=sym, contract=contract,
                                notional_usd=r.get("amount_usd", 0.0), side=side,
                                source=source, reason=r.get("order", ""), now_ts=now_ts)
        changed = True
    if changed:
        tracker.save(COMPLIANCE_FILE)


def _event_mode() -> bool:
    return settings.strategy_mode == "event_alpha" and settings.event_radar_enabled


def _w3w_universe_prices(want: set[str]) -> dict[str, float]:
    """Batch price via Binance W3W price-info — same venue as execution, no CMC
    credit cost, covers runtime-discovered (hot-token) tokens via token_list."""
    from .execution import binance_web3 as bw
    contract_of: dict[str, str] = {}
    for s in want:
        try:
            contract_of[s] = token_list.get_token(s).contract
        except KeyError:
            continue
    try:
        by_addr = bw.price_info(list(contract_of.values())) if contract_of else {}
    except Exception as e:  # noqa: BLE001 — never let a pricing hiccup break the tick
        log.warning("w3w_pricing_failed", error=type(e).__name__)
        by_addr = {}
    prices: dict[str, float] = {}
    for s, c in contract_of.items():
        entry = by_addr.get(c.lower())
        price = entry.get("price") if entry else None
        if price:
            try:
                prices[s] = float(price)
            except (TypeError, ValueError):
                continue
    return prices


def _cmc_universe_prices(want: set[str]) -> dict[str, float]:
    """Legacy pricing path (flag off): CMC by-id, unambiguous but credit-metered."""
    id_of = {s: token_list.cmc_id(s) for s in want}
    ids = [i for i in id_of.values() if i]
    try:
        by_id = cmc_client.get_prices_by_id(ids) if ids else {}
    except Exception as e:  # noqa: BLE001 — never let a pricing hiccup break the tick
        log.warning("cmc_pricing_failed", error=type(e).__name__)
        by_id = {}
    return {s: by_id[i] for s, i in id_of.items() if i and i in by_id}


def _event_prices(symbols: list[str], balances: dict) -> dict[str, float]:
    """USD prices for the universe + anything held (valuation + breakout).

    Post-contest (1/7): priced via Binance W3W `price-info` when
    `binance_w3w_universe_enabled` (default) — same venue as execution, batched,
    no monthly credit cap, and resolves runtime-discovered hot-token candidates.
    Falls back to CMC-by-id when the flag is off. The on-chain DEX-V2 price is
    GARBAGE for tokens whose liquidity lives outside Pancake V2 (AAVE read $0.81 vs
    ~$200), so neither path uses it except for BNB/WBNB (deep, correct V2 pool) and
    as the last-resort fallback below; stablecoins = $1."""
    want = {*symbols, *balances.keys()}
    prices = (_w3w_universe_prices(want) if settings.binance_w3w_universe_enabled
             else _cmc_universe_prices(want))

    wbnb = price_feed.onchain_price_usd("WBNB")
    if wbnb:
        prices["WBNB"] = wbnb
    if "BNB" in balances:
        prices["BNB"] = prices.get("WBNB") or price_feed.onchain_price_usd("BNB") or 0.0
    for stable in STABLECOINS:
        prices.setdefault(stable, 1.0)
    for s in want:                       # held tokens with no CMC id → on-chain best-effort
        if s not in prices and s not in STABLECOINS:
            try:
                p = price_feed.onchain_price_usd(s)
            except Exception:  # noqa: BLE001
                p = None
            if p:
                prices[s] = p
    return prices


def _load_price_cache() -> dict[str, float]:
    if not PRICECACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(PRICECACHE_FILE.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in raw.items() if float(v) > 0}
    except Exception:  # noqa: BLE001 — a corrupt cache must never break a tick
        return {}


def _apply_price_fallback(prices: dict[str, float], balances: dict[str, float]) -> dict[str, float]:
    """Never let a TRANSIENT price-read miss value a HELD token at $0.

    Equity feeds the latched drawdown breaker and every exit; a single failed
    on-chain read (RPC hiccup) would otherwise drop a held token to $0, crater
    equity and (pre-debounce) trip the breaker, or zero an exit's sell size. We
    persist the last good price per symbol and, for any token the wallet still
    HOLDS but couldn't price this tick, fall back to that last-known value. A real
    price (incl. a real crash) always wins — fallback only fills an actual miss."""
    cache = _load_price_cache()
    for sym, p in prices.items():
        if p and p > 0:
            cache[sym] = p                          # refresh cache with fresh good prices
    filled = dict(prices)
    for sym, bal in balances.items():
        if sym in STABLECOINS or bal <= 0:
            continue
        if filled.get(sym, 0.0) <= 0 and cache.get(sym, 0.0) > 0:
            filled[sym] = cache[sym]
            log.warning("price_fallback_last_known", symbol=sym, price=cache[sym])
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        PRICECACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:  # noqa: BLE001 — persistence is best-effort
        pass
    return filled


def _clear_position_book() -> None:
    from .aegis.positions import PositionBook
    book = PositionBook.load(POSITIONS_FILE)
    if book.positions:
        for s in list(book.positions):
            book.close(s)
        book.save(POSITIONS_FILE)


def _volume_provider():
    """Class-routed volume: MAJORS use Binance spot klines, MEMES use Binance Alpha
    klines. Returns a callable(symbol)->(vol, baseline); fail-safe (0,0) per token.
    None only when the whole source is disabled."""
    if settings.volume_source != "binance_alpha_klines":
        return None
    from .aegis.binance_alpha_volume import BinanceAlphaKlinesVolumeProvider
    from .aegis.binance_spot_volume import BinanceSpotKlinesVolumeProvider
    alpha = BinanceAlphaKlinesVolumeProvider()
    majors = {s for s in token_list.alpha_symbols() if token_list.token_class(s) == "major"}
    spot = BinanceSpotKlinesVolumeProvider(symbols=majors)

    def provider(symbol: str) -> tuple[float, float, float | None]:
        # 3-tuple (vol, baseline, 5m move): the move comes from the SAME klines as the
        # volume, so the breakout gate no longer lags on the tick-sampled CMC cache.
        if token_list.token_class(symbol) == "major":
            return spot.volume_and_move(symbol)
        return alpha.volume_and_move(symbol)

    return provider


def _w3w_hot_token_items() -> list[dict] | None:
    """Fetch this tick's server-side-filtered meme candidates (Option B discovery).
    None (not []) on any failure or when the flag is off — the caller falls back to
    the legacy client-side scan; an empty list would instead mean "scanned, found
    nothing", which is a different and wrong signal to send on a network hiccup."""
    if not settings.binance_w3w_universe_enabled:
        return None
    from .aegis import token_class as tc
    from .execution import binance_web3 as bw
    mp = tc.params(tc.MEME)
    try:
        return bw.hot_token(price_change_percent_min=mp.breakout_min * 100)
    except Exception as e:  # noqa: BLE001 — a hiccup here must fall back, never break the tick
        log.warning("w3w_hot_token_failed", error=type(e).__name__)
        return None


def _w3w_safety_check(equity_usd: float):
    """Build the just-in-time honeypot/tax gate for one tick — a fresh `quote()` call
    per candidate (30s TTL, never cached). On pass, registers the token so the rest
    of the pipeline (pricing, execution) can resolve it via token_list.get_token()."""
    from .aegis import sniper
    from .execution import binance_web3 as bw

    def check(sig) -> bool:
        ticket = sniper.meme_ticket_usd(equity_usd)
        amount_wei = str(int(ticket * 10**18))   # USDT has 18 decimals on BSC
        try:
            routes = bw.quote(settings.usdt_address, sig.contract, amount_wei)
        except Exception as e:  # noqa: BLE001 — fail closed: no quote = no entry
            log.warning("w3w_quote_failed", symbol=sig.symbol, error=type(e).__name__)
            return False
        if not routes:
            return False
        best = next((r for r in routes if r.get("isBest")), routes[0])
        to_tok = best.get("toToken") or {}
        if to_tok.get("isHoneyPot"):
            log.warning("w3w_honeypot_blocked", symbol=sig.symbol, contract=sig.contract)
            return False
        try:
            tax = float(to_tok.get("taxRate") or 0)
        except (TypeError, ValueError):
            tax = 1.0
        if tax > settings.binance_w3w_max_tax_rate:
            log.warning("w3w_tax_too_high", symbol=sig.symbol, tax=tax)
            return False
        try:
            decimals = int(to_tok.get("decimal") or 18)
        except (TypeError, ValueError):
            decimals = 18
        token_list.register_discovered(sig.symbol, sig.contract, decimals)
        return True

    return check


def _beta_momentum(symbols: list[str]) -> dict[str, float]:
    """Blended (1h+24h) momentum for the MAJOR symbols, sourced by CMC id (same as the
    price feed → no same-symbol collision). Fail-safe: {} on any error (beta then holds)."""
    from .aegis import beta_core as bc
    majors = [s for s in symbols if token_list.token_class(s) == "major"]
    id_of = {s: token_list.cmc_id(s) for s in majors}
    ids = [i for i in id_of.values() if i]
    if not ids:
        return {}
    try:
        by_id = cmc_client.get_quotes_by_id(ids)
    except Exception as e:  # noqa: BLE001 — never let a momentum fetch break the tick
        log.warning("beta_momentum_failed", error=type(e).__name__)
        return {}
    quotes = {s: by_id[i] for s, i in id_of.items() if i and i in by_id}
    return bc.build_momentum(quotes, w_1h=settings.beta_core_mom_w1h)


def _btc_quote() -> dict:
    """BTC quote for regime classification: CMC primary, CoinGecko FALLBACK (2/7,
    user call) if CMC errors (quota/network) — "2 cái đó tác dụng như nhau" for this
    read, so either source is fine. Never raises; {} on total failure (caller keeps
    the last regime flag, same as any other regime-update failure)."""
    try:
        return cmc_client.get_quotes(["BTC"]).get("BTC", {})
    except Exception as e:  # noqa: BLE001 — CMC hiccup: try the fallback, don't give up yet
        log.warning("cmc_btc_quote_failed", error=type(e).__name__)
        from .data import coingecko_client
        return coingecko_client.get_quotes(["BTC"]).get("BTC", {})


def _maybe_update_regime(now_ts: float):
    """Refresh the regime flag at most once per `regime_update_seconds` (hourly).

    Reads BTC momentum from CMC (CoinGecko fallback) and classifies it (deterministic,
    robust, free). On any failure we keep the last flag — `current_regime` downgrades
    to CAUTIOUS if it goes stale, so a dead updater can never silently keep us aggressive.
    """
    from .aegis import regime as rg
    st = rg.RegimeState.load(REGIME_FILE)
    if now_ts - st.updated_at < settings.regime_update_seconds:
        return st
    try:
        quote = _btc_quote()
        # CMC Agent Hub: market Fear & Greed refines the BTC regime (tightening-only).
        fear_greed = cmc_agent_hub.get_fear_greed()
        flag, reason = rg.decide_regime(quote, fear_greed=fear_greed)
        # Claude regime advisor (hourly, advisory, TIGHTENING-ONLY, fail-safe): an LLM
        # read of BTC + Fear & Greed that can only step risk DOWN, never up.
        from .aegis import claude_advisor
        flag, claude_rec, claude_reason = claude_advisor.advise_regime(
            flag, btc_quote=quote, fear_greed=fear_greed)
        if claude_reason:
            reason = f"{reason}; Claude: {claude_reason}"
        fg_value = (fear_greed or {}).get("value")
        st = rg.RegimeState(flag=flag.value, updated_at=now_ts, reason=reason, fg_value=fg_value)
        st.save(REGIME_FILE)
        log.info("regime_updated", flag=flag.value, reason=reason,
                 fear_greed=(fear_greed or {}).get("value"), claude=claude_rec or None)
        # CMC Agent Hub: refresh the community-trending set for the token-selection bias.
        _refresh_trending(now_ts)
        _refresh_macro(now_ts)          # CMC Agent Hub macro calendar (for the event guard)
        _write_claude_advice(now_ts, claude_rec, claude_reason, flag.value)
    except Exception as e:  # noqa: BLE001 — never let a data hiccup break the tick
        log.info("regime_update_failed", error=type(e).__name__)
    return st


def _refresh_trending(now_ts: float) -> None:
    """Cache the CMC Agent Hub community-trending set to disk so the 60s rails can
    read it without a network call (fully fail-safe — empty set on any error)."""
    try:
        syms = cmc_agent_hub.get_trending_symbols()
        CMC_SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        CMC_SIGNAL_FILE.write_text(
            json.dumps({"trending": sorted(syms), "updated_at": now_ts}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.info("cmc_trending_refresh_failed", error=type(e).__name__)


def _refresh_macro(now_ts: float) -> None:
    """Cache the CMC Agent Hub macro/catalyst calendar to disk so the 60s rails can read
    it without a network call (fully fail-safe — empty list on any error)."""
    if not settings.macro_events_enabled:
        return
    try:
        from .data import cmc_skill_hub
        events = cmc_skill_hub.upcoming_macro_events(limit=8)
        MACRO_FILE.parent.mkdir(parents=True, exist_ok=True)
        MACRO_FILE.write_text(
            json.dumps({"events": events, "updated_at": now_ts}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.info("cmc_macro_refresh_failed", error=type(e).__name__)


def _load_macro(now_ts: float) -> list[dict]:
    """Read the cached CMC macro events (raw {title,date_str,url}); [] if absent or stale,
    so a dead updater silently disables the guard rather than blocking forever."""
    try:
        if not settings.macro_events_enabled or not MACRO_FILE.exists():
            return []
        d = json.loads(MACRO_FILE.read_text(encoding="utf-8"))
        if now_ts - float(d.get("updated_at", 0.0)) > settings.regime_max_age_seconds:
            return []
        return list(d.get("events", []))
    except Exception:  # noqa: BLE001
        return []


def _macro_panel(now_ts: float) -> dict:
    """Dashboard panel for the CMC Agent Hub macro calendar: the next few upcoming
    catalysts (nearest-first) + whether the event guard is currently standing the agent
    down. Fail-safe — empty calendar on any error."""
    try:
        from datetime import datetime, timezone

        from .aegis import macro_calendar
        events = _load_macro(now_ts)
        now = datetime.now(timezone.utc)
        block, reason = macro_calendar.guard(events, now, within_days=settings.macro_guard_days)
        return {"events": macro_calendar.annotate(events, now)[:5],
                "guard": {"block": block, "reason": reason}}
    except Exception:  # noqa: BLE001
        return {"events": [], "guard": {"block": False, "reason": None}}


def _write_claude_advice(now_ts: float, rec: str, reason: str, applied: str) -> None:
    """Cache the hourly Claude regime advisory for the dashboard (fail-safe)."""
    try:
        CLAUDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_FILE.write_text(json.dumps(
            {"recommendation": rec, "reason": reason, "applied": applied,
             "updated_at": now_ts}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.info("claude_advice_write_failed", error=type(e).__name__)


def _load_claude_advice(now_ts: float) -> dict | None:
    """Read the cached Claude advisory; None if absent, stale, or empty."""
    try:
        if not CLAUDE_FILE.exists():
            return None
        d = json.loads(CLAUDE_FILE.read_text(encoding="utf-8"))
        if now_ts - float(d.get("updated_at", 0.0)) > settings.regime_max_age_seconds:
            return None
        return d if d.get("recommendation") else None
    except Exception:  # noqa: BLE001
        return None


def _load_trending(now_ts: float) -> frozenset[str]:
    """Read the cached CMC-trending set; empty if absent or stale (so a dead updater
    silently falls back to pure money-flow ranking)."""
    try:
        if not CMC_SIGNAL_FILE.exists():
            return frozenset()
        d = json.loads(CMC_SIGNAL_FILE.read_text(encoding="utf-8"))
        if now_ts - float(d.get("updated_at", 0.0)) > settings.regime_max_age_seconds:
            return frozenset()
        return frozenset(d.get("trending", []))
    except Exception:  # noqa: BLE001
        return frozenset()


def _write_status_snapshot(equity, drawdown, cum_return, strategy_mode, prices,
                           results, dry_run: bool, now, scan_rows=None) -> None:
    """Write a MASKED, public-safe snapshot for the live dashboard. Fail-safe — never
    raises into the tick. Contains NO secrets: only on-chain/market state already public
    (equity, regime, the CMC Agent Hub reads, open positions, and recent trade hashes)."""
    try:
        from datetime import datetime, timezone

        from .aegis import regime as rg
        from .aegis.positions import PositionBook
        rstate = rg.RegimeState.load(REGIME_FILE)
        fng = cmc_agent_hub.get_fear_greed()
        trending = sorted(_load_trending(time.time()))
        book = PositionBook.load(POSITIONS_FILE)
        positions = []
        for sym, p in book.positions.items():
            px = prices.get(sym, 0.0)
            gain = ((px - p.entry_price) / p.entry_price) if p.entry_price else 0.0
            positions.append({"symbol": sym, "class": p.token_class,
                              "price": px, "gain_pct": round(gain * 100, 1),
                              "usd_size": round(p.usd_size, 2)})
        compliance = None
        try:
            from .aegis.compliance import ComplianceTracker
            rep = ComplianceTracker.load(COMPLIANCE_FILE).report(time.time())
            compliance = {"today": rep.valid_trades_today, "need_today": settings.track1_min_trades_per_day,
                          "week": rep.valid_trades_total, "need_week": settings.track1_min_trades_total}
        except Exception:  # noqa: BLE001
            compliance = None
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        prior: list = []
        equity_hist: list = []
        if STATUS_FILE.exists():
            try:
                _prev = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
                prior = _prev.get("recent_trades", [])
                equity_hist = _prev.get("equity_history", [])
            except Exception:  # noqa: BLE001
                prior, equity_hist = [], []
        equity_hist = (equity_hist + [round(equity, 2)])[-90:]   # ~90 ticks for the sparkline
        fresh = [{"time": ts, "token_in": r.get("token_in"), "token_out": r.get("token_out"),
                  "usd": round(r.get("amount_usd", 0) or 0, 2), "tx": r.get("tx"),
                  "simulated": bool(r.get("simulated", True))}
                 for r in (results or []) if r.get("token_in")]
        recent = (fresh + prior)[:10]
        syms = token_list.alpha_symbols()
        majors = sum(1 for s in syms if token_list.token_class(s) == "major")
        snap = {
            "updated_at": ts,
            "mode": "DRY" if dry_run else "LIVE",
            "strategy": strategy_mode,
            "equity": round(equity, 2),
            "drawdown_pct": round(drawdown.current_drawdown() * 100, 2),
            "return_pct": round(cum_return * 100, 2),
            "regime": {"flag": rstate.flag, "reason": rstate.reason},
            "claude": _load_claude_advice(time.time()),
            "agent_hub": {"fear_greed": fng, "trending": trending},
            "macro": _macro_panel(time.time()),
            "positions": positions,
            "recent_trades": recent,
            "equity_history": equity_hist,
            "scan": scan_rows or [],
            "scan_firing": sum(1 for r in (scan_rows or []) if r.get("fires")),
            "compliance": compliance,
            "universe": {"total": len(syms), "majors": majors, "memes": len(syms) - majors},
            "backend": settings.execution_backend,
            "wallet": settings.agent_wallet_address,
            "breaker": {"alert_pct": round(settings.max_drawdown_alert * 100),
                        "cap_pct": round(settings.max_drawdown_cap * 100)},
        }
        WEB_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(snap), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — dashboard export must never break a tick
        log.info("status_snapshot_failed", error=type(e).__name__)


def _contest_end_epoch() -> float:
    """Configured contest-end ISO → epoch seconds. Fail-safe: an unparseable value
    yields a far-future time → the clock reads 'too early' → inactive (never escalates)."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(settings.tournament_clock_end.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return time.time() + 365 * 86400


def _apply_clock(clock, *, meme_cap, base_max_slots, entry_flag, flag, meme_order_usd):
    """Apply a tournament-clock directive to the MEME sleeve only. Inactive => everything
    unchanged (a disabled/off-window clock is byte-identical to no clock). Returns
    (meme_cap, meme_usd, meme_flag, label). Escalation may raise the lottery cap ABOVE the
    regime cap and, in a daily-halt, hand the memes the true regime flag (beta stays halted)."""
    if not clock.active:
        return meme_cap, meme_order_usd, entry_flag, ""
    base = base_max_slots if meme_cap is None else meme_cap
    meme_flag = flag if clock.relax_meme_breaker else entry_flag
    return (base + clock.extra_meme_slots, meme_order_usd * clock.meme_ticket_mult,
            meme_flag, f"+clock:{clock.reason}")


def _event_decision(state: PortfolioState, prices: dict, symbols: list[str],
                    block_entries: bool = False, our_return: float = 0.0,
                    current_dd: float = 0.0):
    """Run the v2 sniper decision (volume breakout + regime valve + cooldown),
    persisting the position book + cooldown. DRY_RUN-safe; never broadcasts here.

    `block_entries` (daily soft breaker) forces the entry valve to RISK_OFF so NO new
    positions are opened this tick — at the SOURCE, so the book is never mutated with a
    buy we won't execute (no phantom positions). Exits/stops still run unconditionally."""
    from .aegis import regime as rg
    from .aegis import sniper
    from .aegis import tournament_clock as tclock
    from .aegis.cooldown import CooldownBook
    from .aegis.market_feed import MarketFeed
    from .aegis.positions import PositionBook
    now_ts = time.time()
    book = PositionBook.load(POSITIONS_FILE)
    cooldowns = CooldownBook.load(COOLDOWN_FILE)
    feed = MarketFeed(volume_provider=_volume_provider())
    rstate = _maybe_update_regime(now_ts)
    flag = rg.current_regime(rstate, max_age_s=settings.regime_max_age_seconds, now=now_ts)
    entry_flag = rg.Regime.RISK_OFF if block_entries else flag   # daily breaker: entries off
    trending = _load_trending(now_ts)

    # BARBELL: when the beta core is enabled it OWNS majors (momentum-basket hold) and the
    # sniper handles MEMES only (lottery). Beta runs first; we then (a) record its exits into
    # the cooldown and (b) shrink the stable cash the meme sleeve sees by beta's net deploy,
    # so the two sleeves don't double-spend the same USDT. block_entries (daily breaker)
    # suppresses NEW entries in both without flattening existing holds.
    beta_orders: list[TradeOrder] = []
    beta_label = ""
    sniper_state = state
    sniper_classes: set[str] | None = None
    meme_cap: int | None = None
    if settings.beta_core_enabled:
        from .aegis import beta_core as bc
        cooling = cooldowns.cooling_down(now=now_ts, cooldown_s=settings.aegis_cooldown_seconds)
        base_floor = max(settings.stablecoin_floor_usd, state.equity_usd * settings.stablecoin_floor_pct)
        # Beta-specific regime: extreme Fear & Greed forces beta to RISK_OFF even from
        # CAUTIOUS (the 24/6 whipsaw fix — trend-following must not run through fear-driven
        # chop). The meme sniper's own regime (`flag`) is untouched by this.
        beta_flag = rg.beta_regime(flag, rstate.fg_value)
        # GLOBAL concurrent-position cap (shared across BOTH sleeves): RISK_ON N / CAUTIOUS 1 /
        # RISK_OFF 0. Alts are highly correlated to BTC, so total exposure — not per-sleeve — is
        # what a BTC dump hits; capping TOTAL positions is the real DD-gate control. Beta fills
        # the cap first (majors), memes get whatever slots remain (often 0-1). Graduated by
        # regime so the agent flexes with the market, never betting a direction.
        cap = (settings.max_concurrent_positions if flag == rg.Regime.RISK_ON
               else 1 if flag == rg.Regime.CAUTIOUS else 0)
        beta_names = (settings.beta_core_max_names if beta_flag == rg.Regime.RISK_ON
                      else 1 if beta_flag == rg.Regime.CAUTIOUS else 0)
        beta_max = min(beta_names, cap)
        # Reserve ONE meme ticket whenever memes can trade this regime (0 in RISK_OFF). At
        # ~$33 NAV, reserving 2 tickets ($10) starved beta below 2 names; reserving 1 ($5)
        # lets beta hold 2 majors while memes still get at least one lottery shot (a 2nd meme
        # is opportunistic from cash left after beta). Scales naturally as equity grows.
        meme_reserve = sniper.meme_ticket_usd(state.equity_usd) if rg.params(flag).max_slots > 0 else 0.0
        beta_orders, beta_mode = bc.decide_beta(
            state, prices, _beta_momentum(symbols), book=book, regime_flag=beta_flag, now=now_ts,
            max_names=beta_max,
            position_usd=state.equity_usd * settings.beta_core_position_pct,
            floor_usd=base_floor + meme_reserve,
            min_momentum=settings.beta_core_min_momentum, trail_pct=settings.beta_core_trail_pct,
            hard_tp_pct=settings.beta_core_hard_tp_pct,
            hard_stop_pct=settings.beta_core_hard_stop_pct,
            breakeven_trigger=settings.aegis_breakeven_trigger_pct,
            breakeven_buffer=settings.aegis_breakeven_buffer_pct,
            exit_min_momentum=settings.beta_core_exit_min_momentum,
            rotation_margin=settings.beta_core_rotation_margin,
            min_hold_sec=settings.beta_core_min_hold_sec, cooldown_symbols=cooling,
            block_entries=block_entries)
        for o in beta_orders:
            if o.token_out == "USDT":              # an exit (sold back to stable) → cooldown
                cooldowns.record_exit(o.token_in, now_ts)
        net_beta = (sum(o.amount_in_usd for o in beta_orders if o.token_in == "USDT")
                    - sum(o.amount_in_usd for o in beta_orders if o.token_out == "USDT"))
        sniper_state = replace(state, stable_value_usd=max(0.0, state.stable_value_usd - net_beta))
        sniper_classes = {"meme"}                  # beta owns majors → sniper handles memes only
        # Memes get only the slots left under the GLOBAL cap after beta took its majors.
        majors_held = sum(1 for p in book.positions.values() if p.token_class == "major")
        meme_cap = max(0, cap - majors_held)
        beta_label = f"+beta:{beta_mode}"

    # Tournament clock (#3): in the final window AND only while NOT yet likely in a paying
    # spot, ESCALATE the meme lottery sleeve (extra slots + bigger ticket + ignore the daily
    # breaker) — never beta. Gated by tournament_clock_enabled; inactive => byte-identical.
    clock = tclock.decide_clock(
        now=now_ts, contest_end=_contest_end_epoch(), our_return=our_return,
        current_dd=current_dd, regime_flag=flag, enabled=settings.tournament_clock_enabled,
        arm_days=settings.tournament_clock_arm_days,
        full_send_days=settings.tournament_clock_full_send_days,
        safe_return=settings.tournament_clock_safe_return,
        max_push_dd=settings.tournament_clock_max_push_dd,
        extra_slots_arm=settings.tournament_clock_extra_slots_arm,
        extra_slots_full=settings.tournament_clock_extra_slots_full,
        ticket_mult_arm=settings.tournament_clock_ticket_mult_arm,
        ticket_mult_full=settings.tournament_clock_ticket_mult_full)
    meme_cap, meme_usd, meme_flag, clock_label = _apply_clock(
        clock, meme_cap=meme_cap, base_max_slots=rg.params(entry_flag).max_slots,
        entry_flag=entry_flag, flag=flag, meme_order_usd=sniper.meme_ticket_usd(state.equity_usd))

    hot_token_items = _w3w_hot_token_items()
    # A hot-token-discovered contract isn't in the static 149/tradable-alpha files (that
    # gate is what's being retired), so it needs a permissive `allow` here — the REAL
    # safety gate for these candidates is `_w3w_safety_check` (fresh honeypot/tax quote),
    # not local list membership.
    entry_allow = (lambda c: bool(c)) if hot_token_items is not None else None
    orders, mode = sniper.run(sniper_state, prices, book=book, feed=feed, cooldowns=cooldowns,
                              regime_flag=meme_flag, universe=symbols, now=now_ts, trending=trending,
                              max_meme_positions=meme_cap, meme_usd=meme_usd,
                              manage_classes=sniper_classes, allow=entry_allow,
                              hot_token_items=hot_token_items,
                              safety_check=_w3w_safety_check(state.equity_usd) if hot_token_items is not None else None)
    book.save(POSITIONS_FILE)
    cooldowns.prune(now=now_ts, cooldown_s=settings.aegis_cooldown_seconds)
    cooldowns.save(COOLDOWN_FILE)
    label = f"{mode}:{flag.value}" + beta_label + clock_label + (":dayhalt" if block_entries else "")
    return beta_orders + orders, label, _scan_rows(feed.last_snapshots)


def _scan_rows(snapshots, limit: int = 8) -> list[dict]:
    """Top scan candidates for the dashboard — ranked by volume multiple. Shows WHY a
    high-volume token is or isn't firing (a flat price = no confirmed move = no entry)."""
    from .aegis import token_class as tc
    from .aegis.volume_breakout import breakout_pct
    rows = []
    for sym, s in (snapshots or {}).items():
        if s.baseline_vol <= 0 or not s.has_route:
            continue
        vol_x = s.vol_5m / s.baseline_vol
        bo = breakout_pct(s)   # same source the live entry uses (kline move when available)
        cls = token_list.token_class(sym)
        cp = tc.params(cls)
        fires = (vol_x >= cp.vol_mult and cp.breakout_min <= bo <= cp.breakout_max
                 and s.liquidity_ok)
        # NOTE: do NOT publish cp.vol_mult / breakout bounds here — status.json is a
        # PUBLIC file and the exact entry thresholds are the strategy edge ("don't lộ
        # bài"). The dashboard renders the bar from vol_x alone; `fires` is the only
        # threshold-derived field exposed, and it can't be inverted to the bar.
        rows.append({"symbol": sym, "class": cls, "vol_x": round(vol_x, 1),
                     "bo_pct": round(bo * 100, 1), "fires": fires})
    rows.sort(key=lambda r: r["vol_x"], reverse=True)
    return rows[:limit]


def _load_drawdown() -> DrawdownTracker:
    dt = DrawdownTracker(settings.max_drawdown_alert, settings.max_drawdown_cap,
                         latch_ticks=settings.drawdown_latch_ticks)
    if DRAWDOWN_FILE.exists():
        d = json.loads(DRAWDOWN_FILE.read_text(encoding="utf-8"))
        dt.peak = d.get("peak", 0.0)
        dt._tripped = d.get("tripped", False)
        dt._breach_streak = d.get("breach_streak", 0)
    return dt


def _save_drawdown(dt: DrawdownTracker) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    DRAWDOWN_FILE.write_text(
        json.dumps({"peak": dt.peak, "tripped": dt._tripped, "breach_streak": dt._breach_streak}),
        encoding="utf-8")


def _baseline_equity(current_equity: float) -> float:
    """Starting equity for PnL — captured on the first tick and persisted, so
    cumulative return is measured against actual starting capital (not a static
    budget that may not match what's funded)."""
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))["start_equity"]
    RUNTIME.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps({"start_equity": current_equity}), encoding="utf-8")
    return current_equity


def _build_prices(symbols: list[str], quotes: dict, balances: dict) -> dict[str, float]:
    prices = {s: q["price"] for s, q in quotes.items() if q.get("price")}
    for stable in STABLECOINS:
        prices.setdefault(stable, 1.0)
    if "BNB" in balances:
        prices["BNB"] = prices.get("WBNB") or price_feed.onchain_price_usd("BNB") or 0.0
    return prices


def _amount_in_tokens(order: TradeOrder, prices: dict[str, float]) -> float:
    price = prices.get(order.token_in, 0.0)
    return order.amount_in_usd / price if price > 0 else 0.0


def tick(dry_run: bool | None = None) -> dict:
    dry_run = settings.dry_run if dry_run is None else dry_run
    now = utcnow()
    event_mode = _event_mode()

    balances = read_onchain_balances(settings.agent_wallet_address)
    if event_mode:
        # Primary strategy: trade the liquid eligible (Alpha) universe, priced on-chain.
        symbols = token_list.alpha_symbols()
        prices = _event_prices(symbols, balances)
    else:
        symbols = token_list.tradable_symbols()
        quotes = cmc_client.get_quotes([s for s in symbols if s != "WBNB"] + ["WBNB"])
        prices = _build_prices(symbols, quotes, balances)

    # Robust valuation: a transient price-read miss must not value a held token at
    # $0 (which would crater equity, glitch the breaker, and zero exit sizing).
    prices = _apply_price_fallback(prices, balances)

    pf = Portfolio()
    equity = pf.equity(balances, prices)              # full wallet value (incl native BNB) for PnL
    stable_value = pf.stable_value(balances, prices)
    # Tradable holdings exclude native BNB: it is the gas reserve and is not an
    # ERC-20 the router can swap directly (only WBNB is).
    token_values = {
        s: balances[s] * prices.get(s, 0.0)
        for s in balances if s in symbols
    }
    # Risk = non-stable tradable holdings only (native gas BNB is NOT deployable
    # capital, so it must not count toward deployed risk).
    risk_value = sum(v for s, v in token_values.items() if s not in STABLECOINS)

    drawdown = _load_drawdown()
    drawdown.update(equity)
    # Daily soft breaker: anchor today's open equity and decide whether intraday bleed has
    # hit the threshold (→ block NEW entries for the rest of the UTC day; exits still run).
    day = DailyBreaker.load(DAYSTATE_FILE)
    day.roll(equity, now.strftime("%Y-%m-%d"))
    day.save(DAYSTATE_FILE)
    daily_halt = day.should_halt_new(equity, settings.daily_soft_breaker_pct)
    # CMC Agent Hub macro guard: stand DOWN (halt new entries) into an imminent high-volatility
    # catalyst from CMC's macro calendar. Tightening-only + fail-safe (no/distant events ⇒ no
    # halt; exits always run). One more Agent Hub skill driving a real decision.
    from .aegis import macro_calendar
    macro_halt, macro_reason = macro_calendar.guard(
        _load_macro(now.timestamp()), now, within_days=settings.macro_guard_days)
    if macro_halt:
        log.info("macro_guard_halt", reason=macro_reason)
    daily_halt = daily_halt or macro_halt
    trade_counter = TradeCounter.load(TRADES_FILE)

    state = PortfolioState(
        equity_usd=equity, risk_value_usd=risk_value, stable_value_usd=stable_value,
        token_values_usd=token_values,
        drawdown_tripped=drawdown.breaker_tripped(), cap_breached=drawdown.cap_breached(),
    )

    action = evaluate(state, drawdown, trade_counter, now,
                      min_trade_interval_h=settings.min_trade_interval_h,
                      low_equity_usd=LOW_EQUITY_USD)

    scan_rows: list[dict] = []
    if action.derisk:
        orders = rebalance_strategy.derisk_orders(state)
        mode = "derisk"
        if event_mode:
            _clear_position_book()          # flatten simulated event positions too
    elif event_mode:
        # Layer B primary (event radar) with Layer A (eligible basket) fallback.
        # daily_halt blocks new entries at the SOURCE (no phantom book positions).
        orders, mode, scan_rows = _event_decision(
            state, prices, symbols, block_entries=daily_halt,
            our_return=pnl.cumulative_return(_baseline_equity(equity), equity),
            current_dd=drawdown.current_drawdown())
        if action.halt_buys:
            orders = [o for o in orders if o.token_in not in STABLECOINS]
    else:
        # Baseline: fractional diversified hold + breaker on the majors basket.
        mode = "baseline-hold"
        basket = token_list.basket_symbols(settings.basket_size)
        orders = adaptive_hold_strategy.decide(state, basket, settings.deploy_frac)
        if action.halt_buys or daily_halt:   # baseline is stateless → safe to strip buys here
            orders = [o for o in orders if o.token_in not in STABLECOINS]
        if action.needs_compliance_trade and not orders and settings.track1_compliance_enabled:
            orders = _compliance_orders(state)

    # Track-1 min-trade compliance (additive; event mode only, never bypasses gates).
    if event_mode and not action.derisk:
        orders = _apply_min_trade_compliance(orders, state, prices, time.time())

    # Gas guard: if native BNB is too low to reliably pay for EXITS, stop opening
    # new positions (buys spend a stablecoin) so we never get stuck unable to sell.
    if not action.derisk and balances.get("BNB", 0.0) < settings.min_gas_bnb:
        buys = [o for o in orders if o.token_in in STABLECOINS]
        if buys:
            log.warning("low_gas_bnb_block_buys", bnb=round(balances.get("BNB", 0.0), 5),
                        n_blocked=len(buys))
            orders = [o for o in orders if o.token_in not in STABLECOINS]

    if daily_halt:
        log.warning("daily_soft_breaker_active", intraday_dd=round(day.drawdown(equity), 4),
                    threshold=settings.daily_soft_breaker_pct, day_open=round(day.open_equity, 2))
    cum_return = pnl.cumulative_return(_baseline_equity(equity), equity)
    log.info("tick", equity=round(equity, 2), drawdown=round(drawdown.current_drawdown(), 4),
             cumulative_return=round(cum_return, 4), strategy=mode,
             safeguard=action.reason, n_orders=len(orders), dry_run=dry_run)

    results = _execute(orders, prices, dry_run, trade_counter, now)
    if event_mode:
        _record_valid_trades(results, time.time())
        _reconcile_failed_entries(results)   # drop phantom positions from reverted buys

    trade_counter.save(TRADES_FILE)
    _save_drawdown(drawdown)
    _notify(action, results, equity, drawdown, cum_return, now)
    _write_status_snapshot(equity, drawdown, cum_return, mode, prices, results, dry_run, now, scan_rows)
    return {"equity": equity, "action": action.reason, "orders": len(orders), "results": results}


def flatten_to_cash(dry_run: bool | None = None) -> dict:
    """KILL-SWITCH: immediately sell every non-stable holding to USDT and clear the
    position + cooldown books. Independent of strategy and breaker — for a manual
    emergency halt. Honors DRY_RUN unless explicitly overridden (CLI passes False)."""
    dry_run = settings.dry_run if dry_run is None else dry_run
    now = utcnow()
    balances = read_onchain_balances(settings.agent_wallet_address)
    prices = _apply_price_fallback(_event_prices(token_list.alpha_symbols(), balances), balances)
    pf = Portfolio()
    # Value EVERY held token (not just the alpha universe) so nothing is left behind;
    # native BNB stays as gas (it is not a directly swappable ERC-20).
    token_values = {s: balances[s] * prices.get(s, 0.0)
                    for s in balances if s != "BNB" and prices.get(s, 0.0) > 0}
    state = PortfolioState(
        equity_usd=pf.equity(balances, prices), risk_value_usd=0.0,
        stable_value_usd=pf.stable_value(balances, prices), token_values_usd=token_values)
    orders = rebalance_strategy.derisk_orders(state)
    trade_counter = TradeCounter.load(TRADES_FILE)
    results = _execute(orders, prices, dry_run, trade_counter, now)
    trade_counter.save(TRADES_FILE)
    _clear_position_book()
    if COOLDOWN_FILE.exists():
        COOLDOWN_FILE.unlink()
    log.warning("flatten_to_cash", n_orders=len(orders), dry_run=dry_run,
                equity=round(state.equity_usd, 2))
    return {"orders": len(orders), "results": results, "dry_run": dry_run}


_last_heartbeat_hour: dict = {"h": None}


def _notify(action, results, equity, drawdown, cum_return, now) -> None:
    """Best-effort Telegram alerts. Never raises (alerts must not break trading)."""
    try:
        if action.derisk:
            notifier.send(notifier.format_breaker(equity, drawdown.current_drawdown()))
        live = sum(1 for r in results if not r.get("simulated", True) and "error" not in r)
        if live:
            notifier.send(notifier.format_trades(live, equity))
        if _last_heartbeat_hour["h"] != now.hour:
            _last_heartbeat_hour["h"] = now.hour
            notifier.send(notifier.format_heartbeat(equity, drawdown.current_drawdown(), cum_return))
    except Exception:  # noqa: BLE001
        pass


def _compliance_orders(state: PortfolioState) -> list[TradeOrder]:
    """Fallback min-trade order. It MUST be an eligible-by-contract trade (in the
    149 allowlist) or it does not count — so we sell a held ELIGIBLE token, or buy
    the safest eligible token. NEVER WBNB, which is not in the 149 (the old bug)."""
    # Prefer selling a held non-stable ELIGIBLE token back to USDT.
    for sym, val in state.token_values_usd.items():
        if sym in STABLECOINS or val < COMPLIANCE_ORDER_USD:
            continue
        try:
            contract = token_list.get_token(sym).contract
        except KeyError:
            continue
        if token_list.is_eligible(contract):
            return [TradeOrder(sym, "USDT", COMPLIANCE_ORDER_USD, "min-trade compliance")]
    # Else buy the safest eligible (liquid, tradable) token — first by liquidity.
    if state.stable_value_usd >= COMPLIANCE_ORDER_USD:
        eligible = token_list.alpha_symbols()
        if eligible:
            return [TradeOrder("USDT", eligible[0], COMPLIANCE_ORDER_USD, "min-trade compliance")]
    return []




def _make_executor_for(backend: str, dry_run: bool):
    """Build a named execution backend. PancakeSwap (default) trades on-chain on the
    registered wallet (battle-tested). 'openocean'/'1inch' route through a DEX AGGREGATOR
    (best price across all BSC DEXs → far lower slippage, much larger tradable universe);
    they return ready-to-sign calldata that we sign LOCALLY (self-custody preserved).
    'twak' routes through the Trust Wallet Agent Kit CLI (its own local wallet)."""
    if backend == "twak":
        from .execution.twak_executor import TwakExecutor
        return TwakExecutor(dry_run=dry_run)
    account = None
    if not dry_run:
        from eth_account import Account
        account = Account.from_key(settings.agent_private_key)
    if backend == "openocean":
        from .execution.openocean import OpenOcean
        return OpenOcean(account=account, dry_run=dry_run)
    if backend in ("1inch", "oneinch"):
        from .execution.oneinch import OneInch
        return OneInch(account=account, dry_run=dry_run)
    return PancakeSwap(account=account, dry_run=dry_run)


def _make_executor(dry_run: bool):
    """The configured primary execution backend."""
    return _make_executor_for(settings.execution_backend, dry_run)


_ROUTABLE_BACKENDS = ("1inch", "openocean", "pancake")   # every backend the live router can quote


def _execute(orders, prices, dry_run, trade_counter, now) -> list[dict]:
    if not orders:
        return []
    configured = settings.execution_backend
    executors: dict[str, object] = {}                       # built lazily, reused this tick

    def _dex(backend):
        if backend not in executors:
            executors[backend] = _make_executor_for(backend, dry_run)
        return executors[backend]

    results = []
    for o in orders:
        amount_in = _amount_in_tokens(o, prices)
        if amount_in <= 0:
            continue
        is_exit = o.token_out in STABLECOINS              # selling to stable = closing a position
        if configured == "twak":
            backends = ["twak"]                            # separate wallet → never crosses over
        else:
            # Flexible venue selection (2/7, user call): quote every aggregator LIVE
            # for THIS token/size and prefer whichever has the best liquidity right
            # now, instead of a fixed configured primary. Falls back to the configured
            # backend if every live quote fails (network hiccup, no route yet).
            from .execution import best_execution
            ranked = best_execution.rank_backends(
                {b: _dex(b) for b in _ROUTABLE_BACKENDS}, o.token_in, o.token_out, amount_in)
            best = ranked[0] if ranked else configured
            if is_exit:
                # EXIT is non-negotiable: fail over through the rest of the ranking,
                # then any backend the live ranking couldn't quote, as a last resort.
                rest = ranked[1:] + [b for b in _ROUTABLE_BACKENDS if b != best and b not in ranked]
                backends = [best, *rest]
            else:
                backends = [best]        # ENTRY: best-liquidity venue only, no failover (price discipline)
        last_err = None
        for attempt, backend in enumerate(backends):
            try:
                r = _dex(backend).swap(o.token_in, o.token_out, amount_in)
                if not r.simulated:
                    trade_counter.record_trade(now)
                row = {"order": o.reason, "simulated": r.simulated, "tx": r.tx_hash,
                       "token_in": o.token_in, "token_out": o.token_out,
                       "amount_usd": o.amount_in_usd, "backend": backend}
                if attempt > 0:                            # the top choice had failed → record the save
                    row["failover_backend"] = backend
                    log.warning("swap_failover_ok", reason=o.reason, backend=backend)
                results.append(row)
                break
            except Exception as e:  # noqa: BLE001 — one failed swap must not abort the tick
                last_err = e
                log.warning("swap_failed", reason=o.reason, backend=backend, error=str(e))
        else:                                               # every backend (incl. failover) failed
            results.append({"order": o.reason, "error": str(last_err),
                            "token_in": o.token_in, "token_out": o.token_out})
            if is_exit:                                     # a stuck EXIT is a real DD risk → page now
                _alert_exit_failure(o, str(last_err))
    return results


def _alert_exit_failure(order, error: str) -> None:
    """Page Telegram when an EXIT (stop / trailing / derisk) could not execute on ANY
    backend — the position is still open and unprotected, so the operator must know
    immediately. Best-effort: never raises into the tick."""
    try:
        notifier.send(notifier.format_exit_failure(order.token_in, order.reason, error))
    except Exception:  # noqa: BLE001
        pass
