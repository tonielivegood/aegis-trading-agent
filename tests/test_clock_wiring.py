"""Tests for the tournament-clock wiring into the event tick (pure helpers)."""
from __future__ import annotations

import datetime as dt

from src.agent import agent_loop as al
from src.agent.aegis.regime import Regime
from src.agent.aegis.tournament_clock import ClockDirective


def test_apply_clock_inactive_is_noop():
    cap, usd, flag, label = al._apply_clock(
        ClockDirective(), meme_cap=1, base_max_slots=1,
        entry_flag=Regime.CAUTIOUS, flag=Regime.CAUTIOUS, meme_order_usd=5.0)
    assert cap == 1 and usd == 5.0 and flag == Regime.CAUTIOUS and label == ""


def test_apply_clock_active_arm_escalates():
    d = ClockDirective(active=True, extra_meme_slots=1, meme_ticket_mult=1.4,
                       relax_meme_breaker=True, reason="arm")
    # entry_flag is RISK_OFF here as if a daily-halt forced it off; relax hands memes the true flag.
    cap, usd, flag, label = al._apply_clock(
        d, meme_cap=1, base_max_slots=1, entry_flag=Regime.RISK_OFF,
        flag=Regime.CAUTIOUS, meme_order_usd=5.0)
    assert cap == 2                        # 1 + 1 extra → ABOVE the regime cap (the convex push)
    assert usd == 5.0 * 1.4                # bigger ticket
    assert flag == Regime.CAUTIOUS         # relaxed: memes ignore the daily soft breaker
    assert label == "+clock:arm"


def test_apply_clock_uses_base_slots_when_meme_cap_none():
    d = ClockDirective(active=True, extra_meme_slots=2, meme_ticket_mult=2.0,
                       relax_meme_breaker=True, reason="full_send")
    cap, *_ = al._apply_clock(d, meme_cap=None, base_max_slots=1,
                              entry_flag=Regime.CAUTIOUS, flag=Regime.CAUTIOUS, meme_order_usd=5.0)
    assert cap == 3                        # base 1 + 2 extra


def test_apply_clock_no_relax_keeps_entry_flag():
    d = ClockDirective(active=True, extra_meme_slots=1, meme_ticket_mult=1.4,
                       relax_meme_breaker=False, reason="arm")
    _, _, flag, _ = al._apply_clock(d, meme_cap=0, base_max_slots=1,
                                    entry_flag=Regime.RISK_OFF, flag=Regime.CAUTIOUS,
                                    meme_order_usd=5.0)
    assert flag == Regime.RISK_OFF         # not relaxed → memes stay halted


def test_contest_end_epoch_parses_iso(monkeypatch):
    monkeypatch.setattr(al.settings, "tournament_clock_end", "2026-06-28T00:00:00Z")
    expect = dt.datetime(2026, 6, 28, tzinfo=dt.timezone.utc).timestamp()
    assert abs(al._contest_end_epoch() - expect) < 1.0


def test_contest_end_epoch_failsafe_on_bad_value(monkeypatch):
    import time
    monkeypatch.setattr(al.settings, "tournament_clock_end", "not-a-date")
    assert al._contest_end_epoch() > time.time() + 300 * 86400   # far future → clock inactive
