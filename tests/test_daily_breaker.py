"""TDD for the daily soft circuit-breaker — bound intraday bleed, reset each UTC day."""
from __future__ import annotations

from src.agent.risk.daily_breaker import DailyBreaker


def test_roll_anchors_open_equity_on_first_tick_of_day():
    db = DailyBreaker()
    db.roll(100.0, "2026-06-22")
    assert db.date == "2026-06-22" and db.open_equity == 100.0
    # later same-day ticks DON'T move the open
    db.roll(90.0, "2026-06-22")
    assert db.open_equity == 100.0


def test_roll_resets_on_new_day():
    db = DailyBreaker(date="2026-06-22", open_equity=100.0)
    db.roll(80.0, "2026-06-23")          # new UTC day re-anchors to current equity
    assert db.date == "2026-06-23" and db.open_equity == 80.0


def test_drawdown_and_halt_threshold():
    db = DailyBreaker(date="2026-06-22", open_equity=100.0)
    assert abs(db.drawdown(92.0) - 0.08) < 1e-9
    assert db.should_halt_new(92.0, 0.08) is True     # exactly at threshold → halt
    assert db.should_halt_new(93.0, 0.08) is False    # only -7% → still trading
    assert db.should_halt_new(50.0, 0.0) is False     # threshold 0 disables


def test_no_open_equity_is_safe():
    db = DailyBreaker()
    assert db.drawdown(50.0) == 0.0 and db.should_halt_new(50.0, 0.08) is False


def test_persistence_roundtrip(tmp_path):
    p = tmp_path / "daily.json"
    DailyBreaker(date="2026-06-22", open_equity=100.0).save(p)
    db = DailyBreaker.load(p)
    assert db.date == "2026-06-22" and db.open_equity == 100.0


def test_load_missing_or_corrupt_is_fresh(tmp_path):
    assert DailyBreaker.load(tmp_path / "nope.json") == DailyBreaker()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert DailyBreaker.load(bad) == DailyBreaker()
