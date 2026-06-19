"""Regime layer — the DEPLOYMENT VALVE, and the real DQ control.

Meme/Alpha tokens are highly correlated: in a market crash three positions behave
like one leveraged bet, so the latched −20% breaker alone is not enough. The regime
flag throttles how much risk the sniper may carry, BEFORE a position is ever opened:

    RISK_ON   → 20% NAV/position, up to 3 slots   (BTC calm/up)
    CAUTIOUS  → 15% NAV/position, up to 2 slots   (BTC choppy / mildly down)
    RISK_OFF  → 0% / 0 slots → NO new entries      (BTC dumping; rails also trim)

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


_PARAMS: dict[Regime, RegimeParams] = {
    Regime.RISK_ON: RegimeParams(0.20, 3, True),
    Regime.CAUTIOUS: RegimeParams(0.15, 2, True),
    Regime.RISK_OFF: RegimeParams(0.0, 0, False),
}


def params(flag: Regime | str) -> RegimeParams:
    return _PARAMS[Regime(flag)]


def position_usd(nav_usd: float, flag: Regime | str) -> float:
    """Per-position size in USD for the current regime (0 in RISK_OFF)."""
    return max(0.0, nav_usd * params(flag).size_pct)


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

    def to_dict(self) -> dict:
        return {"flag": self.flag, "updated_at": self.updated_at, "reason": self.reason}

    @classmethod
    def load(cls, path: Path) -> RegimeState:
        if not path.exists():
            return cls()
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(flag=d.get("flag", Regime.CAUTIOUS.value),
                   updated_at=float(d.get("updated_at", 0.0)), reason=d.get("reason", ""))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()), encoding="utf-8")


def current_regime(state: RegimeState, *, max_age_s: float, now: float) -> Regime:
    """Resolve the effective regime, downgrading a STALE flag to CAUTIOUS."""
    if now - state.updated_at > max_age_s:
        return Regime.CAUTIOUS
    return Regime(state.flag)


def decide_regime(btc_quote: dict) -> tuple[Regime, str]:
    """Map a CMC BTC quote (percent_change_* in PERCENT) to a regime + reason.

    This is the hourly updater's brain. A deterministic classifier on BTC momentum
    is robust and free; an LLM read can later refine it but must not loosen it.
    """
    c1 = float(btc_quote.get("percent_change_1h") or 0.0) / 100.0
    c24 = float(btc_quote.get("percent_change_24h") or 0.0) / 100.0
    flag = classify_btc(change_1h=c1, change_24h=c24)
    return flag, f"BTC 1h {c1 * 100:+.1f}% / 24h {c24 * 100:+.1f}%"
