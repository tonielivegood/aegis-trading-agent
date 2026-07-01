"""Regime layer — the DEPLOYMENT VALVE, and the real DQ control.

Meme/Alpha tokens are highly correlated: in a market crash three positions behave
like one leveraged bet, so the latched −20% breaker alone is not enough. The regime
flag throttles how much risk the sniper may carry, BEFORE a position is ever opened:

    RISK_ON   → 35% NAV/position, up to 2 slots   (BTC calm/up)
    CAUTIOUS  → 20% NAV/position, up to 1 slot    (BTC choppy / mildly down)
    RISK_OFF  → 0% / 0 slots → NO new entries      (BTC dumping; rails also trim)

Sizing is CONCENTRATED (few, heavy positions) on purpose: at small capital a
winner only moves the total return if the position is big, and fewer round-trips
means less fee bleed. RISK_ON caps total deployment at 2×35% = 70% NAV, leaving a
30% cash cushion under the −20% DQ breaker.

A separate hourly updater (Claude reading BTC via CMC / Agent Hub) writes the flag;
the 60s rails just READ it — cheap and deterministic. This module is pure: the
flag→params mapping, NAV sizing, a deterministic fallback classifier, and the
persisted state. No network here.

Fail-safe: a cold start, or a STALE flag (updater dead), resolves to CAUTIOUS — we
reduce exposure when we are not sure, but never silently stay aggressive, and never
hard-halt (which would risk the min-trade rule) on staleness alone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Regime(str, Enum):
    RISK_ON = "risk_on"
    CAUTIOUS = "cautious"
    RISK_OFF = "risk_off"


@dataclass(frozen=True)
class RegimeParams:
    size_pct: float      # position size as a fraction of NAV
    max_slots: int       # max concurrent positions allowed
    allow_new: bool      # may we open new positions at all
    entry_vol_factor: float = 1.0  # scales the volume-breakout bar (<1 = more aggressive)


# entry_vol_factor scales the volume-breakout bar per regime. It is now 1.0 in every
# regime: the regime expresses RISK APPETITE via position SIZE and SLOTS, NOT by
# loosening signal quality. The earlier RISK_ON 0.75 "beta-capture valve" (major bar
# 2.0→1.5×) was REMOVED after a live soak (21/6) showed it over-fired in an active
# market — many marginal 1.5× spikes that mean-reverted → fee/slippage churn that bled
# equity ~4%/2h. Fewer, higher-conviction entries (full bar) + the meme asymmetric ride
# are the raw-return edge; regime still throttles exposure (RISK_ON 35%/2 vs CAUTIOUS
# 20%/1 vs RISK_OFF 0). Downside bounded by per-position stop, regime-to-cash, breaker.
_PARAMS: dict[Regime, RegimeParams] = {
    Regime.RISK_ON: RegimeParams(0.35, 2, True, entry_vol_factor=1.0),
    Regime.CAUTIOUS: RegimeParams(0.20, 1, True, entry_vol_factor=1.0),
    Regime.RISK_OFF: RegimeParams(0.0, 0, False, entry_vol_factor=1.0),
}


def params(flag: Regime | str) -> RegimeParams:
    return _PARAMS[Regime(flag)]


def position_usd(nav_usd: float, flag: Regime | str) -> float:
    """Per-position size in USD for the current regime (0 in RISK_OFF)."""
    return max(0.0, nav_usd * params(flag).size_pct)


# CMC Agent Hub Fear & Greed floor: at/below this index the whole market is in
# (near-)panic. A momentum strategy that LOOSENS its entry bar in RISK_ON (beta
# capture) must not do so into a market-wide sell-off, even if BTC's last hour looks
# calm. So extreme fear caps aggression — it can only TIGHTEN (RISK_ON → CAUTIOUS),
# never loosen (Greed is NOT a green light: it correlates with blow-off tops).
SENTIMENT_FEAR_FLOOR = 20


def apply_sentiment_floor(flag: Regime | str, fg_value: int | None) -> tuple[Regime, str]:
    """Tighten the regime when CMC's Fear & Greed index shows extreme fear.

    Returns (possibly-tightened regime, reason-suffix). A missing read or a
    non-RISK_ON regime is a no-op (empty suffix) — this NEVER upgrades a regime.
    """
    flag = Regime(flag)
    if fg_value is not None and flag == Regime.RISK_ON and fg_value <= SENTIMENT_FEAR_FLOOR:
        return Regime.CAUTIOUS, f"sentiment floor: F&G {fg_value} ≤ {SENTIMENT_FEAR_FLOOR}"
    return flag, ""


def beta_regime(flag: Regime | str, fg_value: int | None,
                floor: int = SENTIMENT_FEAR_FLOOR) -> Regime:
    """Beta-specific regime: force RISK_OFF on extreme fear, even from CAUTIOUS.

    Fixes the 24/6 whipsaw (beta rotated majors 4x in 9h during CAUTIOUS + extreme
    fear + choppy BTC): `apply_sentiment_floor` only caps RISK_ON -> CAUTIOUS, so
    beta kept trend-following through fear-driven chop. This goes one step further
    for beta only — the meme sniper's own regime handling is unaffected.
    Tightening-only: a missing fg_value or an already-RISK_OFF flag is a no-op."""
    flag = Regime(flag)
    if fg_value is not None and fg_value <= floor and flag != Regime.RISK_OFF:
        return Regime.RISK_OFF
    return flag


def classify_btc(*, change_1h: float, change_24h: float,
                 off_24h: float = -0.08, off_1h: float = -0.04,
                 caution_24h: float = -0.03, caution_1h: float = 0.025) -> Regime:
    """Deterministic fallback classifier from BTC momentum (fractions, e.g. -0.05).

    Used when no LLM read is available, and as the sanity floor for one. A hard
    1h or 24h drop ⇒ RISK_OFF; a mild drop or choppy 1h ⇒ CAUTIOUS; else RISK_ON.
    """
    if change_24h <= off_24h or change_1h <= off_1h:
        return Regime.RISK_OFF
    if change_24h <= caution_24h or abs(change_1h) >= caution_1h:
        return Regime.CAUTIOUS
    return Regime.RISK_ON


@dataclass
class RegimeState:
    flag: str = Regime.CAUTIOUS.value     # cold-start default: cautious, not aggressive
    updated_at: float = 0.0
    reason: str = ""
    fg_value: int | None = None           # last-read Fear & Greed value (for beta_regime)

    def to_dict(self) -> dict:
        return {"flag": self.flag, "updated_at": self.updated_at, "reason": self.reason,
                "fg_value": self.fg_value}

    @classmethod
    def load(cls, path: Path) -> RegimeState:
        if not path.exists():
            return cls()
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(flag=d.get("flag", Regime.CAUTIOUS.value),
                   updated_at=float(d.get("updated_at", 0.0)), reason=d.get("reason", ""),
                   fg_value=d.get("fg_value"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()), encoding="utf-8")


def current_regime(state: RegimeState, *, max_age_s: float, now: float) -> Regime:
    """Resolve the effective regime, downgrading a STALE flag to CAUTIOUS."""
    if now - state.updated_at > max_age_s:
        return Regime.CAUTIOUS
    return Regime(state.flag)


def decide_regime(btc_quote: dict, fear_greed: dict | int | None = None) -> tuple[Regime, str]:
    """Map a CMC BTC quote (percent_change_* in PERCENT) to a regime + reason.

    This is the hourly updater's brain. A deterministic classifier on BTC momentum
    is robust and free; the CMC Agent Hub Fear & Greed read (``fear_greed``) refines
    it via a TIGHTENING-ONLY overlay (extreme fear caps RISK_ON), never loosening it.
    """
    c1 = float(btc_quote.get("percent_change_1h") or 0.0) / 100.0
    c24 = float(btc_quote.get("percent_change_24h") or 0.0) / 100.0
    flag = classify_btc(change_1h=c1, change_24h=c24)
    reason = f"BTC 1h {c1 * 100:+.1f}% / 24h {c24 * 100:+.1f}%"
    fg_value = fear_greed.get("value") if isinstance(fear_greed, dict) else fear_greed
    flag, suffix = apply_sentiment_floor(flag, fg_value)
    if suffix:
        reason = f"{reason}; {suffix}"
    return flag, reason
