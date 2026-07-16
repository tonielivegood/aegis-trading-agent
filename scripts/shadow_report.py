"""Shadow-mode PnL report — the input to the user's go-live decision (v2 spec:
>=10 cluster events, then the human decides; the bot never flips itself live).

Run: python scripts/shadow_report.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.copy_trade.positions import PositionStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "data" / "copy_trade" / "closed_trades.jsonl"
SHADOW = ROOT / "data" / "copy_trade" / "shadow_positions.json"
GO_LIVE_MIN_EVENTS = 10


def summarize(rows: list[dict]) -> dict:
    sim = [r for r in rows if r.get("simulated")]
    if not sim:
        return {"events": 0, "closed": 0, "wins": 0, "win_rate": None,
                "total_pnl_usd": 0.0, "median_pnl_pct": None, "avg_fees_usd": None}
    pnls = [r["pnl_usd"] for r in sim]
    pcts = [r["pnl_pct"] for r in sim if r.get("pnl_pct") is not None]
    wins = sum(1 for p in pnls if p > 0)
    return {"events": len(sim), "closed": len(sim), "wins": wins,
            "win_rate": wins / len(sim),
            "total_pnl_usd": round(sum(pnls), 4),
            "median_pnl_pct": statistics.median(pcts) if pcts else None,
            "avg_fees_usd": round(statistics.mean(
                r.get("fees_model_usd", 0) for r in sim), 4)}


def main() -> None:
    rows = []
    if JOURNAL.exists():
        rows = [json.loads(l) for l in
                JOURNAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    store = PositionStore(SHADOW)
    store.load()
    s = summarize(rows)
    total_events = s["closed"] + len(store.all())

    print("=" * 60)
    print("  SHADOW-MODE REPORT")
    print("=" * 60)
    print(f"  cluster events total : {total_events} "
          f"(closed {s['closed']}, open {len(store.all())})")
    if s["closed"]:
        print(f"  win rate             : {s['win_rate']:.0%}")
        print(f"  total paper PnL      : ${s['total_pnl_usd']}")
        print(f"  median PnL %         : {s['median_pnl_pct']:+.1%}")
        print(f"  avg modeled fees     : ${s['avg_fees_usd']}")
    for r in [r for r in rows if r.get("simulated")]:
        print(f"    {r['token_symbol']:<14} {r['pnl_pct']:+8.1%}  "
              f"${r['pnl_usd']:+7.2f}  exit={r['reason']}")
    for p in store.all():
        print(f"    {p.token_symbol:<14} OPEN  entry=${p.entry_price_usd:.6g}  "
              f"exits={len(p.exited_by)}/{len(p.cluster_wallets)}")
    print()
    if total_events >= GO_LIVE_MIN_EVENTS:
        print(f"  >= {GO_LIVE_MIN_EVENTS} events reached — time for the go-live decision.")
    else:
        print(f"  {GO_LIVE_MIN_EVENTS - total_events} more events until the "
              f"go-live review.")


if __name__ == "__main__":
    main()
