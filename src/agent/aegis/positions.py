"""Open-position book for the event strategy.

The event radar needs per-position memory (entry price/time, peak, scale-out
tiers already taken) to run take-profit tiers, trailing and time stops. This is
a tiny JSON-serialisable store the agent loop can persist under data/runtime/.
No chain access; pure state.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class OpenPosition:
    symbol: str
    contract: str
    entry_price: float
    usd_size: float
    entry_time: float = field(default_factory=lambda: time.time())
    peak_price: float = 0.0
    entry_baseline_vol: float = 0.0  # 5m volume baseline captured at entry (for volume-exit)
    token_class: str = "meme"        # "major" (scalp) | "meme" (ride) — drives exit params

    def __post_init__(self) -> None:
        if self.peak_price <= 0:
            self.peak_price = self.entry_price

    def gain(self, price: float) -> float:
        return (price - self.entry_price) / self.entry_price if self.entry_price > 0 else 0.0

    def age_s(self, now: float | None = None) -> float:
        return (now or time.time()) - self.entry_time


@dataclass
class PositionBook:
    positions: dict[str, OpenPosition] = field(default_factory=dict)

    def is_open(self, symbol: str) -> bool:
        return symbol in self.positions

    def open(self, pos: OpenPosition) -> None:
        self.positions[pos.symbol] = pos

    def close(self, symbol: str) -> None:
        self.positions.pop(symbol, None)

    def update_peak(self, symbol: str, price: float) -> None:
        p = self.positions.get(symbol)
        if p and price > p.peak_price:
            p.peak_price = price

    # --- persistence ---
    def to_dict(self) -> dict:
        return {s: asdict(p) for s, p in self.positions.items()}

    @classmethod
    def from_dict(cls, d: dict) -> PositionBook:
        return cls({s: OpenPosition(**v) for s, v in (d or {}).items()})

    @classmethod
    def load(cls, path: Path) -> PositionBook:
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()), encoding="utf-8")
