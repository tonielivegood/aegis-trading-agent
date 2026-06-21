"""Token CLASS = a TWO-TIER engine matched to each tier's on-chain TRADING COST.

Measured round-trip cost (fee + gas + 2×slippage) at our ~$12 order size:
  - DEEP MAJORS (ETH/DOGE/SHIB/CAKE/LTC/LINK/XRP/ADA...): ~0.6% → CHEAP.
  - thin MEMES: ~6–12% → EXPENSIVE.

Cost dictates style:
  - MAJOR = active "harvest". Cheap, so trade FREQUENTLY for MODEST profit: enter
    on a small elevated bump (≥2x vol, caught early), take profit at +10%, trail
    tight (5%) so a +8% move isn't given back, cut fast (−5%), recycle in 20 min.
    A WIDE meme-trail here would hand back the small moves majors actually make.
  - MEME = rare "ride". Expensive, so trade SELDOM but for a BIG win: require a real
    ≥3x breakout, then RIDE it (trail 15% from peak, cap +200%), wider stop (−8%),
    25 min patience. Few trades, asymmetric upside that dwarfs the slippage moat.

Regime overlay (see regime.py): RISK_ON loosens the MAJOR bar only (beta-capture);
memes stay strict/rare in every regime. Pure data — scan/decide read these knobs;
values are tunable in a soak before go-live.
"""
from __future__ import annotations

from dataclasses import dataclass

MAJOR = "major"
MEME = "meme"


@dataclass(frozen=True)
class ClassParams:
    # --- entry ---
    vol_mult: float          # 5m/1m volume >= this x baseline
    breakout_min: float      # price must be up at least this (fraction)
    breakout_max: float      # ...but not already past this (catch it EARLY)
    # --- exit ---
    hard_tp_mult: float      # take full profit when value reaches Nx entry
    trailing_pct: float      # give back this much from peak -> exit
    hard_stop_pct: float     # hard per-position stop loss
    no_progress_min: int     # cut a flat trade after this many minutes


PARAMS: dict[str, ClassParams] = {
    # MAJOR: cheap (~0.6% round-trip) but a low bar OVER-TRADES. A live soak (21/6)
    # at an effective 1.5× bar fired ~18 entries/2h on marginal spikes that reverted →
    # slippage churn bled ~4%/2h. Raised to 2.5× + a clearer 0.5% breakout floor so
    # majors fire SELDOM, only on a real move worth the round-trip. Tight exit
    # (+10% TP, 5% trail, −5% stop, 20m recycle) still banks the small major moves.
    MAJOR: ClassParams(vol_mult=2.5, breakout_min=0.005, breakout_max=0.05,
                       hard_tp_mult=1.10, trailing_pct=0.05, hard_stop_pct=0.05,
                       no_progress_min=20),
    # MEME: expensive/thin (~6–12% round-trip) → RARE, BIG ride. Cap at +100% (a
    # reachable target that locks the win, vs +200% that rarely prints).
    MEME: ClassParams(vol_mult=3.0, breakout_min=0.0, breakout_max=0.06,
                      hard_tp_mult=2.0, trailing_pct=0.15, hard_stop_pct=0.08,
                      no_progress_min=25),
}


def params(token_class: str) -> ClassParams:
    return PARAMS.get(token_class, PARAMS[MEME])
