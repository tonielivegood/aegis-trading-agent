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


def breakout_pct(snap: MarketSnapshot) -> float:
    """The 5m breakout used for the entry gate. Prefer the same-source kline move
    (snap.breakout_pct, from Binance Alpha/spot — the SAME window/source as the volume
    spike), which removes the lag of the tick-sampled CMC price cache that bought thin
    memes late on a fading spike. Fall back to the cache when no kline move is supplied."""
    if snap.breakout_pct is not None:
        return snap.breakout_pct
    if snap.price_5m_ago <= 0:
        return 0.0
    return (snap.price_now - snap.price_5m_ago) / snap.price_5m_ago


_breakout_pct = breakout_pct   # back-compat alias


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


def hot_token_signals(items: list[dict], *, breakout_min: float, breakout_max: float,
                      allow: Callable[[str], bool] | None = None) -> list[BreakoutSignal]:
    """Convert Binance's server-side-filtered hot-token results (Option B discovery)
    into ranked BreakoutSignals — a PURE conversion, the caller already fetched
    `items` via `binance_web3.hot_token(...)`. Wash-trading/mint/freeze exclusion and
    the price-change FLOOR already happened server-side; this only applies the
    chase-cap ceiling (hot-token has no upper price-change filter) and ranks by
    the $ volume hot-token reports (there is no baseline-multiple concept here)."""
    out: list[BreakoutSignal] = []
    for it in items:
        contract = (it.get("tokenContractAddress") or "").strip().lower()
        if not contract or (allow and not allow(contract)):
            continue
        try:
            change = float(it.get("change")) / 100.0
            volume = float(it.get("volume"))
            price = float(it.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if change <= 0 or change < breakout_min or change > breakout_max:
            continue
        out.append(BreakoutSignal(
            symbol=it.get("tokenSymbol") or contract, contract=contract,
            vol_multiple=0.0, breakout_pct=change, recent_pump_pct=0.0, slippage_est=0.0,
            price_now=price, baseline_vol=volume,
            reasons=(f"hot-token +{change * 100:.1f}% vol=${volume:,.0f}",),
        ))
    out.sort(key=lambda s: s.baseline_vol, reverse=True)
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
    manage_classes: set[str] | frozenset[str] | None = None,
    safety_check: Callable[[BreakoutSignal], bool] | None = None,
) -> list[TradeOrder]:
    """Turn ranked breakout signals into entry orders, applying every risk gate.

    `position_usd` and `max_positions` are the REGIME-scaled deployment valve
    (RISK_OFF => position_usd=0 / max_positions=0 => no entries). Signals are
    assumed pre-ranked (strongest money-flow first); we fill the free slots in
    that order. Never pyramids, never re-enters a token in cooldown, never lets
    settlement cash drop below the floor. No chain/network access here — this
    function itself stays pure.

    `safety_check`, if given, is called ONLY after every cheap gate already passed
    (not held, not cooling, allowed, sized, floor ok) — i.e. only for a candidate
    that would otherwise take a slot right now. Lets the caller inject a live,
    just-in-time check (e.g. a fresh honeypot/tax quote) without this function
    itself touching the network, and without wasting the check on a candidate
    that would have been skipped anyway. Returning False skips the candidate
    WITHOUT consuming a slot — the next-ranked signal still gets a chance.
    """
    if state.drawdown_tripped or state.cap_breached:
        return []                                    # breaker: no fresh risk
    if position_usd < MIN_ORDER_USD:
        return []                                    # RISK_OFF or dust size
    allow = _default_allow if allow is None else allow

    # Count only positions of the classes THIS sleeve owns (when the barbell splits
    # majors→beta / memes→sniper), so the other sleeve's positions don't eat our slots.
    if manage_classes is None:
        held_count = len(book.positions)
    else:
        held_count = sum(1 for p in book.positions.values() if p.token_class in manage_classes)
    slots = max_positions - held_count
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
        if safety_check is not None and not safety_check(sig):
            continue                                 # just-in-time check failed (e.g. honeypot)
        orders.append(TradeOrder(
            settlement, sig.symbol, size,
            f"breakout vol {sig.vol_multiple:.1f}x +{sig.breakout_pct * 100:.1f}%"))
        stable_left -= size
        slots -= 1
    return orders
