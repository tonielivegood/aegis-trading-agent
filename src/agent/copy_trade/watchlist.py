"""Phase-2 stakeout dossiers: a signal ARMS a token, the monitor FILMS it (one
sample per tick), entry logic (config-gated, later task) reads the film. Films
are append-only JSONL so film_report.py can tune thresholds from real outcomes.
ponytail: RAM-held dossiers — a restart loses active stakeouts but never the
film lines already written; acceptable, stakeouts re-arm on the next buy."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Dossier:
    token_address: str
    armed_at: float
    arm_price: float
    arm_liquidity: float
    armers: list[str]
    samples: list[dict] = field(default_factory=list)
    disarmed: str | None = None


class Watchlist:
    def __init__(self, films_path: Path, max_dossiers: int = 8,
                 max_age_s: float = 6 * 3600) -> None:
        self._path = films_path
        self._max = max_dossiers
        self._max_age = max_age_s
        self._dossiers: dict[str, Dossier] = {}

    def _write(self, row: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def arm(self, token: str, wallet: str, price: float, liquidity: float,
            now: float | None = None) -> bool:
        token = token.lower()
        if token in self._dossiers or len(self._dossiers) >= self._max:
            return False
        now = time.time() if now is None else now
        self._dossiers[token] = Dossier(token_address=token, armed_at=now,
                                        arm_price=price, arm_liquidity=liquidity,
                                        armers=[wallet.lower()])
        self._write({"event": "arm", "token_address": token, "ts": now,
                     "wallet": wallet.lower(), "price": price, "liquidity": liquidity})
        return True

    def note_buy(self, token: str, wallet: str) -> None:
        d = self._dossiers.get(token.lower())
        if d is not None and wallet.lower() not in d.armers:
            d.armers.append(wallet.lower())

    def note_sell(self, token: str, wallet: str, now: float | None = None) -> None:
        d = self._dossiers.get(token.lower())
        if d is not None and wallet.lower() in d.armers:
            self._disarm(d, "armer_sold", time.time() if now is None else now)

    def add_sample(self, token: str, sample: dict) -> None:
        d = self._dossiers.get(token.lower())
        if d is None:
            return
        d.samples.append(sample)
        self._write({"event": "sample", "token_address": d.token_address,
                     **sample})

    def expire(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for d in list(self._dossiers.values()):
            if now - d.armed_at > self._max_age:
                self._disarm(d, "expired", now)

    def disarm(self, token: str, reason: str, now: float | None = None) -> None:
        d = self._dossiers.get(token.lower())
        if d is not None:
            self._disarm(d, reason, time.time() if now is None else now)

    def _disarm(self, d: Dossier, reason: str, now: float) -> None:
        d.disarmed = reason
        self._dossiers.pop(d.token_address, None)
        self._write({"event": "disarm", "token_address": d.token_address,
                     "reason": reason, "ts": now})

    def active(self) -> list[Dossier]:
        return list(self._dossiers.values())

    def get(self, token: str) -> Dossier | None:
        return self._dossiers.get(token.lower())
