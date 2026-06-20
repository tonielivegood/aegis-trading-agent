"""Drawdown circuit breaker — the contest disqualification guard.

Tracks rolling peak equity and the drawdown from that peak. The breaker LATCHES:
once tripped it stays tripped for the session, so a small bounce can't silently
re-enable risk-taking. `alert` is our internal stop (e.g. 0.20); `cap` is the
contest disqualification threshold (e.g. 0.30) we must never reach.

DEBOUNCE: the alert latch only fires after the drawdown stays at/over the
threshold for `latch_ticks` CONSECUTIVE updates. Equity is computed from per-tick
on-chain price reads; a single failed read can momentarily value a held token at
$0 and crater equity. Latching on one such glitch would derisk the agent for the
WHOLE contest (the latch persists across restarts). A real drawdown persists over
several ticks; a glitch does not — so we require a streak before latching. A lone
breach tick resets once equity recovers. (`cap`, the hard DQ line, stays
instantaneous — combined with the valuation last-known-price fallback upstream,
a glitch should never reach it, and we must never be slow approaching DQ.)
"""
from __future__ import annotations

from .guards import require_finite_nonneg


class DrawdownTracker:
    def __init__(self, alert: float = 0.20, cap: float = 0.30, latch_ticks: int = 1) -> None:
        self.alert = alert
        self.cap = cap
        self.latch_ticks = max(1, int(latch_ticks))
        self.peak = 0.0
        self._current = 0.0
        self._tripped = False
        self._breach_streak = 0

    def update(self, equity: float) -> None:
        """Record the latest equity snapshot. Raises on invalid input."""
        equity = require_finite_nonneg(equity, "equity")
        self._current = equity
        if equity > self.peak:
            self.peak = equity
        if self.current_drawdown() >= self.alert:
            self._breach_streak += 1
            if self._breach_streak >= self.latch_ticks:
                self._tripped = True  # latch only after a SUSTAINED breach
        else:
            self._breach_streak = 0   # a lone glitch tick must not latch

    def current_drawdown(self) -> float:
        if self.peak <= 0:
            return 0.0
        return (self.peak - self._current) / self.peak

    def breaker_tripped(self) -> bool:
        """True once the alert stop has LATCHED (after the debounce streak)."""
        return self._tripped

    def cap_breached(self) -> bool:
        """True if the hard contest cap has been reached — emergency (instantaneous)."""
        return self.current_drawdown() >= self.cap

    def reset(self) -> None:
        """Clear the latch (e.g. at the start of a new trading session)."""
        self._tripped = False
        self._breach_streak = 0
