"""Tournament clock — convex-when-behind risk escalation for the final stretch.

A 7-day RAW-RETURN tournament with a 30%-drawdown DQ gate is won from the right tail,
not from cash: steady small gains land mid-pack. This module is the brain that, in the
final window AND only while we are NOT yet in a likely-paying position, ESCALATES the
meme LOTTERY sleeve (bounded downside per ticket, convex upside) — never the correlated
beta-major basket (levering correlated exposure is what bleeds wallets in a choppy week
and is the real DQ risk). Levers: extra lottery slots, a bigger ticket, and letting the
lottery ignore the daily soft breaker. The whole push is capped by a drawdown budget.

PURE + deterministic + fail-safe. Gated by TOURNAMENT_CLOCK_ENABLED in the caller: when
disabled the directive is INACTIVE and caller behaviour is byte-identical to before.

Guards (all must pass to escalate):
  * enabled                         — master switch.
  * regime != RISK_OFF              — never buy lottery tickets into a market-wide crash
                                      (every meme dumps together = correlated loss).
  * days_left <= arm_days           — only late in the contest.
  * our_return < safe_return        — only while NOT likely already in a paying spot
                                      (>= safe_return => PROTECT: hold, do not push).
  * current_dd < max_push_dd        — kill-switch: stop once we've spent the DD budget.
Two tiers by time: `arm` (final ~48h, light) and `full_send` (final ~24h, heavier).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import regime as rg


@dataclass(frozen=True)
class ClockDirective:
    """What the clock asks the caller to do this tick. Defaults = no-op (inactive)."""
    active: bool = False
    extra_meme_slots: int = 0        # extra lottery slots BEYOND the regime cap
    meme_ticket_mult: float = 1.0    # multiply the meme ticket size (1.0 = unchanged)
    relax_meme_breaker: bool = False # let memes ignore the daily soft breaker (beta still respects it)
    reason: str = "off"


def decide_clock(
    *,
    now: float,
    contest_end: float,
    our_return: float,
    current_dd: float,
    regime_flag: rg.Regime | str,
    enabled: bool,
    arm_days: float = 2.0,
    full_send_days: float = 1.0,
    safe_return: float = 0.15,
    max_push_dd: float = 0.15,
    extra_slots_arm: int = 1,
    extra_slots_full: int = 2,
    ticket_mult_arm: float = 1.4,
    ticket_mult_full: float = 2.0,
) -> ClockDirective:
    """Resolve the escalation directive from the contest clock + our standing.

    `now`/`contest_end` are epoch seconds; `our_return`/`current_dd`/`safe_return`/
    `max_push_dd` are fractions (e.g. 0.15 = 15%). Never raises — any odd input that
    fails a guard simply yields an inactive directive (fail-safe to normal behaviour).
    """
    if not enabled:
        return ClockDirective(reason="disabled")
    if rg.Regime(regime_flag) == rg.Regime.RISK_OFF:
        return ClockDirective(reason="risk_off")

    days_left = (contest_end - now) / 86400.0
    if days_left < 0:
        return ClockDirective(reason="contest_over")
    if days_left > arm_days:
        return ClockDirective(reason="too_early")
    if our_return >= safe_return:
        return ClockDirective(reason="protect")        # likely in a paying spot → hold
    if current_dd >= max_push_dd:
        return ClockDirective(reason="dd_budget_spent")  # kill-switch

    if days_left <= full_send_days:
        return ClockDirective(active=True, extra_meme_slots=extra_slots_full,
                              meme_ticket_mult=ticket_mult_full,
                              relax_meme_breaker=True, reason="full_send")
    return ClockDirective(active=True, extra_meme_slots=extra_slots_arm,
                          meme_ticket_mult=ticket_mult_arm,
                          relax_meme_breaker=True, reason="arm")
