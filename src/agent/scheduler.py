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
    sched.add_job(lambda: _safe_tick(dry_run), "interval",
                  minutes=settings.strategy_tick_min, id="strategy_tick",
                  next_run_time=datetime.now(timezone.utc))  # run first tick immediately
    log.info("scheduler_started", tick_min=settings.strategy_tick_min, dry_run=dry_run)
    notifier.send(f"🤖 Agent started [{'DRY-RUN' if dry_run else 'LIVE'}] · tick {settings.strategy_tick_min}min")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")
        notifier.send("🛑 Agent stopped")
