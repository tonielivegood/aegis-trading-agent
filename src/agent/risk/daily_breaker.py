"""Daily soft circuit-breaker — bound intraday bleed without ending the contest.

The latched −20% drawdown breaker is the DQ guard, but tripping it = sitting in cash
for the rest of the week (effectively losing). This softer guard caps how much we can
bleed in a SINGLE UTC day: if equity draws down >= `threshold` from the day's OPEN, we
stop opening NEW positions for the rest of that day (exits/stops and the tiny min-trade
compliance still run), then reset at 00:00 UTC.

That lets the meme lottery keep taking many small +3% shots (the convex upside) while
capping the tail when an active-but-choppy day produces a run of fading entries — the
churn-bleed failure mode. It also catches bleed from ANY source (sniper/beta/compliance),
so it doubles as a simple robustness valve for an unattended 7-day run.

Pure + JSON-serialisable; the agent loop persists it under data/runtime/.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DailyBreaker:
    date: str = ""           # UTC date (YYYY-MM-DD) the open_equity belongs to
    open_equity: float = 0.0  # equity at the first tick of `date`

    def roll(self, equity: float, utc_date: str) -> None:
        """Call once per tick. On a new UTC day, re-anchor the day-open equity."""
        if utc_date != self.date:
            self.date = utc_date
            self.open_equity = max(0.0, equity)

    def drawdown(self, equity: float) -> float:
        """Intraday drawdown from the day-open (0 if no/!positive open)."""
        if self.open_equity <= 0:
            return 0.0
        return (self.open_equity - equity) / self.open_equity

    def should_halt_new(self, equity: float, threshold: float) -> bool:
        """True once the intraday drawdown reaches the threshold (0 disables)."""
        return threshold > 0 and self.drawdown(equity) >= threshold

    # --- persistence ---
    def to_dict(self) -> dict:
        return {"date": self.date, "open_equity": self.open_equity}

    @classmethod
    def load(cls, path: Path) -> DailyBreaker:
        if not path.exists():
            return cls()
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return cls(date=str(d.get("date", "")), open_equity=float(d.get("open_equity", 0.0)))
        except Exception:  # noqa: BLE001 — a corrupt file must never break a tick
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()), encoding="utf-8")
