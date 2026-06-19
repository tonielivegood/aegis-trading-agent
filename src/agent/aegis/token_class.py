"""Token CLASS = the two-speed engine of the sniper.

Majors (deep liquidity, low volatility) and memes (thin, explosive) need DIFFERENT
entry triggers AND exit targets:

  - MAJOR: a 3x-volume + 8-10% spike almost never happens, so the entry must be
    looser (smaller volume bump, small upward move) and we SCALP (+4%) because
    slippage is low enough that small targets are net-profitable.
  - MEME : a real 3x-volume breakout is the trigger, and we RIDE it (cap +200%)
    with a wide trailing stop, because the upside is large and the fees/slippage
    moat means small scalps lose money.

Pure data: the entry/exit knobs per class. scan/decide read these so one engine
serves both speeds. Values are tunable in a soak before go-live.
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
    breakout_max: float      # ...but not already past this (don't chase)
    # --- exit ---
    hard_tp_mult: float      # take full profit when value reaches Nx entry
    trailing_pct: float      # give back this much from peak -> exit
    hard_stop_pct: float     # hard per-position stop loss
    no_progress_min: int     # cut a flat trade after this many minutes


PARAMS: dict[str, ClassParams] = {
    # MAJOR: looser entry, fast scalp.
    MAJOR: ClassParams(vol_mult=2.0, breakout_min=0.003, breakout_max=0.03,
                       hard_tp_mult=1.04, trailing_pct=0.02, hard_stop_pct=0.035,
                       no_progress_min=10),
    # MEME: strict breakout trigger, ride the move.
    MEME: ClassParams(vol_mult=3.0, breakout_min=0.0, breakout_max=0.10,
                      hard_tp_mult=3.0, trailing_pct=0.15, hard_stop_pct=0.08,
                      no_progress_min=15),
}


def params(token_class: str) -> ClassParams:
    return PARAMS.get(token_class, PARAMS[MEME])
