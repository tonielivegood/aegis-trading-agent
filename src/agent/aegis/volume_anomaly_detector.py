"""5-minute market confirmation for a catalyst.

A public catalyst is only actionable if the market confirms it: a fresh volume
spike, a price breakout, and tradable liquidity — but NOT already-blown-off
("already pumped") and NOT a thin/honeypot pool. This module turns a
`MarketSnapshot` into a positive `confirmation_score` and a separate
`risk_penalty`, kept apart so the orchestrator can compute:

    combined_score = event_score + confirmation_score - risk_penalty

Pure and deterministic: the snapshot is supplied by the caller (live wiring
computes it from price/volume + an on-chain quote; tests pass it directly), so
nothing here touches the network.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings

# Positive weights.
W_VOL_SPIKE = 25
W_BREAKOUT = 20
W_LIQUIDITY = 15
# Risk penalties.
P_OVERPUMPED = 30
P_BAD_LIQUIDITY = 50


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    contract: str = ""
    vol_5m: float = 0.0              # last 5-minute volume (USD or token, consistent)
    baseline_vol: float = 0.0        # recent average 5-minute volume
    price_now: float = 0.0
    price_5m_ago: float = 0.0
    # Authoritative 5m move from the SAME kline source as the volume (Binance Alpha for
    # memes / spot for majors). When set, the breakout scan uses THIS instead of the
    # tick-sampled CMC price cache (which lags thin memes → late entries). None = fall
    # back to the cache computation (legacy path / providers that don't supply a move).
    breakout_pct: float | None = None
    recent_pump_pct: float = 0.0     # max % up over a short recent window (0.20 = +20%)
    slippage_est: float = 1.0        # estimated slippage for our order size (0.01 = 1%)
    has_route: bool = False          # a PancakeSwap route exists
    liquidity_ok: bool = False       # pool deep enough for our size


@dataclass(frozen=True)
class MarketConfirmation:
    symbol: str
    confirmation_score: float
    risk_penalty: float
    breakout_pct: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def net(self) -> float:
        return self.confirmation_score - self.risk_penalty


def assess(snap: MarketSnapshot, *,
           vol_spike_mult: float | None = None,
           breakout_pct: float | None = None,
           overpump_pct: float | None = None,
           max_slippage: float | None = None) -> MarketConfirmation:
    vol_spike_mult = settings.aegis_vol_spike_mult if vol_spike_mult is None else vol_spike_mult
    breakout_pct = settings.aegis_breakout_pct if breakout_pct is None else breakout_pct
    overpump_pct = settings.aegis_overpump_pct if overpump_pct is None else overpump_pct
    max_slippage = settings.slippage_fraction if max_slippage is None else max_slippage

    score = 0.0
    penalty = 0.0
    reasons: list[str] = []

    breakout = 0.0
    if snap.price_5m_ago > 0:
        breakout = (snap.price_now - snap.price_5m_ago) / snap.price_5m_ago

    # Bad liquidity is disqualifying — penalise hard and skip the upside checks.
    if not snap.has_route or not snap.liquidity_ok or snap.slippage_est > max_slippage:
        penalty += P_BAD_LIQUIDITY
        reasons.append(f"-{P_BAD_LIQUIDITY} thin liquidity / bad route / high slippage")
        return MarketConfirmation(snap.symbol, score, penalty, breakout, tuple(reasons))

    if snap.baseline_vol > 0 and snap.vol_5m >= vol_spike_mult * snap.baseline_vol:
        score += W_VOL_SPIKE
        reasons.append(f"+{W_VOL_SPIKE} 5m volume >= {vol_spike_mult:g}x baseline")

    if breakout >= breakout_pct:
        score += W_BREAKOUT
        reasons.append(f"+{W_BREAKOUT} price breakout {breakout*100:.1f}%")

    score += W_LIQUIDITY
    reasons.append(f"+{W_LIQUIDITY} liquidity ok, slippage {snap.slippage_est*100:.2f}%")

    # Already blown off the move -> high reversal risk.
    if snap.recent_pump_pct >= overpump_pct:
        penalty += P_OVERPUMPED
        reasons.append(f"-{P_OVERPUMPED} already pumped {snap.recent_pump_pct*100:.0f}%")

    return MarketConfirmation(snap.symbol, score, penalty, breakout, tuple(reasons))
