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
from pathlib import Path

from .config import settings
from .data import cmc_client, price_feed, token_list
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
LOW_EQUITY_USD = max(5.0, settings.min_portfolio_value_usd * 2)
COMPLIANCE_ORDER_USD = 2.0


def _load_drawdown() -> DrawdownTracker:
    dt = DrawdownTracker(settings.max_drawdown_alert, settings.max_drawdown_cap)
    if DRAWDOWN_FILE.exists():
        d = json.loads(DRAWDOWN_FILE.read_text(encoding="utf-8"))
        dt.peak = d.get("peak", 0.0)
        dt._tripped = d.get("tripped", False)
    return dt


def _save_drawdown(dt: DrawdownTracker) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    DRAWDOWN_FILE.write_text(json.dumps({"peak": dt.peak, "tripped": dt._tripped}), encoding="utf-8")


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
    symbols = token_list.tradable_symbols()

    balances = read_onchain_balances(settings.agent_wallet_address)
    quotes = cmc_client.get_quotes([s for s in symbols if s != "WBNB"] + ["WBNB"])
    prices = _build_prices(symbols, quotes, balances)

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
    else:
        # Validated production strategy: fractional diversified hold + breaker.
        # Deploy into the concentrated top-liquidity basket (sized for capital).
        basket = token_list.basket_symbols(settings.basket_size)
        orders = adaptive_hold_strategy.decide(state, basket, settings.deploy_frac)
        if action.halt_buys:
            orders = [o for o in orders if o.token_in not in STABLECOINS]
        if action.needs_compliance_trade and not orders:
            orders = _compliance_orders(state)

    cum_return = pnl.cumulative_return(_baseline_equity(equity), equity)
    log.info("tick", equity=round(equity, 2), drawdown=round(drawdown.current_drawdown(), 4),
             cumulative_return=round(cum_return, 4),
             safeguard=action.reason, n_orders=len(orders), dry_run=dry_run)

    results = _execute(orders, prices, dry_run, trade_counter, now)

    trade_counter.save(TRADES_FILE)
    _save_drawdown(drawdown)
    _notify(action, results, equity, drawdown, cum_return, now)
    return {"equity": equity, "action": action.reason, "orders": len(orders), "results": results}


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
    if state.stable_value_usd >= COMPLIANCE_ORDER_USD:
        return [TradeOrder("USDT", "WBNB", COMPLIANCE_ORDER_USD, "min-trade compliance")]
    for sym, val in state.token_values_usd.items():
        if sym not in STABLECOINS and val >= COMPLIANCE_ORDER_USD:
            return [TradeOrder(sym, "USDT", COMPLIANCE_ORDER_USD, "min-trade compliance")]
    return []


def _make_executor(dry_run: bool):
    """Select the execution backend. Default PancakeSwap on the registered wallet
    (battle-tested); 'twak' routes through the Trust Wallet Agent Kit CLI, which
    drives its OWN local wallet (use only if that wallet is the registered one)."""
    if settings.execution_backend == "twak":
        from .execution.twak_executor import TwakExecutor
        return TwakExecutor(dry_run=dry_run)
    account = None
    if not dry_run:
        from eth_account import Account
        account = Account.from_key(settings.agent_private_key)
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
            results.append({"order": o.reason, "simulated": r.simulated, "tx": r.tx_hash})
        except Exception as e:  # noqa: BLE001 — one failed swap must not abort the tick
            log.warning("swap_failed", reason=o.reason, error=str(e))
            results.append({"order": o.reason, "error": str(e)})
    return results
