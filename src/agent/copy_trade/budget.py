"""Fixed-slice budget tracker for the copy-trade strategy — pure arithmetic, no I/O.
Persistence of which slices are currently open lives in positions.py; this class only
answers 'is there room for one more slice right now'."""
from __future__ import annotations


class CopyTradeBudget:
    def __init__(self, total_usd: float, slice_usd: float) -> None:
        if total_usd <= 0 or slice_usd <= 0:
            raise ValueError("total_usd and slice_usd must be positive")
        self._slice_usd = slice_usd
        self._available_usd = total_usd

    @property
    def available_usd(self) -> float:
        return self._available_usd

    def can_open_new(self) -> bool:
        return self._available_usd >= self._slice_usd

    def allocate(self) -> float:
        if not self.can_open_new():
            raise RuntimeError(
                f"insufficient budget: {self._available_usd:.4f} < slice {self._slice_usd:.4f}"
            )
        self._available_usd -= self._slice_usd
        return self._slice_usd

    def release(self, amount_usd: float) -> None:
        self._available_usd += amount_usd
