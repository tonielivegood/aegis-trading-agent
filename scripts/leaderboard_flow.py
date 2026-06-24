#!/usr/bin/env python
"""Flow-adjusted contest leaderboard — strip DEPOSITS/withdrawals to rank TRADING skill.

The naive leaderboard (now/baseline-1) is fooled by deposits: a $2 wallet funded to $430
shows "+21000%" but earned nothing trading. This reads each wallet's Moralis tx history,
nets out external transfers (deposits +, withdrawals -, SWAPS excluded — they are internal
trades), and ranks by trading P&L instead. Reuses baseline/now from leaderboard.json.

  trading_pnl = now - baseline - net_external_flow
  adj_return  = trading_pnl / (baseline + max(0, net_external_flow))   # return on capital used

Read-only. Validates on two known wallets first (ours: net flow must be ~$0; the naive #1:
a large deposit). Output: flow-adjusted board + our rank + a suggested safe_return.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ENV = dotenv_values(ROOT / ".env")
H = {"X-API-Key": ENV.get("MORALIS_API_KEY"), "accept": "application/json"}
B = "https://deep-index.moralis.io/api/v2.2"
OURS = (ENV.get("AGENT_WALLET_ADDRESS") or "").lower()
START_BLOCK = 105_617_728   # 22/6 00:00 UTC (Moralis dateToBlock)
STABLES = {"USDT", "BSC-USD", "USDC", "DAI", "USD1", "USDD", "FDUSD", "TUSD", "USDE", "BUSD"}
BNB_USD = 600.0             # deposits are mostly stablecoin; BNB precision matters little


def _get(url: str, params: dict) -> dict:
    backoff = 0.5
    for _ in range(8):
        r = requests.get(url, params=params, headers=H, timeout=45)
        if r.status_code == 429:
            time.sleep(backoff); backoff = min(8.0, backoff * 2); continue
        r.raise_for_status(); return r.json()
    return {}


def net_external_flow(addr: str) -> float:
    """USD of non-swap inbound transfers minus outbound, since the contest start.
    Swaps are internal trades → excluded. Dedup by (tx, log_index) kills the
    USDT/BSC-USD double-listing of the same Binance-Peg transfer."""
    flow = 0.0
    seen: set = set()
    cursor = None
    while True:
        params = {"chain": "bsc", "from_block": START_BLOCK, "order": "ASC", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        j = _get(f"{B}/wallets/{addr}/history", params)
        for t in j.get("result", []):
            if t.get("category") == "token swap":
                continue
            txh = t.get("hash")
            for tr in t.get("erc20_transfers", []):
                k = (txh, tr.get("log_index"))
                if k in seen:
                    continue
                seen.add(k)
                if (tr.get("token_symbol") or "").upper() in STABLES:
                    val = float(tr.get("value_formatted") or 0)
                    flow += val if tr.get("direction") == "receive" else -val
            for i, nt in enumerate(t.get("native_transfers", [])):
                k = (txh, "native", i)
                if k in seen:
                    continue
                seen.add(k)
                val = float(nt.get("value_formatted") or 0) * BNB_USD
                flow += val if nt.get("direction") == "receive" else -val
        cursor = j.get("cursor")
        if not cursor:
            break
        time.sleep(0.05)
    return flow


def main() -> None:
    # self-validation on two known wallets
    print("validation:", flush=True)
    print(f"  OURS net flow = ${net_external_flow(OURS):.2f}  (expect ~0)", flush=True)
    nf1 = net_external_flow("0x01AdAf3C554cDeBf7235d460583f0a91bBbd6282")
    print(f"  NAIVE#1 net flow = ${nf1:.2f}  (expect a large deposit)", flush=True)

    lb = json.loads((ROOT / "data" / "runtime" / "leaderboard.json").read_text())
    rows = []
    for i, r in enumerate(lb["ranked"], 1):
        w = r["wallet"]
        try:
            nf = net_external_flow(w)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] {w} ERR {str(e)[:50]}", flush=True); continue
        base, now = r["baseline_usd"], r["now_usd"]
        pnl = now - base - nf
        denom = base + max(0.0, nf)
        adj = (pnl / denom) if denom > 1 else None
        rows.append({"wallet": w, "baseline": base, "now": now, "net_flow": round(nf, 2),
                     "trading_pnl": round(pnl, 2),
                     "adj_return_pct": round(adj * 100, 2) if adj is not None else None})
        if i % 20 == 0:
            print(f"  flow {i}/{len(lb['ranked'])}", flush=True)
        time.sleep(0.05)

    ranked = sorted([r for r in rows if r["adj_return_pct"] is not None and r["baseline"] + max(0, r["net_flow"]) >= 10],
                    key=lambda r: r["adj_return_pct"], reverse=True)
    for n, r in enumerate(ranked, 1):
        r["rank"] = n
    (ROOT / "data" / "runtime" / "leaderboard_flow.json").write_text(json.dumps(ranked, indent=2))

    print("\n========= FLOW-ADJUSTED LEADERBOARD (trading skill, capital>=$10) =========", flush=True)
    print(f"{'#':>3} {'base$':>8} {'now$':>8} {'netflow$':>9} {'pnl$':>8} {'adjRet%':>9}")
    for r in ranked[:12]:
        star = " <== US" if r["wallet"].lower() == OURS else ""
        print(f"{r['rank']:>3} {r['baseline']:>8} {r['now']:>8} {r['net_flow']:>9} "
              f"{r['trading_pnl']:>8} {r['adj_return_pct']:>9}{star}", flush=True)
    me = next((r for r in ranked if r["wallet"].lower() == OURS), None)
    if me:
        print(f"\n>>> OUR FLOW-ADJUSTED RANK: {me['rank']}/{len(ranked)}  (adj_return {me['adj_return_pct']}%)", flush=True)
    if len(ranked) >= 5:
        fifth = ranked[4]["adj_return_pct"]
        print(f">>> 5th-place real return ≈ {fifth}% → suggested safe_return ≈ {round(fifth*1.1,1)}% (5th + 10% buffer)", flush=True)


if __name__ == "__main__":
    main()
