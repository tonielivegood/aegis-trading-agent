"""Volume-breakout signal generator — the PRIMARY entry trigger for the v2 sniper.

Meme/Alpha tokens move on money-flow, not news. This scans the hunting universe's
MarketSnapshots and emits a RANKED list of breakout signals: a real 5m/1m volume
spike (>= vol_mult x baseline) while price is RISING but not already pumped past a
cap (so we catch the START of a move, not the blow-off top).

Pure and deterministic — snapshots are supplied by the caller (MarketFeed); nothing
here touches the network. Eligibility, cooldown, regime-sizing and slot/floor gates
are applied downstream at the entry stage (decide_breakout_entries), keeping this a
fully unit-testable pure function. Fails safe: a zero/absent baseline (no real
volume data) never produces a signal.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..data import token_list
from ..strategy.base_strategy import PortfolioState, TradeOrder
from .positions import PositionBook
from .volume_anomaly_detector import MarketSnapshot

DEFAULT_VOL_MULT = 3.0        # 5m volume >= 3x baseline = money rushing in
DEFAULT_BREAKOUT_MAX = 0.10   # price rising but <= +10% — don't chase a blow-off
DEFAULT_OVERPUMP = 0.10       # skip if already pumped this much over the recent window

MIN_ORDER_USD = 2.0           # below this a swap is dust (fees dominate)
STABLE = "USDT"
# CMC Agent Hub community-trending boost: a breakout that is ALSO trending by community
# activity gets a strength multiplier so it wins the scarce slots over an equally-strong
# but unnoticed token. Re-ranking only — it never admits a signal that failed a gate.
TRENDING_BOOST = 1.5


@dataclass(frozen=True)
class BreakoutSignal:
    symbol: str
    contract: str
    vol_multiple: float       # vol_5m / baseline_vol
    breakout_pct: float       # (price_now - price_5m_ago) / price_5m_ago
    recent_pump_pct: float
    slippage_est: float
    price_now: float
    baseline_vol: float
    reasons: tuple[str, ...] = ()
    trending: bool = False    # token is in CMC's community-trending set this hour

    @property
    def strength(self) -> float:
        """Ranking key — stronger money-flow wins the scarce slots; a CMC-trending
        token is boosted so community attention breaks ties toward it."""
        return self.vol_multiple * (TRENDING_BOOST if self.trending else 1.0)


def _breakout_pct(snap: MarketSnapshot) -> float:
    if snap.price_5m_ago <= 0:
        return 0.0
    return (snap.price_now - snap.price_5m_ago) / snap.price_5m_ago


def scan_breakouts(snapshots: dict[str, MarketSnapshot], *,
                   vol_mult: float = DEFAULT_VOL_MULT,
                   breakout_min: float = 0.0,
                   breakout_max: float = DEFAULT_BREAKOUT_MAX,
                   overpump_pct: float = DEFAULT_OVERPUMP,
                   trending_symbols: frozenset[str] | set[str] = frozenset(),
                   ) -> list[BreakoutSignal]:
    """Emit breakout signals (ranked by volume multiple, strongest first).

    `breakout_min` lets a class require a minimum upward move (majors use +0.3% so a
    flat tick doesn't trigger); memes leave it at 0 (any rise within the cap).
    `trending_symbols` (CMC Agent Hub community-trending set) boosts the rank of any
    matching signal — empty set ⇒ pure money-flow ranking, identical to before.
    """
    out: list[BreakoutSignal] = []
    for sym, snap in snapshots.items():
        # Liquidity is disqualifying — thin pool / no route / high slippage.
        if not snap.has_route or not snap.liquidity_ok:
            continue
        # Real volume spike only; baseline <= 0 means no real data → never fire.
        if snap.baseline_vol <= 0 or snap.vol_5m < vol_mult * snap.baseline_vol:
            continue
        breakout = _breakout_pct(snap)
        # Price must be RISING past the class floor, but not past the chase cap.
        if breakout <= 0 or breakout < breakout_min or breakout > breakout_max:
            continue
        # Already blown off the move over the recent window → reversal risk.
        if snap.recent_pump_pct >= overpump_pct:
            continue
        vm = snap.vol_5m / snap.baseline_vol
        is_trending = sym.upper() in trending_symbols
        reasons = [f"vol {vm:.1f}x baseline", f"breakout +{breakout * 100:.1f}%"]
        if is_trending:
            reasons.append("CMC-trending")
        out.append(BreakoutSignal(
            symbol=sym, contract=(snap.contract or "").lower(), vol_multiple=vm,
            breakout_pct=breakout, recent_pump_pct=snap.recent_pump_pct,
            slippage_est=snap.slippage_est, price_now=snap.price_now,
            baseline_vol=snap.baseline_vol, trending=is_trending,
            reasons=tuple(reasons),
        ))
    out.sort(key=lambda s: s.strength, reverse=True)
    return out


def _default_allow(contract: str) -> bool:
    """A signal may only enter if it is BOTH contest-eligible (in the 149) AND in
    our liquid, tradable Alpha subset — checked by contract address."""
    c = (contract or "").lower()
    return bool(c) and token_list.is_eligible(c) and token_list.is_tradable_alpha(c)


def decide_breakout_entries(
    signals: list[BreakoutSignal], state: PortfolioState, book: PositionBook, *,
    position_usd: float, max_positions: int, floor_usd: float,
    cooldown_symbols: frozenset[str] | set[str] = frozenset(),
    settlement: str = STABLE,
    allow: Callable[[str], bool] | None = None,
    meme_usd: float | None = None,
) -> list[TradeOrder]:
    """Turn ranked breakout signals into entry orders, applying every risk gate.

    `position_usd` and `max_positions` are the REGIME-scaled deployment valve
    (RISK_OFF => position_usd=0 / max_positions=0 => no entries). Signals are
    assumed pre-ranked (strongest money-flow first); we fill the free slots in
    that order. Never pyramids, never re-enters a token in cooldown, never lets
    settlement cash drop below the floor. No chain/network access here.
    """
    if state.drawdown_tripped or state.cap_breached:
        return []                                    # breaker: no fresh risk
    if position_usd < MIN_ORDER_USD:
        return []                                    # RISK_OFF or dust size
    allow = _default_allow if allow is None else allow

    slots = max_positions - len(book.positions)
    if slots <= 0:
        return []

    orders: list[TradeOrder] = []
    stable_left = state.stable_value_usd
    for sig in signals:
        if slots <= 0:
            break
        if book.is_open(sig.symbol):
            continue                                 # no pyramiding
        if sig.symbol in cooldown_symbols:
            continue                                 # cooling down after a recent exit
        if not allow(sig.contract):
            continue                                 # must be eligible + tradable
        # Thin memes enter as a small fixed "lottery" position; majors at regime size.
        size = meme_usd if (meme_usd and token_list.token_class(sig.symbol) == "meme") else position_usd
        if size < MIN_ORDER_USD:
            continue                                 # dust
        if stable_left - size < floor_usd:
            continue                                 # would breach the settlement floor
        orders.append(TradeOrder(
            settlement, sig.symbol, size,
            f"breakout vol {sig.vol_multiple:.1f}x +{sig.breakout_pct * 100:.1f}%"))
        stable_left -= size
        slots -= 1
    return orders
