#!/usr/bin/env python
"""Read-only contest-leaderboard recon (Track-1 BNB Hack).

Phase A tool — NOT in the live bot path, 100% read-only (no signing, no risk).
Estimates the live leaderboard so we know our rank among the registered agents,
which feeds the tournament-clock risk posture (#3).

Pipeline (all via Moralis EVM API + the verified registry contract):
  1. ENUMERATE  — every wallet that successfully called register() on the registry
                  (the contract has no participant array; we read its inbound txs).
  2. VALUE      — each wallet's in-USD token balances NOW and at the contest-start
                  block (22/6 00:00 UTC) → estimated return %.
  3. RANK       — sort by return, locate our wallet, print the board.

Caveats (intentional, good-enough for rank estimation, NOT the official score):
  * Moralis USD pricing != the contest's CMC pricing, and we value all non-spam
    tokens (not strictly the 149 in-scope) — a small, *consistent* bias across
    wallets, so the ORDERING is robust even if absolute returns drift ~0.5%.
  * Registration is open until 25/6 00:00 UTC, so the field can still grow — re-run.
  * Wallets with a <= $1 baseline are unranked (matches the contest's sub-$1 = 0% rule).

Usage:
    python scripts/leaderboard_recon.py
    python scripts/leaderboard_recon.py --start 2026-06-22T00:00:00Z
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console safety

import requests
from dotenv import dotenv_values
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
ENV = dotenv_values(ROOT / ".env")
KEY = ENV.get("MORALIS_API_KEY")
CONTRACT = "0x212c61b9b72c95d95bf29cf032f5e5635629aed5"
OURS = Web3.to_checksum_address(ENV.get("AGENT_WALLET_ADDRESS") or "0x" + "0" * 40)
REGISTER_SELECTOR = "0x" + Web3.keccak(text="register()").hex().lstrip("0x")[:8]
RUNTIME = ROOT / "data" / "runtime"
B = "https://deep-index.moralis.io/api/v2.2"
H = {"X-API-Key": KEY, "accept": "application/json"}
THROTTLE = 0.10


def _get(url: str, params: dict) -> dict:
    """GET with light retry on rate limit (429)."""
    backoff = 0.5
    for _ in range(8):
        r = requests.get(url, params=params, headers=H, timeout=45)
        if r.status_code == 429:
            time.sleep(backoff)
            backoff = min(8.0, backoff * 2)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def date_to_block(date_iso: str) -> int:
    return int(_get(f"{B}/dateToBlock", {"chain": "bsc", "date": date_iso})["block"])


def enumerate_participants() -> dict[str, int]:
    """{wallet: registration_block} for every successful register() call."""
    parts: dict[str, int] = {}
    cursor = None
    while True:
        params = {"chain": "bsc", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        j = _get(f"{B}/{CONTRACT}", params)
        for t in j.get("result", []):
            if (t.get("input", "")[:10].lower() == REGISTER_SELECTOR
                    and (t.get("to_address") or "").lower() == CONTRACT.lower()
                    and str(t.get("receipt_status")) in ("1", "None")):
                parts[Web3.to_checksum_address(t["from_address"])] = int(t["block_number"])
        cursor = j.get("cursor")
        if not cursor:
            break
        time.sleep(THROTTLE)
    return parts


def wallet_equity_usd(addr: str, to_block: int | None = None) -> float:
    """Sum of non-spam token USD value for a wallet (optionally at a past block)."""
    total = 0.0
    cursor = None
    while True:
        params = {"chain": "bsc", "exclude_spam": "true"}
        if to_block:
            params["to_block"] = to_block
        if cursor:
            params["cursor"] = cursor
        j = _get(f"{B}/wallets/{addr}/tokens", params)
        for tok in j.get("result", []):
            usd = tok.get("usd_value")
            if usd:
                total += float(usd)
        cursor = j.get("cursor")
        if not cursor:
            break
        time.sleep(THROTTLE)
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-06-22T00:00:00Z", help="contest-start UTC")
    args = ap.parse_args()

    print(f"register() selector: {REGISTER_SELECTOR}", flush=True)
    parts = enumerate_participants()
    print(f"TOTAL REGISTERED PARTICIPANTS: {len(parts)}  | our wallet in: {OURS in parts}", flush=True)

    start_block = date_to_block(args.start)
    print(f"contest-start {args.start} -> block {start_block}\nvaluing {len(parts)} wallets "
          f"(now + baseline)...", flush=True)

    rows = []
    for i, addr in enumerate(sorted(parts), 1):
        try:
            now_usd = wallet_equity_usd(addr)
            base_usd = wallet_equity_usd(addr, to_block=start_block)
        except Exception as e:  # noqa: BLE001 — skip a flaky wallet, keep the board
            print(f"  [{i}/{len(parts)}] {addr} ERR {str(e)[:60]}", flush=True)
            continue
        ret = (now_usd / base_usd - 1.0) if base_usd > 1.0 else None
        rows.append({"wallet": addr, "baseline_usd": round(base_usd, 2),
                     "now_usd": round(now_usd, 2),
                     "return_pct": round(ret * 100, 2) if ret is not None else None})
        if i % 20 == 0:
            print(f"  valued {i}/{len(parts)}", flush=True)
        time.sleep(THROTTLE)

    ranked = sorted([r for r in rows if r["return_pct"] is not None],
                    key=lambda r: r["return_pct"], reverse=True)
    unranked = [r for r in rows if r["return_pct"] is None]
    for n, r in enumerate(ranked, 1):
        r["rank"] = n

    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "start": args.start, "total_registered": len(parts),
           "ranked": ranked, "unranked_or_sub1": len(unranked)}
    (RUNTIME / "leaderboard.json").write_text(json.dumps(out, indent=2))

    print("\n================ LIVE LEADERBOARD (est.) ================", flush=True)
    print(f"{len(parts)} registered | {len(ranked)} ranked | {len(unranked)} sub-$1/unranked\n")
    print(f"{'#':>3}  {'wallet':42}  {'base$':>8}  {'now$':>8}  {'ret%':>7}")
    ours_row = next((r for r in ranked if r["wallet"] == OURS), None)
    show = ranked[:10]
    if ours_row and ours_row not in show:
        show = ranked[:10] + ["...","neighbors..."]  # placeholder, replaced below
    for r in ranked[:10]:
        star = "  <== US" if r["wallet"] == OURS else ""
        print(f"{r['rank']:>3}  {r['wallet']:42}  {r['baseline_usd']:>8}  {r['now_usd']:>8}  {r['return_pct']:>7}{star}")
    if ours_row:
        lo = max(0, ours_row["rank"] - 3); hi = ours_row["rank"] + 2
        if ours_row["rank"] > 10:
            print("   ...")
            for r in ranked[lo:hi]:
                star = "  <== US" if r["wallet"] == OURS else ""
                print(f"{r['rank']:>3}  {r['wallet']:42}  {r['baseline_usd']:>8}  {r['now_usd']:>8}  {r['return_pct']:>7}{star}")
        print(f"\n>>> OUR RANK: {ours_row['rank']} / {len(ranked)}  "
              f"(return {ours_row['return_pct']:+}% | known-true ~+4.5% → sanity check)")
    else:
        print("\n>>> our wallet not in ranked set (check baseline > $1)")
    print(f"\nsaved -> {(RUNTIME / 'leaderboard.json').relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
