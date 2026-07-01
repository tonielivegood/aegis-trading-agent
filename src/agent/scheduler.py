"""APScheduler wiring for the live window: strategy tick + hourly PnL snapshot."""
from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from . import agent_loop
from .config import settings
from .monitor import notifier
from .monitor.logger import get_logger

log = get_logger(__name__)


def _resync_cadence(sched: BlockingScheduler) -> None:
    """Speed up to `event_tick_seconds_holding` while a position is open, drop back
    to the default `event_tick_seconds` once flat — re-evaluated after EVERY tick so
    a fresh entry/exit this tick takes effect starting the very next one, not two
    ticks later. Protects/harvests an open position more closely (the MYX lesson:
    a 60s-only tick missed the real peak); no urgency while just scanning, flat."""
    from .aegis.positions import PositionBook
    try:
        holding = bool(PositionBook.load(agent_loop.POSITIONS_FILE).positions)
    except Exception:  # noqa: BLE001 — a diagnostic read must never break scheduling
        holding = False
    seconds = settings.event_tick_seconds_holding if holding else settings.event_tick_seconds
    job = sched.get_job("strategy_tick")
    current = job.trigger.interval.total_seconds() if job else None
    if job is not None and current != seconds:
        sched.reschedule_job("strategy_tick", trigger="interval", seconds=seconds)
        log.info("tick_cadence_changed", seconds=seconds, holding=holding)


def _safe_tick(dry_run: bool, sched: BlockingScheduler | None = None) -> None:
    try:
        agent_loop.tick(dry_run)
    except Exception as e:  # noqa: BLE001 — keep the scheduler alive; alert and retry next tick
        log.exception("tick_failed")
        notifier.send(notifier.format_error(str(e)))
    if sched is not None:
        _resync_cadence(sched)


def run(dry_run: bool) -> None:
    sched = BlockingScheduler(timezone="UTC")
    # Event radar runs on a fast (hybrid 60s, faster while holding) cadence; baseline
    # holds tick slowly and has no per-position urgency concept.
    event_mode = settings.strategy_mode == "event_alpha" and settings.event_radar_enabled
    if event_mode:
        interval = {"seconds": settings.event_tick_seconds}
        cadence = f"{settings.event_tick_seconds}s (fast {settings.event_tick_seconds_holding}s while holding)"
        job_fn = lambda: _safe_tick(dry_run, sched)  # noqa: E731
    else:
        interval = {"minutes": settings.strategy_tick_min}
        cadence = f"{settings.strategy_tick_min}min"
        job_fn = lambda: _safe_tick(dry_run)  # noqa: E731
    sched.add_job(job_fn, "interval", id="strategy_tick",
                  next_run_time=datetime.now(timezone.utc), **interval)  # first tick immediately
    log.info("scheduler_started", cadence=cadence, event_mode=event_mode, dry_run=dry_run)
    notifier.send(f"🤖 Agent started [{'DRY-RUN' if dry_run else 'LIVE'}] · tick {cadence}")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")
        notifier.send("🛑 Agent stopped")
