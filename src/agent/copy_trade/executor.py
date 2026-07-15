"""Turn a ParsedSwap alert into a real (or DRY_RUN-simulated) copy-buy or mirror-sell.
Buy: only when budget allows AND the shared safety gate (binance_web3
.passes_safety_check) passes. Sell: only mirrors a position this module itself
opened (matched by token_address + source_wallet) — never sells something it doesn't
have a record of buying, and never applies any stop/take-profit rule of its own
(spec §5 — exit strictly mirrors the source wallet)."""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import settings
from ..data.token_list import register_discovered
from ..execution.best_execution import rank_backends
from ..execution.binance_web3 import passes_safety_check
from ..monitor.logger import get_logger
from .budget import CopyTradeBudget
from .positions import CopyPosition, PositionStore
from .swap_parser import ParsedSwap

log = get_logger(__name__)


def handle_alert(
    alert: ParsedSwap,
    budget: CopyTradeBudget,
    store: PositionStore,
    executors: dict[str, object],
) -> None:
    if alert.direction == "buy":
        _handle_buy(alert, budget, store, executors)
    else:
        _handle_sell(alert, budget, store, executors)


def _handle_buy(
    alert: ParsedSwap, budget: CopyTradeBudget, store: PositionStore,
    executors: dict[str, object],
) -> None:
    if not budget.can_open_new():
        log.info("copy_trade_buy_skipped_budget", token=alert.token_symbol)
        return

    usd_size = budget.allocate()
    amount_wei = str(int(usd_size * 10**18))  # USDT has 18 decimals on BSC
    ok, decimals = passes_safety_check(settings.usdt_address, alert.token_address, amount_wei)
    if not ok:
        budget.release(usd_size)
        log.warning("copy_trade_buy_skipped_safety", token=alert.token_symbol)
        return

    register_discovered(alert.token_symbol, alert.token_address, decimals or alert.token_decimals)
    ranked = rank_backends(executors, "USDT", alert.token_symbol, usd_size)
    if not ranked:
        budget.release(usd_size)
        log.warning("copy_trade_buy_no_route", token=alert.token_symbol)
        return

    executor = executors[ranked[0]]
    resolved_decimals = decimals or alert.token_decimals
    try:
        result = executor.swap("USDT", alert.token_symbol, usd_size)
        # I1: store what OUR swap actually received, NOT alert.token_amount (the source
        # wallet's own, larger buy) — otherwise a mirror-sell can under-sell and strand
        # untracked residual tokens.
        token_amount = _received_amount(result, resolved_decimals, alert.token_amount)
        store.open_position(CopyPosition(
            token_symbol=alert.token_symbol,
            token_address=alert.token_address,
            token_decimals=resolved_decimals,
            source_wallet=alert.wallet,
            usd_size=usd_size,
            token_amount=token_amount,
            opened_at=datetime.now(timezone.utc).isoformat(),
        ))
    except Exception:
        # I2: a swap revert (or persist failure) after allocate() would otherwise leak
        # the slice forever. Return it to the pool before the error propagates to the
        # monitor's scan-loop guard.
        budget.release(usd_size)
        log.warning("copy_trade_buy_failed", token=alert.token_symbol)
        raise
    log.info("copy_trade_bought", token=alert.token_symbol, usd_size=usd_size,
              simulated=getattr(result, "simulated", None))


def _received_amount(result, decimals: int, dry_run_fallback: float) -> float:
    """Human-unit amount of the bought token OUR swap actually yielded, read from the
    SwapResult (`expected_out_wei`). In DRY_RUN (simulated) there is no real balance to
    be wrong about, so fall back to the source-wallet alert amount. (I1)"""
    out_wei = getattr(result, "expected_out_wei", 0) or 0
    if getattr(result, "simulated", False) or out_wei <= 0:
        return dry_run_fallback
    return out_wei / (10 ** decimals)


def _handle_sell(
    alert: ParsedSwap, budget: CopyTradeBudget, store: PositionStore,
    executors: dict[str, object],
) -> None:
    pos = store.find(alert.token_address, alert.wallet)
    if pos is None:
        log.debug("copy_trade_sell_no_matching_position", token=alert.token_symbol)
        return

    ranked = rank_backends(executors, alert.token_symbol, "USDT", pos.token_amount)
    if not ranked:
        log.warning("copy_trade_sell_no_route", token=alert.token_symbol)
        return

    executor = executors[ranked[0]]
    result = executor.swap(alert.token_symbol, "USDT", pos.token_amount)
    store.close_position(alert.token_address, alert.wallet)
    budget.release(pos.usd_size)
    log.info("copy_trade_sold", token=alert.token_symbol,
              simulated=getattr(result, "simulated", None))
