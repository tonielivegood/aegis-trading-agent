"""Token CLASS = the two-speed ENTRY of the sniper, with a unified ASYMMETRIC exit.

Majors (deep liquidity) and memes (thin, explosive) start moving on different
signatures, so the ENTRY trigger differs by class:

  - MAJOR: a 3x-volume spike is rare, so enter on a smaller but still-elevated
    bump (≥2x volume) caught EARLY (≤+5%, before the move is spent). The only
    SAFELY tradable tokens are the ~13 deep-liquidity names (≈0% slippage), so the
    cost of a false signal on them is just the small price move — with the −7%
    asymmetric stop bounding it, we can afford the lower 2x bar to stay ACTIVE.
  - MEME : a real ≥3x-volume breakout is the trigger, caught EARLY (≤+6%). The
    higher bar is justified because a tradable meme carries real entry slippage.

The EXIT is the SAME for both — asymmetric "ride": cut a loser fast (−7%), but let
a winner RUN (trail 15% from peak, cap +200%). The old major "scalp" (+4%/2% trail)
was measured churning ~1%/round-trip on $7 positions in a live soak — death by a
thousand cuts — so it is removed. Track 1 ranks on RAW total return: a few
asymmetric winners beat many small scalps.

Pure data: the entry/exit knobs per class. scan/decide read these so one engine
serves both speeds. Values are tunable in a soak before go-live.
"""
from __future__ import annotations

from dataclasses import dataclass

MAJOR = "major"
MEME = "meme"

# Unified asymmetric-ride EXIT — identical for both classes (cut losers fast,
# let winners run). Only the ENTRY trigger differs by class.
RIDE_TP = 3.0            # +200% hard take-profit cap — let a real mover run
RIDE_TRAIL = 0.15        # give back 15% from peak → exit (ride, don't scalp)
RIDE_STOP = 0.07         # cut a loser fast at −7% (the asymmetric downside)
RIDE_NO_PROGRESS = 25    # 25 min to show progress, else cut a still-flat trade


@dataclass(frozen=True)
class ClassParams:
    # --- entry (two-speed) ---
    vol_mult: float          # 5m/1m volume >= this x baseline
    breakout_min: float      # price must be up at least this (fraction)
    breakout_max: float      # ...but not already past this (catch it EARLY)
    # --- exit (unified asymmetric ride) ---
    hard_tp_mult: float      # take full profit when value reaches Nx entry
    trailing_pct: float      # give back this much from peak -> exit
    hard_stop_pct: float     # hard per-position stop loss
    no_progress_min: int     # cut a flat trade after this many minutes


PARAMS: dict[str, ClassParams] = {
    # MAJOR: looser entry (≥2x, +0.3%..+5%) — deep/cheap tokens, so stay active; but RIDE, no scalp.
    MAJOR: ClassParams(vol_mult=2.0, breakout_min=0.003, breakout_max=0.05,
                       hard_tp_mult=RIDE_TP, trailing_pct=RIDE_TRAIL,
                       hard_stop_pct=RIDE_STOP, no_progress_min=RIDE_NO_PROGRESS),
    # MEME: strict 3x breakout trigger (0%..+6%), RIDE the move.
    MEME: ClassParams(vol_mult=3.0, breakout_min=0.0, breakout_max=0.06,
                      hard_tp_mult=RIDE_TP, trailing_pct=RIDE_TRAIL,
                      hard_stop_pct=RIDE_STOP, no_progress_min=RIDE_NO_PROGRESS),
}


def params(token_class: str) -> ClassParams:
    return PARAMS.get(token_class, PARAMS[MEME])
