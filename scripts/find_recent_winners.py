"""Mine recent CATCHABLE BSC winners from GeckoTerminal (free, keyless).

    .venv/bin/python scripts/find_recent_winners.py                # defaults
    .venv/bin/python scripts/find_recent_winners.py --max-age-days 30 --min-multiple 3

A catchable winner: pool <= max-age-days old, did >= min-multiple measured from the
CLOSE of its first hourly candle (the price a 60s-lag follower could get, not the
sniper launch price), took >= min-peak-hours to reach the peak (3-minute sniper
pumps are excluded — their early buyers are bots we cannot usefully copy), and is
still alive (current liquidity >= min-liq).

Output: data/copy_trade/recent_winners.json + a review table on stdout.
THE HUMAN REVIEWS/EDITS THE LIST before it feeds scripts/build_bsc_smart_wallets.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "copy_trade" / "recent_winners.json"
_GT = "https://api.geckoterminal.com/api/v2"
_SLEEP_S = 2.1          # GeckoTerminal free tier ~30 calls/min


# ---------- pure logic (tested) ----------

def follower_stats(ohlcv: list[list], min_candles: int = 8) -> dict | None:
    """Multiple available to a follower: close of FIRST hourly candle -> max high
    after it. Rows are [ts, o, h, l, c, v]; input order is not trusted (GT returns
    newest-first). None when too few candles to judge a completed run."""
    rows = sorted(ohlcv, key=lambda r: r[0])
    if len(rows) < min_candles:
        return None
    entry = rows[0][4]
    if not entry:
        return None
    later = rows[1:]
    peak_row = max(later, key=lambda r: r[2], default=None)
    if peak_row is None:
        return None
    return {"entry_price": entry,
            "multiple": peak_row[2] / entry,
            "time_to_peak_h": (peak_row[0] - rows[0][0]) / 3600}


def is_catchable(stats: dict, min_multiple: float, min_peak_hours: float) -> bool:
    return stats["multiple"] >= min_multiple and stats["time_to_peak_h"] >= min_peak_hours


# ---------- thin network layer (probe shapes before trusting — see Step 4) ----------

def _get(url: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(url, params=params or {}, timeout=20,
                         headers={"accept": "application/json"})
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 — report script, never crash on one call
        print(f"  !! GET {url} failed: {type(e).__name__}", file=sys.stderr)
        return None


def candidate_pools(max_pages_top: int, max_pages_new: int) -> list[dict]:
    """Pool dicts from top-volume + trending + new pools listings, deduped by address.
    Each: {"pool_address", "token_address", "name", "created_at_iso", "reserve_usd"}."""
    seen: dict[str, dict] = {}
    sources = ([(f"{_GT}/networks/bsc/pools", {"sort": "h24_volume_usd_desc", "page": p})
                for p in range(1, max_pages_top + 1)]
               + [(f"{_GT}/networks/bsc/trending_pools", {"page": 1})]
               + [(f"{_GT}/networks/bsc/new_pools", {"page": p})
                  for p in range(1, max_pages_new + 1)])
    for url, params in sources:
        body = _get(url, params)
        time.sleep(_SLEEP_S)
        for item in (body or {}).get("data") or []:
            attrs = item.get("attributes") or {}
            rel = (((item.get("relationships") or {}).get("base_token") or {})
                   .get("data") or {})
            token_id = rel.get("id") or ""          # "bsc_0xTOKEN"
            addr = (attrs.get("address") or "").lower()
            if not addr or "_" not in token_id:
                continue
            seen.setdefault(addr, {
                "pool_address": addr,
                "token_address": token_id.split("_", 1)[1].lower(),
                "name": attrs.get("name") or "?",
                "created_at_iso": attrs.get("pool_created_at"),
                "reserve_usd": float(attrs.get("reserve_in_usd") or 0)})
    return list(seen.values())


def pool_ohlcv_hour(pool_address: str, limit: int) -> list[list]:
    body = _get(f"{_GT}/networks/bsc/pools/{pool_address}/ohlcv/hour",
                {"aggregate": 1, "limit": limit})
    time.sleep(_SLEEP_S)
    try:
        return body["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return []


def _age_days(created_at_iso: str | None) -> float | None:
    if not created_at_iso:
        return None
    from datetime import datetime
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        return (time.time() - created.timestamp()) / 86400
    except ValueError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Mine catchable recent BSC winners")
    ap.add_argument("--max-age-days", type=float, default=21)
    ap.add_argument("--min-multiple", type=float, default=4.0)
    ap.add_argument("--min-peak-hours", type=float, default=6.0)
    ap.add_argument("--min-liq", type=float, default=20_000)
    ap.add_argument("--pages-top", type=int, default=5)
    ap.add_argument("--pages-new", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true", help="print only, don't write")
    args = ap.parse_args()

    pools = candidate_pools(args.pages_top, args.pages_new)
    young = [p for p in pools
             if (a := _age_days(p["created_at_iso"])) is not None
             and a <= args.max_age_days and p["reserve_usd"] >= args.min_liq]
    print(f"pools listed: {len(pools)}, young+alive candidates: {len(young)}")

    # 24 hourly candles/day + buffer, so the oldest candle reaches a pool's
    # true first candle even for pools near --max-age-days old; GeckoTerminal
    # caps `limit` at 1000 (~41 days) so clamp there.
    ohlcv_limit = min(1000, int(args.max_age_days * 24) + 6)

    winners = []
    for p in young:
        ohlcv = pool_ohlcv_hour(p["pool_address"], ohlcv_limit)
        s = follower_stats(ohlcv)
        if s is None:
            continue
        if is_catchable(s, args.min_multiple, args.min_peak_hours):
            winners.append({
                "token_address": p["token_address"], "symbol": p["name"].split(" /")[0],
                "pool_address": p["pool_address"],
                "age_days": round(_age_days(p["created_at_iso"]), 1),
                "follower_multiple": round(s["multiple"], 2),
                "time_to_peak_h": round(s["time_to_peak_h"], 1),
                "liquidity_usd": int(p["reserve_usd"])})

    winners.sort(key=lambda w: w["follower_multiple"], reverse=True)
    print(f"\n{'symbol':<12}{'age_d':>6}{'mult':>7}{'peak_h':>8}{'liq':>10}  token")
    for w in winners:
        print(f"{w['symbol']:<12}{w['age_days']:>6}{w['follower_multiple']:>7}"
              f"{w['time_to_peak_h']:>8}{w['liquidity_usd']:>10}  {w['token_address']}")
    print(f"\n{len(winners)} catchable winners "
          f"(>= {args.min_multiple}x, peak >= {args.min_peak_hours}h, "
          f"age <= {args.max_age_days}d, liq >= ${args.min_liq:,.0f})")
    if not winners:
        print("None found — retry with --max-age-days 30 and/or --min-multiple 3.")
    if args.dry_run:
        print("--dry-run: not writing")
        return
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(winners, indent=2), encoding="utf-8")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
