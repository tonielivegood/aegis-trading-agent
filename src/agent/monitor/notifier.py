"""Telegram push alerts — outbound, best-effort, send-only.

Sends operational alerts (breaker trips, trades, errors, hourly heartbeat) to a
Telegram chat. Disabled automatically if no bot token/chat id is configured.

Security: token is a secret (never logged); messages never include secrets; send
failures are swallowed so a notification problem can never break trading. We only
SEND — we never read/act on Telegram messages, so it is not an injection surface.
"""
from __future__ import annotations

import json
import urllib.request

from ..config import settings
from .logger import get_logger

log = get_logger(__name__)


def is_enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def send(text: str) -> bool:
    """Send a message. Returns True on success, False if disabled or on failure.
    Never raises."""
    if not is_enabled():
        return False
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        data = json.dumps({"chat_id": settings.telegram_chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        return getattr(resp, "status", 200) == 200
    except Exception:  # noqa: BLE001 — alerts are best-effort; never break the agent
        log.warning("telegram_send_failed")
        return False


# --- message formatters (no secrets, ever) ---

def format_startup(equity: float, dry_run: bool) -> str:
    mode = "DRY-RUN" if dry_run else "LIVE"
    return f"🤖 Agent started [{mode}] · equity ${equity:.2f}"


def format_heartbeat(equity: float, drawdown: float, cumulative_return: float) -> str:
    return (f"💓 Heartbeat · equity ${equity:.2f} · "
            f"DD {drawdown * 100:.1f}% · return {cumulative_return * 100:+.1f}%")


def format_breaker(equity: float, drawdown: float) -> str:
    return f"⚠️ BREAKER/derisk · drawdown {drawdown * 100:.1f}% · moving to stablecoin · equity ${equity:.2f}"


def format_trades(n: int, equity: float) -> str:
    return f"✅ Executed {n} trade(s) · equity ${equity:.2f}"


def format_error(detail: str) -> str:
    return f"🔴 Agent error: {detail[:300]}"
