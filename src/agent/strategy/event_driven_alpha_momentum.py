"""Layer B — Event-Driven Alpha Momentum with risk-gated execution.

Aegis acts as an AI radar over the official 149 eligible-token universe:
  1. detect a public catalyst       (aegis.event_signal_scanner -> event_score)
  2. confirm with 5-min market data  (aegis.volume_anomaly_detector -> confirmation)
  3. combine                          combined = event + confirmation - risk_penalty
  4. enter TINY only if every gate passes (allowlist by address, liquid subset,
     score >= threshold, price-confirmed, not over-pumped, slippage ok, breaker
     clear, caps + stablecoin floor allow, a free slot, no pyramiding)
  5. exit fast — every position has a clear plan at entry:
        breaker (override) > hard take-profit (2x) > hard stop-loss > max-hold
        (5h) > volume-exhaustion (5x entry baseline, after min hold) >
        FOMO/momentum reversal > trailing stop.

Meme/Alpha tokens are temporary instruments to earn the settlement stablecoin —
never long-term holdings. Pure decision logic (no chain/network): callers supply
prices + market snapshots. DRY_RUN still hard-gates any real broadcast downstream.
When no high-confidence catalyst exists and nothing is open, control falls back
to Layer A (eligible basket).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..aegis.catalyst_score import CatalystSignal
from ..aegis.event_signal_scanner import EventScore
from ..aegis import token_class as tc
from ..aegis.positions import OpenPosition, PositionBook
from ..aegis.volume_anomaly_detector import MarketConfirmation, MarketSnapshot
from ..config import settings
from ..data import token_list
from . import eligible_basket_strategy, rebalance_strategy
from .base_strategy import PortfolioState, TradeOrder

MIN_ORDER_USD = 2.0
STABLE = "USDT"


def _stable_floor(state: PortfolioState) -> float:
    """Settlement-cash floor in USD = max(absolute floor, pct of equity)."""
    return max(settings.stablecoin_floor_usd, state.equity_usd * settings.stablecoin_floor_pct)


@dataclass(frozen=True)
class Candidate:
    symbol: str
    contract: str
    event_score: float
    confirmation_score: float
    risk_penalty: float
    breakout_pct: float = 0.0
    recent_pump_pct: float = 0.0
    baseline_vol: float = 0.0
    eligible: bool = False
    tradable: bool = False
    source_tier: int = 1            # 1 authority · 2 project · 3 unverified
    is_official: bool = True        # from a Tier-1/2 (official) source
    volume_confirmed: bool = True   # real 5m volume spike present
    reasons: tuple[str, ...] = ()

    @property
    def combined_score(self) -> float:
        return self.event_score + self.confirmation_score - self.risk_penalty


def _volume_confirmed(snap: MarketSnapshot) -> bool:
    """A real (non-faked) 5m volume spike: needs a positive baseline and a spike."""
    return snap.baseline_vol > 0 and snap.vol_5m >= settings.aegis_vol_spike_mult * snap.baseline_vol


def make_candidate(escore: EventScore, conf: MarketConfirmation, snap: MarketSnapshot, *,
                   source_tier: int = 1, is_official: bool = True) -> Candidate:
    """Fuse a catalyst score with its market confirmation into a gated candidate."""
    contract = (escore.contract or snap.contract or "").lower()
    eligible = bool(contract) and token_list.is_eligible(contract)
    tradable = bool(contract) and token_list.is_tradable_alpha(contract)
    return Candidate(
        symbol=escore.token or snap.symbol, contract=contract, event_score=escore.score,
        confirmation_score=conf.confirmation_score, risk_penalty=conf.risk_penalty,
        breakout_pct=conf.breakout_pct, recent_pump_pct=snap.recent_pump_pct,
        baseline_vol=snap.vol_5m, eligible=eligible, tradable=tradable,
        source_tier=source_tier, is_official=is_official,
        volume_confirmed=_volume_confirmed(snap),
        reasons=tuple(escore.reasons) + tuple(conf.reasons),
    )


def make_candidate_from_signal(sig: CatalystSignal, conf: MarketConfirmation,
                               snap: MarketSnapshot) -> Candidate:
    """Build a candidate from a CatalystScanner signal + market confirmation."""
    contract = (sig.contract or snap.contract or "").lower()
    eligible = bool(contract) and token_list.is_eligible(contract)
    tradable = bool(contract) and token_list.is_tradable_alpha(contract)
    return Candidate(
        symbol=sig.symbol or snap.symbol, contract=contract, event_score=sig.score,
        confirmation_score=conf.confirmation_score, risk_penalty=conf.risk_penalty,
        breakout_pct=conf.breakout_pct, recent_pump_pct=snap.recent_pump_pct,
        baseline_vol=snap.vol_5m, eligible=eligible, tradable=tradable,
        source_tier=sig.source_tier, is_official=sig.is_official,
        volume_confirmed=_volume_confirmed(snap),
        reasons=tuple(sig.reasons) + tuple(conf.reasons),
    )


# ----------------------------- entries -----------------------------

def decide_entries(candidates: list[Candidate], state: PortfolioState, book: PositionBook, *,
                   threshold: float | None = None, order_usd: float | None = None,
                   max_position_usd: float | None = None, max_positions: int | None = None,
                   breakout_min: float | None = None, overpump_pct: float | None = None,
                   floor_usd: float | None = None, floor_pct: float | None = None,
                   require_volume: bool | None = None, fast_confirm_tier1: bool | None = None) -> list[TradeOrder]:
    if state.drawdown_tripped or state.cap_breached:
        return []                                   # breaker: no fresh risk

    threshold = settings.event_signal_threshold if threshold is None else threshold
    order_usd = settings.default_order_usd if order_usd is None else order_usd
    max_position_usd = settings.max_position_usd if max_position_usd is None else max_position_usd
    max_positions = settings.max_open_positions if max_positions is None else max_positions
    breakout_min = settings.aegis_breakout_pct if breakout_min is None else breakout_min
    overpump_pct = settings.aegis_overpump_pct if overpump_pct is None else overpump_pct
    require_volume = settings.aegis_require_volume_confirmation if require_volume is None else require_volume
    fast_confirm_tier1 = settings.aegis_fast_confirm_tier1 if fast_confirm_tier1 is None else fast_confirm_tier1
    floor = max(
        settings.stablecoin_floor_usd if floor_usd is None else floor_usd,
        state.equity_usd * (settings.stablecoin_floor_pct if floor_pct is None else floor_pct),
    )

    slots = max_positions - len(book.positions)
    if slots <= 0:
        return []

    size = min(order_usd, max_position_usd)
    orders: list[TradeOrder] = []
    stable_left = state.stable_value_usd
    for c in sorted(candidates, key=lambda x: x.combined_score, reverse=True):
        if slots <= 0:
            break
        if book.is_open(c.symbol):
            continue                                # no pyramiding into the same token
        if not (c.eligible and c.tradable):
            continue                                # must be allowlisted + liquid
        if c.source_tier >= 3 and not c.is_official:
            continue                                # Tier-3 alone is never enough to enter
        if c.combined_score < threshold:
            continue
        if c.breakout_pct < breakout_min:
            continue                                # price not confirmed
        if c.recent_pump_pct >= overpump_pct:
            continue                                # already pumped — reversal risk
        # Volume confirmation required, except a Tier-1 authority catalyst may take
        # the faster price+liquidity path (pending consecutive 1m confirmation).
        fast = fast_confirm_tier1 and c.source_tier == 1
        if require_volume and not c.volume_confirmed and not fast:
            continue                                # stays WATCHLIST/ARMED until volume confirms
        if size < MIN_ORDER_USD:
            continue
        if stable_left - size < floor:
            continue                                # would breach the stablecoin floor
        orders.append(TradeOrder(STABLE, c.symbol, size, f"aegis entry score={c.combined_score:.0f}"))
        stable_left -= size
        slots -= 1
    return orders


# ----------------------------- exits -----------------------------

def _sell_full(symbol: str, held_usd: float, reason: str) -> TradeOrder:
    return TradeOrder(symbol, STABLE, held_usd, reason)


def decide_exits(book: PositionBook, prices: dict[str, float],
                 snapshots: dict[str, MarketSnapshot], state: PortfolioState, *,
                 now: float | None = None, hard_tp_mult: float | None = None,
                 hard_stop_pct: float | None = None, max_hold_min: int | None = None,
                 min_hold_vol_min: int | None = None, vol_exit_mult: float | None = None,
                 trailing_pct: float | None = None, fomo_trailing_pct: float | None = None,
                 no_progress_min: int | None = None, no_progress_gain: float | None = None,
                 volume_death_mult: float | None = None,
                 volume_death_in_profit: bool | None = None,
                 breakeven_trigger: float | None = None, breakeven_buffer: float | None = None,
                 class_aware: bool = False) -> list[TradeOrder]:
    hard_tp_mult = settings.hard_take_profit_multiple if hard_tp_mult is None else hard_tp_mult
    hard_stop_pct = settings.aegis_hard_stop_pct if hard_stop_pct is None else hard_stop_pct
    max_hold_min = settings.max_hold_minutes if max_hold_min is None else max_hold_min
    min_hold_vol_min = settings.min_hold_minutes_for_volume_exit if min_hold_vol_min is None else min_hold_vol_min
    vol_exit_mult = settings.volume_exit_multiple if vol_exit_mult is None else vol_exit_mult
    trailing_pct = settings.aegis_trailing_stop_pct if trailing_pct is None else trailing_pct
    fomo_trailing_pct = settings.aegis_fomo_trailing_pct if fomo_trailing_pct is None else fomo_trailing_pct
    no_progress_min = settings.aegis_no_progress_minutes if no_progress_min is None else no_progress_min
    no_progress_gain = settings.aegis_no_progress_min_gain if no_progress_gain is None else no_progress_gain
    volume_death_mult = settings.aegis_volume_death_mult if volume_death_mult is None else volume_death_mult
    if volume_death_in_profit is None:
        volume_death_in_profit = settings.aegis_volume_death_in_profit
    breakeven_trigger = settings.aegis_breakeven_trigger_pct if breakeven_trigger is None else breakeven_trigger
    breakeven_buffer = settings.aegis_breakeven_buffer_pct if breakeven_buffer is None else breakeven_buffer

    breaker = state.drawdown_tripped or state.cap_breached
    orders: list[TradeOrder] = []

    for symbol in list(book.positions):
        p = book.positions[symbol]
        held_usd = state.token_values_usd.get(symbol, p.usd_size)
        price = prices.get(symbol, 0.0)

        # 0) Global breaker overrides everything.
        if breaker:
            orders.append(_sell_full(symbol, held_usd, "aegis exit: breaker"))
            book.close(symbol)
            continue
        if price <= 0:
            continue

        book.update_peak(symbol, price)
        p = book.positions[symbol]
        gain = p.gain(price)
        age_min = p.age_s(now) / 60.0

        # Per-position exit knobs: in class-aware mode (the live sniper) a MAJOR
        # scalps (+4%, tight trail, −3.5%) while a MEME rides (+200%, wide trail,
        # −8%). Otherwise use the globally-resolved params (back-compat / tests).
        if class_aware:
            cp = tc.params(p.token_class)
            tp_m, stop_p, trail_base, nopro = (cp.hard_tp_mult, cp.hard_stop_pct,
                                               cp.trailing_pct, cp.no_progress_min)
        else:
            tp_m, stop_p, trail_base, nopro = (hard_tp_mult, hard_stop_pct,
                                               trailing_pct, no_progress_min)

        # 1) Hard take-profit (value reached Nx).
        if gain >= (tp_m - 1.0):
            orders.append(_sell_full(symbol, held_usd, f"aegis exit: hard TP {tp_m:g}x"))
            book.close(symbol)
            continue
        # 2) Hard stop-loss.
        if gain <= -stop_p:
            orders.append(_sell_full(symbol, held_usd, f"aegis exit: hard stop {gain*100:.1f}%"))
            book.close(symbol)
            continue
        # 2b) Breakeven stop: once a trade has RUN to +breakeven_trigger, never let it
        #     round-trip into a loss. If it falls back to ~entry (+buffer for fees), bank
        #     it flat instead of waiting for the −stop. This closes the real gap: the
        #     trailing stop below is gated on price>entry, so a "+5% pop then fade through
        #     entry" was caught only by the −8% hard stop. Buffer locks a hair above net 0.
        peak_gain = (p.peak_price - p.entry_price) / p.entry_price if p.entry_price > 0 else 0.0
        if (breakeven_trigger > 0 and peak_gain >= breakeven_trigger
                and price <= p.entry_price * (1 + breakeven_buffer)):
            orders.append(_sell_full(symbol, held_usd,
                                     f"aegis exit: breakeven ({peak_gain*100:.1f}% peak gave back)"))
            book.close(symbol)
            continue
        # 3) Max hold time.
        if age_min >= max_hold_min:
            orders.append(_sell_full(symbol, held_usd, f"aegis exit: max hold {max_hold_min}m"))
            book.close(symbol)
            continue
        # 3b) No-progress stop: we entered on a breakout that never materialised.
        #     Past the window with the move still not showing (gain below the bar) =>
        #     a dead trade; cut it near breakeven rather than wait for the hard stop.
        if nopro > 0 and age_min >= nopro and gain < no_progress_gain:
            orders.append(_sell_full(symbol, held_usd,
                                     f"aegis exit: no progress ({gain * 100:.1f}% after {nopro}m)"))
            book.close(symbol)
            continue
        # 4) Volume x5 FOMO DEFENSE — NOT a blind sell. A 5x blow-off vs the entry
        #    baseline (after the min-hold guard) tightens the trailing stop, and
        #    forces an exit only if price is ALSO stalling/reversing (printing below
        #    the 5m-ago level). Only fires on REAL volume (entry_baseline_vol > 0).
        snap = snapshots.get(symbol)
        fomo_active = bool(snap and age_min >= min_hold_vol_min and p.entry_baseline_vol > 0
                           and snap.vol_5m >= vol_exit_mult * p.entry_baseline_vol)
        momentum_weak = bool(snap and snap.price_5m_ago > 0 and price < snap.price_5m_ago)
        if fomo_active and momentum_weak:
            orders.append(_sell_full(symbol, held_usd,
                                     f"aegis exit: volume {vol_exit_mult:g}x FOMO defense (price stalled)"))
            book.close(symbol)
            continue
        # 4b) Volume death while in profit: the money-flow that drove the breakout
        #     has dried up (current 5m volume back below its own baseline). Bank the
        #     gain instead of round-tripping. Needs real volume + the min-hold guard.
        if (volume_death_in_profit and gain > 0 and snap and age_min >= min_hold_vol_min
                and snap.baseline_vol > 0 and 0 < snap.vol_5m < volume_death_mult * snap.baseline_vol):
            orders.append(_sell_full(symbol, held_usd, "aegis exit: volume died (inflow gone), in profit"))
            book.close(symbol)
            continue
        # 5) Trailing stop once profitable — TIGHTENED while FOMO defense is active.
        eff_trail = fomo_trailing_pct if fomo_active else trail_base
        if price > p.entry_price and p.peak_price > 0 and price <= p.peak_price * (1 - eff_trail):
            reason = "aegis exit: FOMO trailing stop" if fomo_active else "aegis exit: trailing stop"
            orders.append(_sell_full(symbol, held_usd, reason))
            book.close(symbol)
            continue
    return orders


# ----------------------------- orchestration (two-layer) -----------------------------

def decide(*, candidates: list[Candidate], state: PortfolioState, book: PositionBook,
           prices: dict[str, float], snapshots: dict[str, MarketSnapshot],
           basket_symbols: list[str], now: float | None = None,
           threshold: float | None = None) -> tuple[list[TradeOrder], str]:
    """Two-layer decision. Layer B (event radar) drives whenever a high-confidence
    catalyst exists or positions are open; otherwise Layer A (eligible basket)."""
    threshold = settings.event_signal_threshold if threshold is None else threshold

    if state.drawdown_tripped or state.cap_breached:
        for s in list(book.positions):
            book.close(s)
        return rebalance_strategy.derisk_orders(state), "breaker-derisk"

    exits = decide_exits(book, prices, snapshots, state, now=now)
    entries = decide_entries(candidates, state, book, threshold=threshold)
    high_conf = any(c.combined_score >= threshold and c.eligible and c.tradable for c in candidates)

    if exits or entries or book.positions or high_conf:
        for o in entries:
            sym = o.token_out
            if o.token_in == STABLE and sym in prices:
                snap = snapshots.get(sym)
                book.open(OpenPosition(
                    symbol=sym, contract=_contract_of(candidates, sym),
                    entry_price=prices[sym], usd_size=o.amount_in_usd,
                    entry_baseline_vol=(snap.vol_5m if snap else 0.0),
                ))
        return exits + entries, "aegis-event"

    return eligible_basket_strategy.decide(state, basket_symbols), "baseline-basket"


def _contract_of(candidates: list[Candidate], symbol: str) -> str:
    for c in candidates:
        if c.symbol == symbol:
            return c.contract
    return ""
