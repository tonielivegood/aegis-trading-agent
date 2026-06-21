"""Beta core — the regime-gated momentum-major basket (Phase-2 of the barbell).

A cash-default breakout sniper cannot win a 7-day RAW-RETURN tournament: in a calm
week it sits in cash (~0% = mid-pack). The reliable return source is being LONG a
small basket of the STRONGEST-MOMENTUM majors while the regime is up (RISK_ON), held
with trailing/breakeven/hard stops — NOT breakout-scalped (which bled in the soak).

This module is the brain only: PURE and deterministic. The caller injects portfolio
state, executable prices, a momentum map (blended CMC 1h/24h change, in PERCENT), the
shared position book and the regime flag. No network/chain here; DRY_RUN is enforced
downstream. It trades MAJORS ONLY — the meme lottery sleeve (sniper) is separate and
owns meme-class positions; beta-core owns major-class positions.

Posture (trend-following):
  RISK_ON   → hold up to `max_names` strongest majors, fill empty slots with new leaders.
  CAUTIOUS  → HOLD existing (trail/breakeven still manage), open NOTHING new.
  RISK_OFF  → flatten the whole beta basket to cash.
Breaker (drawdown/cap) overrides everything → flatten to cash.
"""
from __future__ import annotations

from collections.abc import Callable

from ..data import token_list
from ..strategy.base_strategy import PortfolioState, TradeOrder
from . import regime as rg
from .positions import OpenPosition, PositionBook

STABLE = "USDT"
MIN_ORDER_USD = 2.0
MAJOR = "major"


def momentum_score(quote: dict, *, w_1h: float = 0.5, w_24h: float = 1.0) -> float:
    """Blended momentum (in PERCENT): favour sustained 24h trend, add recent 1h.
    A missing field counts as 0 (neutral), never raises."""
    c1 = float(quote.get("percent_change_1h") or 0.0)
    c24 = float(quote.get("percent_change_24h") or 0.0)
    return w_24h * c24 + w_1h * c1


def build_momentum(quotes: dict[str, dict], *, w_1h: float = 0.5,
                   w_24h: float = 1.0) -> dict[str, float]:
    """Map {symbol: CMC quote} → {symbol: blended momentum score} for majors only."""
    out: dict[str, float] = {}
    for sym, q in (quotes or {}).items():
        if token_list.token_class(sym) == MAJOR:
            out[sym] = momentum_score(q, w_1h=w_1h, w_24h=w_24h)
    return out


def _default_allow(symbol: str) -> bool:
    """A name may enter only if it is contest-eligible AND in our liquid tradable
    subset AND classed as a major (memes never enter the beta core)."""
    if token_list.token_class(symbol) != MAJOR:
        return False
    try:
        contract = token_list.get_token(symbol).contract
    except KeyError:
        return False
    c = (contract or "").lower()
    return bool(c) and token_list.is_eligible(c) and token_list.is_tradable_alpha(c)


def select_basket(momentum: dict[str, float], *, max_names: int, min_momentum: float,
                  allow: Callable[[str], bool] | None = None) -> list[str]:
    """The strongest `max_names` majors with blended momentum >= `min_momentum`,
    ranked strongest-first. Only positive, eligible, tradable majors are admitted."""
    allow = _default_allow if allow is None else allow
    ranked = sorted(
        ((s, m) for s, m in momentum.items() if m >= min_momentum and allow(s)),
        key=lambda kv: kv[1], reverse=True,
    )
    return [s for s, _ in ranked[:max_names]]


def _sell_full(symbol: str, held_usd: float, reason: str) -> TradeOrder:
    return TradeOrder(symbol, STABLE, held_usd, reason)


def _beta_held(book: PositionBook) -> list[str]:
    """Symbols in the shared book that the beta core owns (major-class only)."""
    return [s for s, p in book.positions.items() if p.token_class == MAJOR]


def decide_beta(
    state: PortfolioState, prices: dict[str, float], momentum: dict[str, float], *,
    book: PositionBook, regime_flag: rg.Regime | str, now: float,
    max_names: int, position_usd: float, floor_usd: float,
    min_momentum: float = 2.0, trail_pct: float = 0.12, hard_stop_pct: float = 0.10,
    breakeven_trigger: float = 0.05, breakeven_buffer: float = 0.005,
    exit_rank_mult: int = 2, settlement: str = STABLE,
    cooldown_symbols: frozenset[str] | set[str] = frozenset(),
    block_entries: bool = False,
    allow: Callable[[str], bool] | None = None,
) -> tuple[list[TradeOrder], str]:
    """One beta-core decision: exits first (free slots + cut/rotate), then RISK_ON
    entries into the strongest leaders. Returns (orders, mode) and mutates `book`.
    Pure — no chain/network; never pyramids; respects the settlement-cash floor."""
    flag = rg.Regime(regime_flag)
    allow = _default_allow if allow is None else allow
    breaker = state.drawdown_tripped or state.cap_breached
    orders: list[TradeOrder] = []

    held = _beta_held(book)

    # Breaker or RISK_OFF → flatten the whole beta basket to cash.
    if breaker or flag == rg.Regime.RISK_OFF:
        why = "beta exit: breaker" if breaker else "beta exit: risk_off"
        for sym in held:
            held_usd = state.token_values_usd.get(sym, book.positions[sym].usd_size)
            orders.append(_sell_full(sym, held_usd, why))
            book.close(sym)
        return orders, "beta-flat"

    # Keep a wider "still a leader" set so a name that merely slipped a rank isn't churned.
    leaders = set(select_basket(momentum, max_names=max_names * max(1, exit_rank_mult),
                                min_momentum=min_momentum, allow=allow))

    # --- exits on held beta names (trailing / breakeven / hard stop / momentum lost) ---
    exited_now: set[str] = set()
    for sym in held:
        p = book.positions[sym]
        price = prices.get(sym, 0.0)
        if price <= 0:
            continue
        book.update_peak(sym, price)
        p = book.positions[sym]
        held_usd = state.token_values_usd.get(sym, p.usd_size)
        gain = p.gain(price)
        peak_gain = (p.peak_price - p.entry_price) / p.entry_price if p.entry_price > 0 else 0.0

        if gain <= -hard_stop_pct:
            orders.append(_sell_full(sym, held_usd, f"beta exit: hard stop {gain*100:.1f}%"))
            book.close(sym)
        elif (breakeven_trigger > 0 and peak_gain >= breakeven_trigger
              and price <= p.entry_price * (1 + breakeven_buffer)):
            orders.append(_sell_full(sym, held_usd, f"beta exit: breakeven ({peak_gain*100:.1f}% peak)"))
            book.close(sym)
        elif price > p.entry_price and p.peak_price > 0 and price <= p.peak_price * (1 - trail_pct):
            orders.append(_sell_full(sym, held_usd, "beta exit: trailing stop"))
            book.close(sym)
        elif sym not in leaders:
            orders.append(_sell_full(sym, held_usd, "beta exit: momentum lost"))
            book.close(sym)
        if not book.is_open(sym):
            exited_now.add(sym)        # never re-enter a name we exited this tick

    # --- entries: RISK_ON only; fill empty slots with the strongest non-held leaders ---
    # `block_entries` (daily soft breaker) suppresses NEW entries WITHOUT flattening the
    # basket — existing holds keep riding their trailing/breakeven stops.
    if flag == rg.Regime.RISK_ON and position_usd >= MIN_ORDER_USD and not block_entries:
        basket = select_basket(momentum, max_names=max_names, min_momentum=min_momentum, allow=allow)
        slots = max_names - len(_beta_held(book))
        stable_left = state.stable_value_usd
        for sym in basket:
            if slots <= 0:
                break
            if book.is_open(sym) or sym in exited_now or sym in cooldown_symbols:
                continue
            if sym not in prices or prices[sym] <= 0:
                continue
            if stable_left - position_usd < floor_usd:
                continue                                  # would breach the settlement floor
            try:
                contract = token_list.get_token(sym).contract
            except KeyError:
                contract = ""
            orders.append(TradeOrder(settlement, sym, position_usd,
                                     f"beta entry: momentum {momentum.get(sym, 0.0):+.1f}%"))
            book.open(OpenPosition(symbol=sym, contract=contract, entry_price=prices[sym],
                                   usd_size=position_usd, token_class=MAJOR))
            stable_left -= position_usd
            slots -= 1

    return orders, "beta"
