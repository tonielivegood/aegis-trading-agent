"""v2 sniper orchestrator — the live decision for the cash-default meme sniper.

One tick: build market snapshots for the hunting universe (+ open positions),
run the exit rails first (freeing slots, recording each exit into the cooldown
book), then — only if the REGIME valve allows new risk — scan for volume
breakouts and open regime-sized entries into the free slots.

Feed, regime flag and cooldown book are injected, so the whole flow is unit-
testable with no chain/network. Never signs or broadcasts; DRY_RUN is enforced
downstream. Price decisions use the on-chain price supplied in `prices`; volume
comes from the feed's snapshot (Binance Alpha klines).
"""
from __future__ import annotations

from collections.abc import Callable

from ..config import settings
from ..data import token_list
from ..strategy import event_driven_alpha_momentum as edam
from ..strategy.base_strategy import PortfolioState, TradeOrder
from . import regime as rg
from . import token_class as tc
from .cooldown import CooldownBook
from .positions import OpenPosition, PositionBook
from .volume_breakout import BreakoutSignal, decide_breakout_entries, hot_token_signals, scan_breakouts

STABLE = "USDT"


def meme_ticket_usd(equity_usd: float) -> float:
    """Meme ticket size: a % of equity, capped so a thin Alpha pool can still absorb it."""
    return min(equity_usd * settings.meme_order_pct, settings.meme_order_cap_usd)


def _scan_by_class(snapshots, overpump_pct: float, vol_factor: float = 1.0,
                   trending: frozenset[str] | set[str] = frozenset(),
                   manage_classes: set[str] | frozenset[str] | None = None) -> list[BreakoutSignal]:
    """Scan each token with its CLASS entry params (majors enter looser, memes need
    a real 3x breakout), then merge and rank by money-flow strength.

    `vol_factor` is the regime BETA-CAPTURE valve: in RISK_ON it is < 1, lowering the
    volume bar so the agent deploys into mild deep-major momentum and rides a rising
    market instead of sitting in cash waiting for a rare sharp breakout.
    `trending` is the cached CMC Agent Hub community-trending set; it re-ranks
    qualified signals toward tokens with real community attention (never admits new ones)."""
    by_class: dict[str, dict] = {}
    for sym, snap in snapshots.items():
        by_class.setdefault(token_list.token_class(sym), {})[sym] = snap
    sigs: list[BreakoutSignal] = []
    for cls, snaps in by_class.items():
        if manage_classes is not None and cls not in manage_classes:
            continue                         # another sleeve owns this class
        cp = tc.params(cls)
        # Beta-capture loosening applies to the CHEAP major tier only; memes stay
        # strict (rare, big-ride) in every regime — they're too expensive to churn.
        bar = cp.vol_mult * (vol_factor if cls == tc.MAJOR else 1.0)
        sigs += scan_breakouts(snaps, vol_mult=bar, breakout_min=cp.breakout_min,
                               breakout_max=cp.breakout_max, overpump_pct=overpump_pct,
                               trending_symbols=trending)
    sigs.sort(key=lambda s: s.strength, reverse=True)
    return sigs


def run(state: PortfolioState, prices: dict[str, float], *, book: PositionBook,
        feed, cooldowns: CooldownBook, regime_flag: rg.Regime | str,
        universe: list[str], now: float, floor_usd: float | None = None,
        settlement: str = STABLE, overpump_pct: float | None = None,
        cooldown_s: float | None = None,
        allow: Callable[[str], bool] | None = None,
        trending: frozenset[str] | set[str] = frozenset(),
        manage_classes: set[str] | frozenset[str] | None = None,
        max_meme_positions: int | None = None,
        meme_usd: float | None = None,
        hot_token_items: list[dict] | None = None,
        safety_check: Callable[[BreakoutSignal], bool] | None = None,
        ) -> tuple[list[TradeOrder], str]:
    overpump_pct = settings.aegis_overpump_pct if overpump_pct is None else overpump_pct
    cooldown_s = settings.aegis_cooldown_seconds if cooldown_s is None else cooldown_s
    meme_usd = meme_ticket_usd(state.equity_usd) if meme_usd is None else meme_usd
    if floor_usd is None:
        floor_usd = max(settings.stablecoin_floor_usd,
                        state.equity_usd * settings.stablecoin_floor_pct)

    # Breaker overrides everything: flatten, record cooldowns, sit in cash.
    if state.drawdown_tripped or state.cap_breached:
        exits = edam.decide_exits(book, prices, {}, state, now=now, manage_classes=manage_classes)
        for o in exits:
            cooldowns.record_exit(o.token_in, now)
        return exits, "sniper-breaker"

    need = set(universe) | set(book.positions)
    snapshots = feed.build_snapshots(sorted(need), prices)

    # Exits first — frees slots within the tick and records each into cooldown.
    # class_aware: majors scalp (+4%/−3.5%), memes ride (+200%/−8%).
    exits = edam.decide_exits(book, prices, snapshots, state, now=now,
                              manage_classes=manage_classes, class_aware=True)
    for o in exits:
        cooldowns.record_exit(o.token_in, now)

    # Regime valve: RISK_OFF (allow_new False) → no fresh entries this tick.
    params = rg.params(regime_flag)
    entries: list[TradeOrder] = []
    if params.allow_new:
        sigs: list[BreakoutSignal] = []
        if hot_token_items is not None:
            # Option B discovery: memes come from Binance's server-side-filtered
            # hot-token feed, not the client-side scan of a self-maintained universe
            # file. Whatever class(es) hot_token_items doesn't cover (majors, when
            # this sleeve still owns them) still scan snapshots as before.
            if manage_classes is None or tc.MEME in manage_classes:
                mp = tc.params(tc.MEME)
                sigs += hot_token_signals(hot_token_items, breakout_min=mp.breakout_min,
                                          breakout_max=mp.breakout_max)
            scan_classes = {tc.MAJOR} if manage_classes is None else (manage_classes - {tc.MEME})
        else:
            scan_classes = manage_classes
        sigs += _scan_by_class(snapshots, overpump_pct, vol_factor=params.entry_vol_factor,
                               trending=trending, manage_classes=scan_classes)
        sigs.sort(key=lambda s: s.strength, reverse=True)
        cooling = cooldowns.cooling_down(now=now, cooldown_s=cooldown_s)
        pos_usd = rg.position_usd(state.equity_usd, regime_flag)
        # The caller's cap is AUTHORITATIVE when given: the barbell caps entries BELOW the
        # regime's own slots (shared global cap), while the tournament-clock may raise the
        # lottery sleeve ABOVE the regime cap (the convex late-game push). None => regime default.
        max_pos = params.max_slots if max_meme_positions is None else max_meme_positions
        entries = decide_breakout_entries(
            sigs, state, book, position_usd=pos_usd, max_positions=max_pos,
            floor_usd=floor_usd, cooldown_symbols=cooling, settlement=settlement, allow=allow,
            meme_usd=meme_usd, manage_classes=manage_classes, safety_check=safety_check)
        for o in entries:
            sym = o.token_out
            if o.token_in == settlement and sym in prices:
                snap = snapshots.get(sym)
                try:
                    contract = token_list.get_token(sym).contract
                except KeyError:
                    contract = ""
                book.open(OpenPosition(
                    symbol=sym, contract=contract, entry_price=prices[sym],
                    usd_size=o.amount_in_usd, token_class=token_list.token_class(sym),
                    entry_baseline_vol=(snap.vol_5m if snap else 0.0)))
    return exits + entries, "sniper"
