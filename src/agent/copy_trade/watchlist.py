"""Phase-2 stakeout dossiers: a signal ARMS a token, the monitor FILMS it (one
sample per tick), entry logic (config-gated, later task) reads the film. Films
are append-only JSONL so film_report.py can tune thresholds from real outcomes.
ponytail: RAM-held dossiers — a restart loses active stakeouts but never the
film lines already written; acceptable, stakeouts re-arm on the next buy."""
from __future__ import annotations

import json
import statistics
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


def phase2_score(d: Dossier, cfg: dict, voting: set[str]) -> tuple[bool, str]:
    """All six film fingerprints green + enough film + >=2 voting armers + price
    in band. Returns (ok, reason) — reason names the FIRST failing check, checks
    run in order and short-circuit (no point scoring a film that's too short)."""
    if len([a for a in d.armers if a in voting]) < 2:
        return False, "need_2_voting_armers"
    if len(d.samples) < cfg.get("phase2_min_samples", 15):
        return False, "film_too_short"

    window = d.samples[-30:]

    prices = [s["price"] for s in window if s["price"]]
    if not prices or max(prices) / min(prices) > cfg.get("phase2_base_ratio_max", 1.35):
        return False, "no_base"

    holders = [s["holders"] for s in window if s["holders"] is not None]
    if not holders:
        return False, "holders_unknown"
    if holders[-1] < holders[0] * (1 + cfg.get("phase2_holder_growth_min_pct", 0.05)):
        return False, "holders_flat"

    if d.samples[-1]["liq"] < 0.9 * d.arm_liquidity:
        return False, "liq_draining"

    conc = next((s for s in reversed(window)
                if s["top_pct"] is not None and s["top5_pct"] is not None), None)
    if conc is None:
        return False, "holders_unknown"
    if (conc["top_pct"] > cfg.get("max_single_holder_pct", 0.15)
            or conc["top5_pct"] > cfg.get("max_top5_holder_pct", 0.40)):
        return False, "whale_risk"

    last_price = d.samples[-1]["price"]
    recent_prices = [s["price"] for s in d.samples[-15:] if s["price"]]
    median_price = statistics.median(recent_prices) if recent_prices else last_price
    if (last_price > cfg.get("phase2_entry_band", 1.15) * median_price
            or last_price > cfg.get("phase2_max_vs_arm", 1.25) * d.arm_price):
        return False, "chasing"

    return True, ""
