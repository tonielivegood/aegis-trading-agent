"""Tests for the tournament-clock escalation brain (pure, fail-safe, gated)."""
from __future__ import annotations

from src.agent.aegis import tournament_clock as tc
from src.agent.aegis.regime import Regime

DAY = 86400.0
END = 1_000_000.0  # arbitrary contest-end epoch


def _decide(**kw):
    """Sensible 'behind, late, healthy, enabled' baseline; override per test."""
    base = dict(
        now=END - 1.5 * DAY,      # 1.5 days left → in the arm window
        contest_end=END,
        our_return=0.04,          # +4% → below the +15% safe line
        current_dd=0.02,          # 2% → well under the 15% budget
        regime_flag=Regime.CAUTIOUS,
        enabled=True,
    )
    base.update(kw)
    return tc.decide_clock(**base)


def test_disabled_is_inactive():
    d = _decide(enabled=False)
    assert not d.active and d.reason == "disabled"


def test_risk_off_never_escalates():
    # Even late + behind, do not buy lottery tickets into a market-wide crash.
    d = _decide(regime_flag=Regime.RISK_OFF)
    assert not d.active and d.reason == "risk_off"


def test_too_early_is_inactive():
    d = _decide(now=END - 4 * DAY)   # 4 days left, arm window is 2
    assert not d.active and d.reason == "too_early"


def test_contest_over_is_inactive():
    d = _decide(now=END + DAY)       # past the end
    assert not d.active and d.reason == "contest_over"


def test_protect_when_already_safe():
    # In a likely-paying spot → hold/protect, do NOT push.
    d = _decide(our_return=0.20)     # +20% >= +15% safe line
    assert not d.active and d.reason == "protect"


def test_safe_return_boundary_is_protect():
    d = _decide(our_return=0.15, safe_return=0.15)   # exactly at the line
    assert not d.active and d.reason == "protect"


def test_kill_switch_when_dd_budget_spent():
    d = _decide(current_dd=0.15, max_push_dd=0.15)   # hit the 15% budget
    assert not d.active and d.reason == "dd_budget_spent"


def test_arm_tier_in_final_48h():
    d = _decide(now=END - 1.5 * DAY)  # between full-send (1d) and arm (2d)
    assert d.active and d.reason == "arm"
    assert d.extra_meme_slots == 1
    assert d.meme_ticket_mult == 1.4
    assert d.relax_meme_breaker is True


def test_full_send_tier_in_final_24h():
    d = _decide(now=END - 0.5 * DAY)  # inside the final 24h
    assert d.active and d.reason == "full_send"
    assert d.extra_meme_slots == 2
    assert d.meme_ticket_mult == 2.0
    assert d.relax_meme_breaker is True


def test_risk_on_can_escalate():
    d = _decide(regime_flag=Regime.RISK_ON)
    assert d.active and d.reason == "arm"


def test_inactive_directive_is_a_noop():
    # The dataclass defaults must be a true no-op so a disabled clock changes nothing.
    d = tc.ClockDirective()
    assert d.active is False
    assert d.extra_meme_slots == 0
    assert d.meme_ticket_mult == 1.0
    assert d.relax_meme_breaker is False


def test_custom_thresholds_respected():
    # Tighter safe line + a 1-day arm window.
    d = _decide(now=END - 0.8 * DAY, arm_days=1.0, full_send_days=0.5,
                safe_return=0.05, our_return=0.04)
    assert d.active and d.reason == "arm"   # 0.8d left: <=1d arm, >0.5d full-send
