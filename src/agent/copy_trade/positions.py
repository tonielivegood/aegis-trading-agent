"""Disk-backed copy-trade position store. Every mutation writes to disk synchronously
before returning, so a process restart (crash, deploy, VPS reboot) can always recover
open positions by reloading this file — the exact property the RAM-only
`token_list._discovered` registry lacked, which orphaned two real positions for 9 days
(see docs/superpowers/specs/2026-07-15-copy-trade-gem-hunter-design.md)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class CopyPosition:
    token_symbol: str
    token_address: str
    token_decimals: int
    source_wallet: str
    usd_size: float
    token_amount: float
    opened_at: str


class PositionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._positions: list[CopyPosition] = []

    def load(self) -> None:
        if not self._path.exists():
            self._positions = []
            return
        raw = json.loads(self._path.read_text(encoding="utf-8") or "[]")
        self._positions = [CopyPosition(**p) for p in raw]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([asdict(p) for p in self._positions], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def open_position(self, pos: CopyPosition) -> None:
        self._positions.append(pos)
        self._save()

    def close_position(self, token_address: str, source_wallet: str) -> CopyPosition | None:
        pos = self.find(token_address, source_wallet)
        if pos is None:
            return None
        self._positions.remove(pos)
        self._save()
        return pos

    def find(self, token_address: str, source_wallet: str) -> CopyPosition | None:
        for p in self._positions:
            if p.token_address.lower() == token_address.lower() and p.source_wallet.lower() == source_wallet.lower():
                return p
        return None

    def all(self) -> list[CopyPosition]:
        return list(self._positions)
