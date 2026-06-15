"""Position sizing with hard caps.

Enforces two invariants that protect the wallet:
  1. No single token position exceeds `max_position_pct` of equity.
  2. Total risk-asset exposure never pushes the stablecoin reserve below
     `stablecoin_floor_pct` of equity.

`size_for` fails safe (returns 0.0) on any invalid input — it must never emit a
position from a NaN/negative value.
"""
from __future__ import annotations

from .guards import is_bad_number, require_finite_nonneg


class PositionSizer:
    def __init__(self, equity: float, max_position_pct: float, stablecoin_floor_pct: float) -> None:
        self.equity = require_finite_nonneg(equity, "equity")
        self.max_position_pct = max_position_pct
        self.stablecoin_floor_pct = stablecoin_floor_pct

    def max_position_usd(self) -> float:
        return self.equity * self.max_position_pct

    def deployable_usd(self, current_risk_usd: float) -> float:
        """USD still deployable to risk assets without breaching the stable floor."""
        if is_bad_number(current_risk_usd):
            return 0.0
        risk_budget = self.equity * (1.0 - self.stablecoin_floor_pct)
        return max(0.0, risk_budget - current_risk_usd)

    def size_for(self, current_token_usd: float, current_risk_usd: float) -> float:
        """USD to add to a token, respecting per-token and portfolio caps."""
        if is_bad_number(current_token_usd) or is_bad_number(current_risk_usd):
            return 0.0
        per_token_room = max(0.0, self.max_position_usd() - current_token_usd)
        deployable = self.deployable_usd(current_risk_usd)
        return max(0.0, min(per_token_room, deployable))
