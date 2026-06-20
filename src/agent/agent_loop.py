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
from pathlib import Path

from .config import settings
from .data import cmc_agent_hub, cmc_client, price_feed, token_list
from .data.token_list import STABLECOINS
from .execution.pancakeswap import PancakeSwap
from .monitor import notifier, pnl
from .monitor.logger import get_logger
from .monitor.safeguard import evaluate
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
PRICECACHE_FILE = RUNTIME / "last_prices.json"
CMC_SIGNAL_FILE = RUNTIME / "cmc_signal.json"   # cached CMC Agent Hub trending set (hourly)
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


def _event_prices(symbols: list[str], balances: dict) -> dict[str, float]:
    """USD prices for the universe + anything held (valuation + breakout).

    Priced via CMC by CMC-id (unambiguous, accurate). The on-chain DEX-V2 price is
    GARBAGE for tokens whose liquidity lives outside Pancake V2 (AAVE read $0.81 vs
    ~$200) — and since we now EXECUTE through an aggregator, the CMC fair price is both
    correct and coherent with the actual fill. BNB/WBNB keep their on-chain price (deep,
    correct V2 pool); stablecoins = $1; anything left unpriced falls back on-chain."""
    want = {*symbols, *balances.keys()}
    id_of = {s: token_list.cmc_id(s) for s in want}
    ids = [i for i in id_of.values() if i]
    try:
        by_id = cmc_client.get_prices_by_id(ids) if ids else {}
    except Exception as e:  # noqa: BLE001 — never let a pricing hiccup break the tick
        log.warning("cmc_pricing_failed", error=type(e).__name__)
        by_id = {}
    prices = {s: by_id[i] for s, i in id_of.items() if i and i in by_id}

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

    def provider(symbol: str) -> tuple[float, float]:
        if token_list.token_class(symbol) == "major":
            return spot.volume_tuple(symbol)
        return alpha.volume_tuple(symbol)

    return provider


def _maybe_update_regime(now_ts: float):
    """Refresh the regime flag at most once per `regime_update_seconds` (hourly).

    Reads BTC momentum from CMC and classifies it (deterministic, robust, free).
    On any failure we keep the last flag — `current_regime` downgrades to CAUTIOUS
    if it goes stale, so a dead updater can never silently keep us aggressive.
    """
    from .aegis import regime as rg
    st = rg.RegimeState.load(REGIME_FILE)
    if now_ts - st.updated_at < settings.regime_update_seconds:
        return st
    try:
        quote = cmc_client.get_quotes(["BTC"]).get("BTC", {})
        # CMC Agent Hub: market Fear & Greed refines the BTC regime (tightening-only).
        fear_greed = cmc_agent_hub.get_fear_greed()
        flag, reason = rg.decide_regime(quote, fear_greed=fear_greed)
        st = rg.RegimeState(flag=flag.value, updated_at=now_ts, reason=reason)
        st.save(REGIME_FILE)
        log.info("regime_updated", flag=flag.value, reason=reason,
                 fear_greed=(fear_greed or {}).get("value"))
        # CMC Agent Hub: refresh the community-trending set for the token-selection bias.
        _refresh_trending(now_ts)
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


def _event_decision(state: PortfolioState, prices: dict, symbols: list[str]):
    """Run the v2 sniper decision (volume breakout + regime valve + cooldown),
    persisting the position book + cooldown. DRY_RUN-safe; never broadcasts here."""
    from .aegis import regime as rg
    from .aegis import sniper
    from .aegis.cooldown import CooldownBook
    from .aegis.market_feed import MarketFeed
    from .aegis.positions import PositionBook
    now_ts = time.time()
    book = PositionBook.load(POSITIONS_FILE)
    cooldowns = CooldownBook.load(COOLDOWN_FILE)
    feed = MarketFeed(volume_provider=_volume_provider())
    rstate = _maybe_update_regime(now_ts)
    flag = rg.current_regime(rstate, max_age_s=settings.regime_max_age_seconds, now=now_ts)
    trending = _load_trending(now_ts)
    orders, mode = sniper.run(state, prices, book=book, feed=feed, cooldowns=cooldowns,
                              regime_flag=flag, universe=symbols, now=now_ts, trending=trending)
    book.save(POSITIONS_FILE)
    cooldowns.prune(now=now_ts, cooldown_s=settings.aegis_cooldown_seconds)
    cooldowns.save(COOLDOWN_FILE)
    return orders, f"{mode}:{flag.value}"


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
    trade_counter = TradeCounter.load(TRADES_FILE)

    state = PortfolioState(
        equity_usd=equity, risk_value_usd=risk_value, stable_value_usd=stable_value,
        token_values_usd=token_values,
        drawdown_tripped=drawdown.breaker_tripped(), cap_breached=drawdown.cap_breached(),
    )

    action = evaluate(state, drawdown, trade_counter, now,
                      min_trade_interval_h=settings.min_trade_interval_h,
                      low_equity_usd=LOW_EQUITY_USD)

    if action.derisk:
        orders = rebalance_strategy.derisk_orders(state)
        mode = "derisk"
        if event_mode:
            _clear_position_book()          # flatten simulated event positions too
    elif event_mode:
        # Layer B primary (event radar) with Layer A (eligible basket) fallback.
        orders, mode = _event_decision(state, prices, symbols)
        if action.halt_buys:
            orders = [o for o in orders if o.token_in not in STABLECOINS]
    else:
        # Baseline: fractional diversified hold + breaker on the majors basket.
        mode = "baseline-hold"
        basket = token_list.basket_symbols(settings.basket_size)
        orders = adaptive_hold_strategy.decide(state, basket, settings.deploy_frac)
        if action.halt_buys:
            orders = [o for o in orders if o.token_in not in STABLECOINS]
        if action.needs_compliance_trade and not orders:
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

    cum_return = pnl.cumulative_return(_baseline_equity(equity), equity)
    log.info("tick", equity=round(equity, 2), drawdown=round(drawdown.current_drawdown(), 4),
             cumulative_return=round(cum_return, 4), strategy=mode,
             safeguard=action.reason, n_orders=len(orders), dry_run=dry_run)

    results = _execute(orders, prices, dry_run, trade_counter, now)
    if event_mode:
        _record_valid_trades(results, time.time())

    trade_counter.save(TRADES_FILE)
    _save_drawdown(drawdown)
    _notify(action, results, equity, drawdown, cum_return, now)
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


def _make_executor(dry_run: bool):
    """Select the execution backend. Default PancakeSwap on the registered wallet
    (battle-tested). 'openocean'/'1inch' route through a DEX AGGREGATOR (best price
    across all BSC DEXs → far lower slippage, much larger tradable universe); they
    return ready-to-sign calldata that we sign LOCALLY (self-custody preserved).
    'twak' routes through the Trust Wallet Agent Kit CLI (its own local wallet)."""
    backend = settings.execution_backend
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


def _execute(orders, prices, dry_run, trade_counter, now) -> list[dict]:
    if not orders:
        return []
    dex = _make_executor(dry_run)

    results = []
    for o in orders:
        amount_in = _amount_in_tokens(o, prices)
        if amount_in <= 0:
            continue
        try:
            r = dex.swap(o.token_in, o.token_out, amount_in)
            if not r.simulated:
                trade_counter.record_trade(now)
            results.append({"order": o.reason, "simulated": r.simulated, "tx": r.tx_hash,
                            "token_in": o.token_in, "token_out": o.token_out,
                            "amount_usd": o.amount_in_usd})
        except Exception as e:  # noqa: BLE001 — one failed swap must not abort the tick
            log.warning("swap_failed", reason=o.reason, error=str(e))
            results.append({"order": o.reason, "error": str(e),
                            "token_in": o.token_in, "token_out": o.token_out})
    return results
