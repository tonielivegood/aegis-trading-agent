"""Claude regime advisor — an HOURLY, advisory, TIGHTENING-ONLY overlay.

This is the one place an LLM touches the trading decision, and it is fenced off hard:

- **Out of the hot path.** Called only by the hourly regime updater, never the 60s tick.
- **Tightening-only.** Claude reads BTC H1/24h momentum + the CMC Fear & Greed index and
  recommends a regime, but the result can only step the agent to the SAME or a MORE
  DEFENSIVE posture (RISK_ON → CAUTIOUS → RISK_OFF). It can never make the agent more
  aggressive — enforced in code (`min` risk rank), not just in the prompt. So a
  hallucination or prompt-injection can only ever REDUCE risk.
- **Fail-safe.** Any error, timeout, missing key, or unparseable reply returns the base
  regime unchanged. The advisor can never block or break the updater.
- **Bounded output.** Claude's reply is parsed to a regime enum; it is never executed as
  an instruction.

Uses the official Anthropic SDK (Haiku by default — cheap/fast for an hourly call).
"""
from __future__ import annotations

import anthropic

from ..config import settings
from ..monitor.logger import get_logger
from .regime import Regime

log = get_logger(__name__)

# Risk rank: lower = more defensive. "Tightening" means moving to a lower rank.
_RISK_RANK = {Regime.RISK_OFF: 0, Regime.CAUTIOUS: 1, Regime.RISK_ON: 2}

# DANGER GATE (recalibration "b", 22/6): Claude's tightening is APPLIED only when a hard,
# concrete danger signal corroborates it. A low Fear & Greed reading on its own — while BTC is
# flat/up — must NOT sit us out of an alt rally (this kept us at 1 beta name in a rising,
# fearful-sentiment market). Real danger = true extreme fear OR a genuine BTC drop.
_DANGER_FG = 20       # Fear & Greed <= this = genuine extreme fear
_DANGER_24H = -3.0    # BTC 24h % <= this = a real down day
_DANGER_1H = -2.0     # BTC 1h %  <= this = a sharp drop now

_SYSTEM = (
    "You are the risk officer for an autonomous crypto trading agent on BNB Chain. "
    "DEFAULT to keeping the agent's current mechanical regime. Recommend a MORE "
    "DEFENSIVE regime ONLY on concrete danger: a sharp BTC drop (24h <= -3% or a sharp "
    "1h fall) OR accelerating downside momentum. A low Fear & Greed reading is NORMAL in "
    "this market and is NOT by itself a reason to step down — only treat fear as danger "
    "when the index is <= 20 AND momentum is also negative. Mild chop, neutral sentiment, "
    "small moves, or fear with flat/positive momentum are NOT reasons to step down. You "
    "may never recommend a more aggressive regime than the current one. Reply with EXACTLY "
    "two lines: line 1 is one of RISK_ON, CAUTIOUS, RISK_OFF; line 2 is a one-sentence "
    "reason. Output nothing else."
)


def _parse_regime(text: str) -> Regime | None:
    for line in text.strip().splitlines():
        t = line.strip().upper().strip("*-•# ")
        for r in (Regime.RISK_OFF, Regime.CAUTIOUS, Regime.RISK_ON):
            if t == r.value.upper() or t == r.name:
                return r
    return None


def advise_regime(base: Regime | str, *, btc_quote: dict,
                  fear_greed: dict | int | None) -> tuple[Regime, str, str]:
    """Return (effective_regime, claude_recommendation, claude_reason).

    `effective_regime` is the more defensive of `base` and Claude's recommendation
    (tightening-only). Fail-safe: returns (base, "", "") if disabled, unconfigured,
    or on any error.
    """
    base = Regime(base)
    if not settings.claude_advisor_enabled or not settings.anthropic_api_key:
        return base, "", ""
    try:
        c1 = float(btc_quote.get("percent_change_1h") or 0.0)
        c24 = float(btc_quote.get("percent_change_24h") or 0.0)
        fg = fear_greed.get("value") if isinstance(fear_greed, dict) else fear_greed
        user = (
            f"Current mechanical regime: {base.value}.\n"
            f"BTC momentum: 1h {c1:+.2f}%, 24h {c24:+.2f}%.\n"
            f"Fear & Greed index: {fg if fg is not None else 'n/a'} "
            "(0 = extreme fear, 100 = extreme greed).\n"
            "Recommend the regime (same or more defensive only)."
        )
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key,
                                     timeout=20.0, max_retries=1)
        resp = client.messages.create(
            model=settings.anthropic_model, max_tokens=120,
            system=_SYSTEM, messages=[{"role": "user", "content": user}])
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text")
        rec = _parse_regime(text)
        if rec is None:
            return base, "", ""
        # TIGHTENING-ONLY: never more aggressive than the mechanical base.
        # DANGER GATE: a downgrade is APPLIED only when a hard danger signal corroborates it
        # (real extreme fear or a genuine BTC drop). Otherwise Claude's caution is recorded
        # but NOT applied — we don't sit out a flat/rising market on fear-sentiment alone.
        # This only ever RESTRICTS a step-down; it can never add risk.
        danger = ((fg is not None and fg <= _DANGER_FG) or c24 <= _DANGER_24H or c1 <= _DANGER_1H)
        wants_tighten = _RISK_RANK[rec] < _RISK_RANK[base]
        effective = rec if (wants_tighten and danger) else base
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        reason = lines[1][:160] if len(lines) >= 2 else ""
        log.info("claude_regime", recommended=rec.value, base=base.value,
                 applied=effective.value, danger=danger)
        return effective, rec.value, reason
    except Exception as e:  # noqa: BLE001 — advisory must never break the regime updater
        log.info("claude_advisor_failed", error=type(e).__name__)
        return base, "", ""
