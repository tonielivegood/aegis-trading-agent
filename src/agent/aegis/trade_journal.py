"""Append-only trade journal — one JSON line per executed (real, non-simulated) fill.

Purpose: give the soak-test pass bar (win-rate, per-trade stop discipline) an actual
data source. Pure I/O helpers only — no chain/network, no strategy logic. Every
write is fail-safe from the CALLER's perspective (this module raises on a genuine
disk error, but the caller in agent_loop.py wraps every call so a journal-write
hiccup never breaks a trading tick).
"""
from __future__ import annotations

import json
from pathlib import Path


def record_entry(path: Path, *, symbol: str, token_class: str, entry_price: float,
                 usd_size: float, reason: str, backend: str, tx: str | None,
                 time_iso: str) -> None:
    _append(path, {
        "event": "entry", "time": time_iso, "symbol": symbol, "token_class": token_class,
        "entry_price": entry_price, "usd_size": usd_size, "reason": reason,
        "backend": backend, "tx": tx,
    })


def record_exit(path: Path, *, symbol: str, token_class: str, entry_price: float,
                exit_price: float, usd_size: float, hold_minutes: float, reason: str,
                backend: str, tx: str | None, time_iso: str) -> None:
    pnl_pct = (exit_price / entry_price - 1.0) if entry_price > 0 else 0.0
    pnl_usd = usd_size * pnl_pct
    _append(path, {
        "event": "exit", "time": time_iso, "symbol": symbol, "token_class": token_class,
        "entry_price": entry_price, "exit_price": exit_price, "usd_size": usd_size,
        "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "hold_minutes": hold_minutes,
        "reason": reason, "backend": backend, "tx": tx,
    })


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def read_all(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def report(path: Path) -> dict:
    """Win-rate / PnL summary over every recorded EXIT (entries aren't counted —
    a trade's outcome is only known once it closes)."""
    rows = [r for r in read_all(path) if r.get("event") == "exit"]
    n = len(rows)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "avg_pnl_pct": None,
                "worst_pnl_pct": None, "total_pnl_usd": 0.0}
    wins = [r for r in rows if r.get("pnl_usd", 0.0) > 0]
    pnl_pcts = [r.get("pnl_pct", 0.0) for r in rows]
    return {
        "n_trades": n,
        "win_rate": len(wins) / n,
        "avg_pnl_pct": sum(pnl_pcts) / n,
        "worst_pnl_pct": min(pnl_pcts),
        "total_pnl_usd": sum(r.get("pnl_usd", 0.0) for r in rows),
    }
