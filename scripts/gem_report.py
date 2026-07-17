"""Gem-hunt scorecard from self-collected data (BscScan is dead — this replaces it).

    .venv/bin/python scripts/gem_report.py            # both sections
    .venv/bin/python scripts/gem_report.py --days 7   # signal lookback window

Section 1 — WALLET HOLD-TIMES (wallet_events.jsonl): who scalps, who holds.
Section 2 — SIGNAL OUTCOMES (signals.jsonl + closed_trades.jsonl backfill):
            for every cluster signal, the max price multiple the token reached
            afterwards (GeckoTerminal hourly OHLCV). Answers THE question:
            do our signals ever point at a 2x/5x gem, and are we exiting too early?
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from src.agent.copy_trade.prices import get_pair_stats

ROOT = Path(__file__).resolve().parents[1]
EVENTS_PATH = ROOT / "data" / "copy_trade" / "wallet_events.jsonl"
SIGNALS_PATH = ROOT / "data" / "copy_trade" / "signals.jsonl"
JOURNAL_PATH = ROOT / "data" / "copy_trade" / "closed_trades.jsonl"
_GT_OHLCV = "https://api.geckoterminal.com/api/v2/networks/bsc/pools/{pool}/ohlcv/hour"

SCALPER_MAX_S = 900          # median hold < 15 min
SWING_MAX_S = 86400          # < 24 h


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


# ---------- pure logic (tested) ----------

def match_hold_times(rows: list[dict]) -> dict[str, list[float]]:
    """wallet -> observed hold durations (s). First 'in' opens a cycle per
    (wallet, token); the next 'out' closes it. Top-ups while holding are ignored
    (median estimate, not accounting). 'out' without 'in' (pre-existing bags) skipped."""
    open_since: dict[tuple[str, str], float] = {}
    holds: dict[str, list[float]] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        key = (r["wallet"], r["token_address"])
        t = _ts(r["ts"])
        if r["direction"] == "in":
            open_since.setdefault(key, t)
        elif key in open_since:
            holds.setdefault(r["wallet"], []).append(t - open_since.pop(key))
    return holds


def classify(median_s: float) -> str:
    if median_s < SCALPER_MAX_S:
        return "SCALPER"
    if median_s < SWING_MAX_S:
        return "SWING"
    return "HOLDER"


def _max_high_since(ohlcv: list[list], since_ts: float) -> float | None:
    """Max candle-high after since_ts.
    ohlcv rows: [ts, open, high, low, close, volume] (GeckoTerminal shape)."""
    highs = [row[2] for row in ohlcv if row[0] >= since_ts]
    return max(highs) if highs else None


def max_multiple(ohlcv: list[list], since_ts: float, base_price: float) -> float | None:
    """Max candle-high after since_ts, as a multiple of base_price.
    ohlcv rows: [ts, open, high, low, close, volume] (GeckoTerminal shape)."""
    if not base_price:
        return None
    mx = _max_high_since(ohlcv, since_ts)
    return mx / base_price if mx else None


# ---------- thin network layer ----------

def fetch_max_price_since(token_address: str, since_ts: float) -> float | None:
    """Best pool's hourly candles (up to 7d back). None on any failure."""
    stats = get_pair_stats(token_address)
    if stats is None or not stats.get("pair_address"):
        return None
    try:
        r = requests.get(_GT_OHLCV.format(pool=stats["pair_address"]),
                         params={"aggregate": 1, "limit": 168}, timeout=20)
        r.raise_for_status()
        ohlcv = r.json()["data"]["attributes"]["ohlcv_list"]
    except Exception:  # noqa: BLE001
        return None
    return _max_high_since(ohlcv, since_ts)


# ---------- report ----------

def wallet_section() -> None:
    rows = _read_jsonl(EVENTS_PATH)
    holds = match_hold_times(rows)
    print(f"\n{'=' * 72}\n  WALLET HOLD-TIMES  ({len(rows)} events, "
          f"{len(holds)} wallets with >=1 round-trip)\n{'=' * 72}")
    if not holds:
        print("  no matched round-trips yet — let the bot collect for a few days")
        return
    print(f"  {'wallet':<44} {'trips':>5} {'median':>10}  class")
    for w, hs in sorted(holds.items(), key=lambda kv: statistics.median(kv[1])):
        med = statistics.median(hs)
        nice = (f"{med:.0f}s" if med < 120 else
                f"{med / 60:.0f}m" if med < 7200 else
                f"{med / 3600:.1f}h" if med < 172800 else f"{med / 86400:.1f}d")
        print(f"  {w:<44} {len(hs):>5} {nice:>10}  {classify(med)}")


def signal_section(days: float) -> None:
    horizon = time.time() - days * 86400
    seen: dict[str, dict] = {}
    for r in _read_jsonl(SIGNALS_PATH):          # one row per decision
        if _ts(r["ts"]) >= horizon and r["token_address"] not in seen:
            seen[r["token_address"]] = {"ts": _ts(r["ts"]), "price": r.get("price_usd"),
                                        "symbol": r.get("token_symbol", "?"),
                                        "decision": r["decision"]}
    for r in _read_jsonl(JOURNAL_PATH):          # backfill: the real trades
        t = r["token_address"]
        if _ts(r["opened_at"]) >= horizon and t not in seen:
            seen[t] = {"ts": _ts(r["opened_at"]), "price": r.get("entry_price_usd"),
                       "symbol": r.get("token_symbol", "?"),
                       "decision": f"traded({r.get('reason')}, {r.get('pnl_usd')}$)"}
    print(f"\n{'=' * 72}\n  SIGNAL OUTCOMES — last {days:g} days, "
          f"{len(seen)} unique tokens\n{'=' * 72}")
    if not seen:
        print("  no signals recorded yet")
        return
    scored = []
    print(f"  {'token':<8} {'when':<17} {'sig price':>12} {'max since':>12} "
          f"{'mult':>6}  decision")
    for token, s in sorted(seen.items(), key=lambda kv: kv[1]["ts"]):
        mx = fetch_max_price_since(token, s["ts"])
        mult = (mx / s["price"]) if (mx and s["price"]) else None
        if mult:
            scored.append(mult)
        when = datetime.fromtimestamp(s["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        print(f"  {s['symbol']:<8} {when:<17} "
              f"{s['price'] if s['price'] else '?':>12} "
              f"{mx if mx else '?':>12} "
              f"{f'{mult:.1f}x' if mult else '?':>6}  {s['decision']}")
        time.sleep(0.5)                          # GeckoTerminal free-tier politeness
    if scored:
        n = len(scored)
        print(f"\n  scored {n}: "
              f">=2x: {sum(m >= 2 for m in scored)}/{n}   "
              f">=5x: {sum(m >= 5 for m in scored)}/{n}   "
              f">=10x: {sum(m >= 10 for m in scored)}/{n}")
        print("  (>=5x hit-rate ~1/8 or better = the wallet list can feed the "
              "trail exit; worse = rebuild the list from Section 1's HOLDERs)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Gem-hunt scorecard (self-built data)")
    ap.add_argument("--days", type=float, default=7.0)
    args = ap.parse_args()
    wallet_section()
    signal_section(args.days)


if __name__ == "__main__":
    main()
