"""Drawdown circuit breaker — the contest disqualification guard.

Tracks rolling peak equity and the drawdown from that peak. The breaker LATCHES:
once tripped it stays tripped for the session, so a small bounce can't silently
re-enable risk-taking. `alert` is our internal stop (e.g. 0.20); `cap` is the
contest disqualification threshold (e.g. 0.30) we must never reach.
"""
from __future__ import annotations

from .guards import require_finite_nonneg


class DrawdownTracker:
    def __init__(self, alert: float = 0.20, cap: float = 0.30) -> None:
        self.alert = alert
        self.cap = cap
        self.peak = 0.0
        self._current = 0.0
        self._tripped = False

    def update(self, equity: float) -> None:
        """Record the latest equity snapshot. Raises on invalid input."""
        equity = require_finite_nonneg(equity, "equity")
        self._current = equity
        if equity > self.peak:
            self.peak = equity
        if self.current_drawdown() >= self.alert:
            self._tripped = True  # latch

    def current_drawdown(self) -> float:
        if self.peak <= 0:
            return 0.0
        return (self.peak - self._current) / self.peak

    def breaker_tripped(self) -> bool:
        """True if the internal alert stop has been hit (latched)."""
        return self._tripped or self.current_drawdown() >= self.alert

    def cap_breached(self) -> bool:
        """True if the hard contest cap has been reached — emergency."""
        return self.current_drawdown() >= self.cap

    def reset(self) -> None:
        """Clear the latch (e.g. at the start of a new trading session)."""
        self._tripped = False
