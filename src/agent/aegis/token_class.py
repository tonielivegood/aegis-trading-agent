"""Token CLASS = two tiers, both now "CONFIRMED MOMENTUM + RIDE" (redesign 21/6).

A live soak (21/6, real money) proved the old logic — "catch a 0.5% blip on a
1-MINUTE volume spike, then recycle on a 20-minute timer" — was a CHURN MACHINE:
~18 entries in 2h on one-minute noise that mean-reverted, bleeding slippage (~−5%).
The signal had no edge and the TIME-BASED "no-progress" exit forced small losses and
immediate re-entry, paying the round-trip cost over and over.

Redesign principles (trader-driven):
  - ENTER ONLY ON A CONFIRMED MOVE: a SUSTAINED volume surge (5-MINUTE candles, not a
    1-minute blip) AND price already up a real amount (>=3%). We act on a trend that
    has begun — a slightly later entry, but the noise is filtered out.
  - THEN RIDE: a WIDE trailing stop + high cap let a winner run; the trailing stop
    (NOT a timer) banks profit. Losers are cut by a hard stop. There is NO time exit.
  - MEME = the asymmetric tail (+100-300% lives here). MAJOR = ACTIVE on a CONFIRMED
    move: a major having a good day routinely runs +10-30%, very tradable now that we
    filter noise (5m candles + a real +3% move) and RIDE instead of churning. Major's
    volume bar is LOWER than meme's (cheaper to trade, we want those days); the +3%
    confirmation + ride exits are what prevent the old churn, not a sky-high bar.

Exit is take-profit / hard-stop / trailing only (no_progress_min=0 disables the time
exit). Regime overlay (regime.py) only throttles EXPOSURE now (size/slots), never the
signal bar. Values are principled starting points, tunable in a soak — not backtested.
"""
from __future__ import annotations

from dataclasses import dataclass

MAJOR = "major"
MEME = "meme"


@dataclass(frozen=True)
class ClassParams:
    # --- entry ---
    vol_mult: float          # latest 5m candle volume >= this x the 5m baseline (median)
    breakout_min: float      # price must already be up at least this (confirmed move)
    breakout_max: float      # ...but not already blown off past this
    # --- exit (TP / hard-stop / trailing only; NO time-based exit) ---
    hard_tp_mult: float      # take full profit when value reaches Nx entry (the cap)
    trailing_pct: float      # give back this much from peak -> exit (the ride's real exit)
    hard_stop_pct: float     # hard per-position stop loss
    no_progress_min: int     # 0 = DISABLED (no time exit). Kept for back-compat only.


PARAMS: dict[str, ClassParams] = {
    # MAJOR — ACTIVE on a CONFIRMED move. A 2.5x sustained 5m volume surge WITH price
    # already +3% (up to +15%) = a real major move starting (good days run +10-30%).
    # RIDE it: 7% trail locks most of a modest major move, +30% hard-TP banks a great
    # day, cut at −7%. No time exit — the +3% confirmation + ride (not a high bar) are
    # what stop the churn, so a lower bar here is safe and lets us catch major days.
    MAJOR: ClassParams(vol_mult=2.5, breakout_min=0.03, breakout_max=0.15,
                       hard_tp_mult=1.30, trailing_pct=0.07, hard_stop_pct=0.07,
                       no_progress_min=0),
    # MEME — the asymmetric tail (primary). Confirmed ignition: 4x sustained 5m volume
    # AND price +3% (up to +20%, so we still catch a fast starter), then RIDE: 25% trail
    # from peak, cap +200%, −12% stop (thin, but our 1inch-built universe is <=~2.2%
    # slippage). No time exit — a winner runs until the trail or cap; a loser hits −12%.
    MEME: ClassParams(vol_mult=4.0, breakout_min=0.03, breakout_max=0.20,
                      hard_tp_mult=3.0, trailing_pct=0.25, hard_stop_pct=0.12,
                      no_progress_min=0),
}


def params(token_class: str) -> ClassParams:
    return PARAMS.get(token_class, PARAMS[MEME])
