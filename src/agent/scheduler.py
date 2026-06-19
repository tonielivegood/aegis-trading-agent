"""APScheduler wiring for the live window: strategy tick + hourly PnL snapshot."""
from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from . import agent_loop
from .config import settings
from .monitor import notifier
from .monitor.logger import get_logger

log = get_logger(__name__)


def _safe_tick(dry_run: bool) -> None:
    try:
        agent_loop.tick(dry_run)
    except Exception as e:  # noqa: BLE001 — keep the scheduler alive; alert and retry next tick
        log.exception("tick_failed")
        notifier.send(notifier.format_error(str(e)))


def run(dry_run: bool) -> None:
    sched = BlockingScheduler(timezone="UTC")
    # Event radar runs on a fast (hybrid 60s) cadence; baseline holds tick slowly.
    event_mode = settings.strategy_mode == "event_alpha" and settings.event_radar_enabled
    if event_mode:
        interval = {"seconds": settings.event_tick_seconds}
        cadence = f"{settings.event_tick_seconds}s"
    else:
        interval = {"minutes": settings.strategy_tick_min}
        cadence = f"{settings.strategy_tick_min}min"
    sched.add_job(lambda: _safe_tick(dry_run), "interval", id="strategy_tick",
                  next_run_time=datetime.now(timezone.utc), **interval)  # first tick immediately
    log.info("scheduler_started", cadence=cadence, event_mode=event_mode, dry_run=dry_run)
    notifier.send(f"🤖 Agent started [{'DRY-RUN' if dry_run else 'LIVE'}] · tick {cadence}")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")
        notifier.send("🛑 Agent stopped")
