"""Build data/copy_trade/wallet_candidates.json — staged BSC smart-money candidates
(Part 1 of docs/superpowers/specs/2026-07-16-cluster-signal-filter-design.md).

Manual, LOCAL run (gmgn-cli is only configured on the dev machine, never the VPS):

    python scripts/build_bsc_smart_wallets.py --winners-file data/copy_trade/recent_winners.json [--dry-run]
    python scripts/build_bsc_smart_wallets.py --winners 0xtokA 0xtokB ... --with-gmgn

Two candidate sources, merged and scored by wallet_discovery:
  1. gmgn-cli recent smart-money trades (maker frequency = BSC activity signal) —
     opt-in via --with-gmgn only; this source produced the 2026-07 scalper
     contamination and stays out of the default run.
  2. early buyers shared across >=2 of the winner tokens (our own edge), sourced
     either from --winners-file (scripts/find_recent_winners.py output) or
     --winners (hand-picked addresses)
Prints the scored table for user review; writes to --out (a staging file, NOT
wallets.json — deploy to the live list happens after manual review); --dry-run
skips writing entirely.
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
    DEFAULT_ENDPOINTS, DEFAULT_LOGS_ENDPOINTS, TRANSFER_TOPIC, RpcError, RpcPool,
)
from src.agent.copy_trade.wallet_discovery import (  # noqa: E402
    build_ranked_list, cross_winner_candidates, early_buyers, passes_filters,
    score_candidate, wallet_activity,
)

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0x0000000000000000000000000000000000000000"
EARLY_WINDOW_BLOCKS = 4 * 60 * 60 // 3   # ~4h of BSC blocks after pair creation


def load_winners_file(path: str) -> list[str]:
    """Token addresses from find_recent_winners.py output (Task 1 shape)."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return [r["token_address"] for r in rows if r.get("token_address")]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--winners", nargs="+",
                     help="winner token addresses given directly")
    src.add_argument("--winners-file",
                     help="JSON from scripts/find_recent_winners.py")
    ap.add_argument("--with-gmgn", action="store_true",
                    help="ALSO mine gmgn-cli smart-money makers (OFF by default: "
                         "this source produced the 2026-07 scalper contamination)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=26)
    ap.add_argument("--out", default=str(ROOT / "data" / "copy_trade"
                                         / "wallet_candidates.json"),
                    help="staging output (NOT wallets.json — audition first)")
    return ap.parse_args(argv)


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
    """Highest-liquidity BSC pair that actually has a pairCreatedAt timestamp —
    some DexScreener pairs (seen for real on 2026-07-16, e.g. an older LAB pool)
    are missing this field on the metadata even though other pairs for the same
    token have it, so the single highest-liquidity pair isn't always usable."""
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
                     timeout=20)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "bsc"]
    pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
    for p in pairs:
        if p.get("pairCreatedAt"):
            return p
    return None


AVG_BLOCK_TIME_S = 3  # BSC's stable post-Luban block time; only used to anchor an
                       # estimate for RECENT history (our winner tokens are all
                       # <2 years old) — never used to search all the way to genesis.


def block_at_timestamp(pool: RpcPool, ts: int) -> int:
    """First block at/after unix ts (public RPC has no direct timestamp->block API).

    Anchors on the current tip and BSC's average block time to estimate a starting
    point, then refines with a narrow local binary search — rather than bisecting
    the full [1, latest] range. A plain full-range binary search probes deep into
    chain history (back to 2020) on its first few iterations regardless of how
    recent the target timestamp is, and free public RPC endpoints don't reliably
    have archive data that far back (confirmed live 2026-07-16: both configured
    endpoints returned a bare null for an old block, crashing the naive version of
    this function). Anchoring avoids ever probing that deep for our real use case."""
    latest = pool.latest_block()
    latest_blk = pool.call("eth_getBlockByNumber", [hex(latest), False])
    if latest_blk is None:
        raise RpcError(f"no data for the latest block ({latest}) from any endpoint")
    latest_ts = int(latest_blk["timestamp"], 16)
    est_blocks_back = max(0, (latest_ts - ts) // AVG_BLOCK_TIME_S)
    window = max(2000, est_blocks_back // 10)  # covers block-time estimate drift
    lo = max(1, latest - est_blocks_back - window)
    hi = min(latest, latest - est_blocks_back + window)
    while lo < hi:
        mid = (lo + hi) // 2
        blk = pool.call("eth_getBlockByNumber", [hex(mid), False])
        if blk is None:
            raise RpcError(f"no data for block {mid} from any endpoint")
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
        # chunk=40: free public endpoints cap eth_getLogs ranges hard (1rpc.io/bnb
        # at 50 blocks, nodies.app at 250 — confirmed live 2026-07-16), well under
        # get_logs_chunked's 2000-block default used elsewhere for small polling
        # ranges. This call spans a full ~4h window in one go, so it must respect
        # the tightest known limit across the configured endpoints.
        logs = pool.get_logs_chunked(start, start + EARLY_WINDOW_BLOCKS,
                                     topics=[TRANSFER_TOPIC], address=token_address,
                                     chunk=40)
        exclude = {pair["pairAddress"].lower(), token_address.lower(), ZERO}
        buyers = early_buyers(logs, exclude=exclude)
        print(f"  {pair['baseToken']['symbol']}: {len(logs)} transfers, "
              f"{len(buyers)} early buyers")
        return buyers
    except Exception as e:
        print(f"  !! error scanning {token_address}: {e} — skipping")
        return []


def main() -> None:
    args = parse_args()
    winners = (load_winners_file(args.winners_file) if args.winners_file
               else args.winners)
    if not winners:
        print("winners list is empty — nothing to scan")
        raise SystemExit(1)

    env = dotenv_values(ROOT / ".env")
    bscscan_key = env.get("BSCSCAN_API_KEY", "")
    pool = RpcPool(DEFAULT_ENDPOINTS, logs_endpoints=DEFAULT_LOGS_ENDPOINTS)
    now = int(time.time())

    gmgn_counts: dict[str, int] = {}
    if args.with_gmgn:
        print("== source 1: gmgn-cli recent smart-money trades (EXPLICITLY enabled) ==")
        gmgn_counts = gmgn_maker_counts(fetch_gmgn_trades())
        print(f"  {len(gmgn_counts)} distinct makers")
    else:
        print("== gmgn source: SKIPPED (opt-in via --with-gmgn) ==")

    print("== early buyers across winner tokens ==")
    buyers_by_token = {t: scan_winner(pool, t) for t in winners}
    early_counts = cross_winner_candidates(buyers_by_token, min_tokens=2)
    print(f"  {len(early_counts)} wallets early in >=2 winners")

    candidates = assemble_candidates(gmgn_counts, early_counts)
    candidates.sort(key=lambda c: c["score"], reverse=True)

    print(f"== filtering top candidates (contract check + activity if available, need {args.top}) ==")
    kept: list[dict] = []
    for c in candidates:
        if len(kept) >= args.top:
            break
        code = pool.get_code(c["address"])
        if code != "0x":
            print(f"  drop {c['address'][:12]}… (contract)")
            continue
        # BscScan's free tier stopped serving BSC data (2026-07-16) — both the v1
        # and v2 endpoints reject it. When activity data is unavailable we skip
        # the bot/cold-wallet checks rather than reject every candidate; the
        # final list is manually reviewed by the user before wallets.json is
        # trusted, so an unfiltered-for-activity candidate is an acceptable risk.
        act = wallet_activity(bscscan_key, c["address"], now)
        time.sleep(0.25)   # BscScan free tier: 5 req/s (when it's working)
        if act is not None:
            ok, reason = passes_filters(act, code)
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
        print(f"\n--dry-run: not writing {args.out}")
        return
    out_path = Path(args.out)
    out_path.write_text(json.dumps(wallets, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\nWrote {len(wallets)} candidates to {out_path}")


if __name__ == "__main__":
    main()
