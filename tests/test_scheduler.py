"""TDD for the scheduler's dynamic tick-cadence resync (fast tick while holding)."""
from __future__ import annotations

from src.agent import scheduler


class _FakeInterval:
    def __init__(self, seconds):
        self._seconds = seconds

    def total_seconds(self):
        return self._seconds


class _FakeTrigger:
    def __init__(self, seconds):
        self.interval = _FakeInterval(seconds)


class _FakeJob:
    def __init__(self, seconds):
        self.trigger = _FakeTrigger(seconds)


class _FakeScheduler:
    def __init__(self, current_seconds):
        self._job = _FakeJob(current_seconds)
        self.rescheduled_to = None

    def get_job(self, job_id):
        assert job_id == "strategy_tick"
        return self._job

    def reschedule_job(self, job_id, trigger, seconds):
        assert job_id == "strategy_tick" and trigger == "interval"
        self.rescheduled_to = seconds


def _book_with(mocker, positions: dict):
    book = mocker.Mock()
    book.positions = positions
    mocker.patch("src.agent.aegis.positions.PositionBook.load", return_value=book)


def test_resync_speeds_up_when_holding(mocker):
    mocker.patch.object(scheduler.settings, "event_tick_seconds", 60)
    mocker.patch.object(scheduler.settings, "event_tick_seconds_holding", 30)
    _book_with(mocker, {"MYX": object()})
    sched = _FakeScheduler(current_seconds=60)
    scheduler._resync_cadence(sched)
    assert sched.rescheduled_to == 30


def test_resync_slows_back_down_when_flat(mocker):
    mocker.patch.object(scheduler.settings, "event_tick_seconds", 60)
    mocker.patch.object(scheduler.settings, "event_tick_seconds_holding", 30)
    _book_with(mocker, {})
    sched = _FakeScheduler(current_seconds=30)   # was fast last tick
    scheduler._resync_cadence(sched)
    assert sched.rescheduled_to == 60


def test_resync_noop_when_cadence_already_correct(mocker):
    mocker.patch.object(scheduler.settings, "event_tick_seconds", 60)
    mocker.patch.object(scheduler.settings, "event_tick_seconds_holding", 30)
    _book_with(mocker, {})
    sched = _FakeScheduler(current_seconds=60)   # already at the flat cadence
    scheduler._resync_cadence(sched)
    assert sched.rescheduled_to is None


def test_resync_fails_safe_to_default_on_read_error(mocker):
    mocker.patch.object(scheduler.settings, "event_tick_seconds", 60)
    mocker.patch.object(scheduler.settings, "event_tick_seconds_holding", 30)
    mocker.patch("src.agent.aegis.positions.PositionBook.load", side_effect=RuntimeError("boom"))
    sched = _FakeScheduler(current_seconds=30)
    scheduler._resync_cadence(sched)   # must not raise
    assert sched.rescheduled_to == 60   # treats a read failure as "not holding"


def test_safe_tick_resyncs_after_a_successful_tick(mocker):
    mocker.patch.object(scheduler.agent_loop, "tick", return_value={})
    resync = mocker.patch.object(scheduler, "_resync_cadence")
    sched = _FakeScheduler(current_seconds=60)
    scheduler._safe_tick(True, sched)
    resync.assert_called_once_with(sched)


def test_safe_tick_still_resyncs_after_a_failed_tick(mocker):
    mocker.patch.object(scheduler.agent_loop, "tick", side_effect=RuntimeError("boom"))
    mocker.patch.object(scheduler, "notifier")
    resync = mocker.patch.object(scheduler, "_resync_cadence")
    sched = _FakeScheduler(current_seconds=60)
    scheduler._safe_tick(True, sched)   # must not raise
    resync.assert_called_once_with(sched)


def test_safe_tick_without_scheduler_skips_resync(mocker):
    mocker.patch.object(scheduler.agent_loop, "tick", return_value={})
    resync = mocker.patch.object(scheduler, "_resync_cadence")
    scheduler._safe_tick(True)   # baseline mode: no sched passed
    resync.assert_not_called()
