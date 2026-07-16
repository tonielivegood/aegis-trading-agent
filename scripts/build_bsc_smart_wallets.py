"""Build data/copy_trade/wallets.json — the self-built 50-wallet BSC smart-money list
(Part 1 of docs/superpowers/specs/2026-07-16-cluster-signal-filter-design.md).

Manual, LOCAL run (gmgn-cli is only configured on the dev machine, never the VPS):

    python scripts/build_bsc_smart_wallets.py --winners 0xtokA 0xtokB ... [--dry-run]

Two candidate sources, merged and scored by wallet_discovery:
  1. gmgn-cli recent smart-money trades (maker frequency = BSC activity signal)
  2. early buyers shared across >=2 of the hand-picked winner tokens (our own edge)
Prints the scored table for user review; --dry-run skips writing the file.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.copy_trade.rpc_pool import (  # noqa: E402
    DEFAULT_ENDPOINTS, TRANSFER_TOPIC, RpcPool,
)
from src.agent.copy_trade.wallet_discovery import (  # noqa: E402
    build_ranked_list, cross_winner_candidates, early_buyers, passes_filters,
    score_candidate, wallet_activity,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "copy_trade" / "wallets.json"
ZERO = "0x0000000000000000000000000000000000000000"
EARLY_WINDOW_BLOCKS = 4 * 60 * 60 // 3   # ~4h of BSC blocks after pair creation


def gmgn_maker_counts(trades: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in trades:
        addr = (t.get("maker") or "").lower()
        if addr:
            counts[addr] = counts.get(addr, 0) + 1
    return counts


def assemble_candidates(gmgn_counts: dict[str, int],
                        early_counts: dict[str, int]) -> list[dict]:
    out = []
    for addr in set(gmgn_counts) | set(early_counts):
        sources = ([s for s, hit in (("gmgn", addr in gmgn_counts),
                                     ("early_buyer", addr in early_counts)) if hit])
        out.append({
            "address": addr,
            "sources": sources,
            "score": score_candidate(wins_early=early_counts.get(addr, 0),
                                     gmgn_hits=gmgn_counts.get(addr, 0),
                                     in_both=len(sources) == 2),
        })
    return out


def fetch_gmgn_trades(limit: int = 500) -> list[dict]:
    gmgn_cli = shutil.which("gmgn-cli") or "gmgn-cli"
    proc = subprocess.run(
        [gmgn_cli, "track", "smartmoney", "--chain", "bsc",
         "--limit", str(limit), "--raw"],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        print(f"gmgn-cli failed: {proc.stderr.strip()}", file=sys.stderr)
        return []
    try:
        return json.loads(proc.stdout).get("list", [])
    except json.JSONDecodeError as e:
        print(f"gmgn-cli returned malformed JSON: {e} — skipping", file=sys.stderr)
        return []


def dexscreener_pair(token_address: str) -> dict | None:
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
                     timeout=20)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "bsc"]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def block_at_timestamp(pool: RpcPool, ts: int) -> int:
    """Binary-search the first block at/after unix ts (public RPC has no direct API)."""
    lo, hi = 1, pool.latest_block()
    while lo < hi:
        mid = (lo + hi) // 2
        blk = pool.call("eth_getBlockByNumber", [hex(mid), False])
        if int(blk["timestamp"], 16) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def scan_winner(pool: RpcPool, token_address: str) -> list[str]:
    try:
        pair = dexscreener_pair(token_address)
        if pair is None or not pair.get("pairCreatedAt"):
            print(f"  !! no BSC pair found for {token_address} — skipping")
            return []
        created_ts = int(pair["pairCreatedAt"]) // 1000
        start = block_at_timestamp(pool, created_ts)
        logs = pool.get_logs_chunked(start, start + EARLY_WINDOW_BLOCKS,
                                     topics=[TRANSFER_TOPIC], address=token_address)
        exclude = {pair["pairAddress"].lower(), token_address.lower(), ZERO}
        buyers = early_buyers(logs, exclude=exclude)
        print(f"  {pair['baseToken']['symbol']}: {len(logs)} transfers, "
              f"{len(buyers)} early buyers")
        return buyers
    except Exception as e:
        print(f"  !! error scanning {token_address}: {e} — skipping")
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--winners", nargs="+", required=True,
                    help="5-10 hand-picked recent BSC winner token addresses")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    env = dotenv_values(ROOT / ".env")
    bscscan_key = env.get("BSCSCAN_API_KEY", "")
    pool = RpcPool(DEFAULT_ENDPOINTS)
    now = int(time.time())

    print("== source 1: gmgn-cli recent smart-money trades ==")
    gmgn_counts = gmgn_maker_counts(fetch_gmgn_trades())
    print(f"  {len(gmgn_counts)} distinct makers")

    print("== source 2: early buyers across winner tokens ==")
    buyers_by_token = {t: scan_winner(pool, t) for t in args.winners}
    early_counts = cross_winner_candidates(buyers_by_token, min_tokens=2)
    print(f"  {len(early_counts)} wallets early in >=2 winners")

    candidates = assemble_candidates(gmgn_counts, early_counts)
    candidates.sort(key=lambda c: c["score"], reverse=True)

    print(f"== filtering top candidates (contract/bot/cold checks, need {args.top}) ==")
    kept: list[dict] = []
    for c in candidates:
        if len(kept) >= args.top:
            break
        act = wallet_activity(bscscan_key, c["address"], now)
        time.sleep(0.25)   # BscScan free tier: 5 req/s
        if act is None:
            print(f"  skip {c['address'][:12]}… activity lookup failed")
            continue
        ok, reason = passes_filters(act, pool.get_code(c["address"]))
        if not ok:
            print(f"  drop {c['address'][:12]}… ({reason})")
            continue
        kept.append(c)

    ranked = build_ranked_list(kept, top_n=args.top)
    added_at = datetime.now(timezone.utc).isoformat()
    wallets = [{"address": c["address"], "label": f"BSC_SMART_{i+1:02d}",
                "score": round(c["score"], 2), "sources": c["sources"],
                "added_at": added_at, "notes": ""}
               for i, c in enumerate(ranked)]

    print(f"\n{'label':<14}{'score':>7}  sources          address")
    for w in wallets:
        print(f"{w['label']:<14}{w['score']:>7}  {','.join(w['sources']):<16} {w['address']}")

    if args.dry_run:
        print("\n--dry-run: not writing wallets.json")
        return
    OUT_PATH.write_text(json.dumps(wallets, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\nWrote {len(wallets)} wallets to {OUT_PATH}")


if __name__ == "__main__":
    main()
