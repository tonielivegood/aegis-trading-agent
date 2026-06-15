"""Minimum-trade-count compliance tracker.

The contest requires non-zero trading activity; idle hours hurt. This tracks
trade timestamps and answers "do we need to place a trade now?". State persists
to a small JSON ledger so the count survives restarts during the live window.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _as_utc(dt: datetime) -> datetime:
    """Normalize to timezone-aware UTC; assume naive timestamps are already UTC.

    Prevents naive/aware mixing from raising TypeError during live operation.
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class TradeCounter:
    def __init__(self, timestamps: list[datetime] | None = None) -> None:
        self.timestamps: list[datetime] = [_as_utc(t) for t in (timestamps or [])]

    def record_trade(self, ts: datetime) -> None:
        self.timestamps.append(_as_utc(ts))

    def last_trade_time(self) -> datetime | None:
        return max(self.timestamps) if self.timestamps else None

    def needs_trade(self, now: datetime, interval_h: int) -> bool:
        """True if no trade has happened within the last `interval_h` hours."""
        last = self.last_trade_time()
        if last is None:
            return True
        return (_as_utc(now) - last) >= timedelta(hours=interval_h)

    def trades_in_last_24h(self, now: datetime) -> int:
        cutoff = _as_utc(now) - timedelta(hours=24)
        return sum(1 for t in self.timestamps if t > cutoff)

    # --- persistence ---
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([t.isoformat() for t in self.timestamps]), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "TradeCounter":
        if not path.exists():
            return cls(timestamps=[])
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = [datetime.fromisoformat(s) for s in raw]
        return cls(timestamps=ts)


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
