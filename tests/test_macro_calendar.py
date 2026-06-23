"""TDD for the CMC Agent Hub macro-calendar guard (pure date logic, no network)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.agent.aegis import macro_calendar as mc

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def test_parse_event_date_human_formats():
    assert mc.parse_event_date("4 July 2026") == date(2026, 7, 4)
    assert mc.parse_event_date("28 Jul 2026") == date(2026, 7, 28)
    assert mc.parse_event_date("  1 January 2027 ") == date(2027, 1, 1)


def test_parse_event_date_bad_input_is_none():
    for bad in (None, "", "next week", "2026-07-04", "garbage"):
        assert mc.parse_event_date(bad) is None


def test_annotate_drops_past_sorts_and_tags_days():
    events = [
        {"title": "Far", "date": "28 July 2026"},
        {"title": "Past", "date": "1 June 2026"},          # dropped (already happened)
        {"title": "Near", "date": "25 June 2026"},
        {"title": "Unparseable", "date": "soon"},          # dropped
    ]
    out = mc.annotate(events, NOW)
    assert [e["title"] for e in out] == ["Near", "Far"]    # past + bad dropped, nearest first
    assert out[0]["days_until"] == 2 and out[1]["days_until"] == 35


def test_guard_blocks_when_event_is_imminent():
    events = [{"title": "FOMC", "date": "23 June 2026"}]   # today
    block, reason = mc.guard(events, NOW, within_days=1)
    assert block and "FOMC" in reason and "today" in reason


def test_guard_blocks_event_tomorrow_within_window():
    events = [{"title": "CPI", "date": "24 June 2026"}]
    block, reason = mc.guard(events, NOW, within_days=1)
    assert block and "in 1d" in reason


def test_guard_does_not_block_distant_event():
    # The real contest case: nearest CMC event is 1 July (8 days out) → no block this week.
    events = [{"title": "MiCA Fully Enforced", "date": "1 July 2026"}]
    block, reason = mc.guard(events, NOW, within_days=1)
    assert block is False and reason is None


def test_guard_failsafe_on_empty_or_garbage():
    assert mc.guard([], NOW, within_days=1) == (False, None)
    assert mc.guard(None, NOW, within_days=1) == (False, None)
    assert mc.guard([{"title": "X", "date": "whenever"}], NOW, within_days=1) == (False, None)
