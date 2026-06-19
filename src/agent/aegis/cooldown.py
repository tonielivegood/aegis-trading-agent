"""Per-token re-entry cooldown — anti-whipsaw memory.

After the sniper exits a token, the same token often keeps printing volume spikes
for a while; re-entering immediately means getting chopped on the same fader. The
cooldown book records each exit time and reports which symbols are still "cooling
down" so the entry stage can skip them. Tiny JSON state, persisted across restarts.
Pure (time is injected); no chain/network access.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CooldownBook:
    last_exit: dict[str, float] = field(default_factory=dict)

    def record_exit(self, symbol: str, now: float) -> None:
        self.last_exit[symbol] = now

    def cooling_down(self, *, now: float, cooldown_s: float) -> set[str]:
        """Symbols whose most recent exit is still within the cooldown window."""
        return {s for s, t in self.last_exit.items() if now - t < cooldown_s}

    def prune(self, *, now: float, cooldown_s: float) -> None:
        """Drop entries past their cooldown to keep the state bounded."""
        self.last_exit = {s: t for s, t in self.last_exit.items() if now - t < cooldown_s}

    # --- persistence ---
    @classmethod
    def load(cls, path: Path) -> CooldownBook:
        if not path.exists():
            return cls()
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls({s: float(t) for s, t in (d or {}).items()})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.last_exit), encoding="utf-8")
