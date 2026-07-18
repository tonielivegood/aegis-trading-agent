"""Audition report: score observe-only candidate wallets from the bot's own
wallet_events.jsonl before they get real-money cluster votes. This is the wallet-
quality filter whose absence (BscScan died, filter skipped) caused the 2026-07
scalper contamination — self-built now, so it can never silently disappear again.

    .venv/bin/python scripts/wallet_audition.py            # all wallets in the log
    .venv/bin/python scripts/wallet_audition.py --days 3

Verdicts: PROMOTE (>= MIN_GEM_BUYS gem-band buys AND gem share >= MIN_GEM_PCT of
unique tokens AND median hold >= MIN_MEDIAN_HOLD_S), REJECT (enough data, bar
failed), INSUFFICIENT (no completed hold-time data yet — never promotable).
Gem-band stats are evaluated at REPORT time via DexScreener (ponytail: a token
bought 2 days ago may have grown since — small skew, acceptable for a 2-3 day
audition window; age barely drifts, mcap can)."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gem_report import classify, match_hold_times          # noqa: E402
from src.agent.copy_trade.prices import get_pair_stats             # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVENTS_PATH = ROOT / "data" / "copy_trade" / "wallet_events.jsonl"
CONFIG_PATH = ROOT / "data" / "copy_trade" / "config.json"

MIN_GEM_BUYS = 3
MIN_GEM_PCT = 0.25          # of unique tokens bought
MIN_MEDIAN_HOLD_S = 1800    # 30 min — scalpers need not apply


# ---------- pure logic (tested) ----------

def is_gem_band(stats: dict | None, max_age_days: float, max_mcap: float,
                min_liq: float) -> bool:
    """Same three rules as the live entry filter (trade_engine._passes_gem_filter);
    unknown age/mcap/no data = NOT gem band, mirroring the live filter's refusal."""
    if stats is None:
        return False
    created = stats.get("pair_created_at_ms")
    if created is None:
        return False
    if (time.time() - created / 1000) / 86400 > max_age_days:
        return False
    mcap = stats.get("market_cap_usd")
    if mcap is None or mcap > max_mcap:
        return False
    return (stats.get("liquidity_usd") or 0) >= min_liq


def audit_wallets(rows: list[dict], stats_by_token: dict, holds: dict,
                  cfg: dict) -> list[dict]:
    per: dict[str, set[str]] = {}
    for r in rows:
        if r["direction"] == "in":
            per.setdefault(r["wallet"], set()).add(r["token_address"])
    out = []
    for wallet, tokens in per.items():
        gem_tokens = {t for t in tokens
                      if is_gem_band(stats_by_token.get(t),
                                     cfg.get("max_token_age_days", 14),
                                     cfg.get("max_market_cap_usd", 5_000_000),
                                     cfg.get("min_liquidity_usd", 20_000))}
        hs = holds.get(wallet) or []
        median_hold = statistics.median(hs) if hs else None
        gem_pct = len(gem_tokens) / len(tokens) if tokens else 0.0
        if not hs:
            verdict = "INSUFFICIENT"
        elif len(gem_tokens) < MIN_GEM_BUYS:
            verdict = "REJECT"
        elif gem_pct >= MIN_GEM_PCT and median_hold >= MIN_MEDIAN_HOLD_S:
            verdict = "PROMOTE"
        else:
            verdict = "REJECT"
        out.append({"wallet": wallet, "unique_tokens": len(tokens),
                    "gem_tokens": len(gem_tokens), "gem_pct": round(gem_pct, 2),
                    "median_hold_s": median_hold,
                    "hold_class": classify(median_hold) if median_hold is not None else "?",
                    "verdict": verdict})
    return sorted(out, key=lambda r: (r["verdict"] != "PROMOTE", -r["gem_pct"]))


def convergence_windows(rows: list[dict], gem_tokens: set[str],
                        window_minutes: int) -> dict[str, int]:
    """Per gem token: max distinct wallets buying within any sliding window —
    the data behind a possible window_minutes 15->30 config decision at promotion."""
    win_s = window_minutes * 60
    buys: dict[str, list[tuple[float, str]]] = {}
    for r in rows:
        if r["direction"] == "in" and r["token_address"] in gem_tokens:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
            buys.setdefault(r["token_address"], []).append((ts, r["wallet"]))
    out = {}
    for token, events in buys.items():
        events.sort()
        best = 0
        for i, (t0, _) in enumerate(events):
            wallets = {w for t, w in events[i:] if t - t0 <= win_s}
            best = max(best, len(wallets))
        out[token] = best
    return out


# ---------- report ----------

def main() -> None:
    ap = argparse.ArgumentParser(description="Candidate-wallet audition report")
    ap.add_argument("--days", type=float, default=7.0)
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["copy_settings"]
    horizon = time.time() - args.days * 86400
    rows = []
    for line in (EVENTS_PATH.read_text(encoding="utf-8").splitlines()
                 if EVENTS_PATH.exists() else []):
        try:
            r = json.loads(line)
            if datetime.fromisoformat(r["ts"]).timestamp() >= horizon:
                rows.append(r)
        except (ValueError, KeyError):
            continue
    tokens = {r["token_address"] for r in rows if r["direction"] == "in"}
    print(f"{len(rows)} events, {len(tokens)} unique tokens bought — "
          f"fetching pair stats (~{len(tokens) * 0.3:.0f}s)…")
    stats_by_token = {}
    for t in tokens:
        stats_by_token[t] = get_pair_stats(t)
        time.sleep(0.25)

    holds = match_hold_times(rows)
    table = audit_wallets(rows, stats_by_token, holds, cfg)
    print(f"\n{'wallet':<44}{'toks':>5}{'gems':>5}{'gem%':>6}{'hold':>8}"
          f"{'class':>9}  verdict")
    for r in table:
        h = r["median_hold_s"]
        nice = ("?" if h is None else f"{h:.0f}s" if h < 120 else
                f"{h / 60:.0f}m" if h < 7200 else f"{h / 3600:.1f}h")
        print(f"{r['wallet']:<44}{r['unique_tokens']:>5}{r['gem_tokens']:>5}"
              f"{r['gem_pct']:>6}{nice:>8}{r['hold_class']:>9}  {r['verdict']}")
    n_promote = sum(1 for r in table if r["verdict"] == "PROMOTE")
    print(f"\nPROMOTE: {n_promote} | REJECT: "
          f"{sum(1 for r in table if r['verdict'] == 'REJECT')} | INSUFFICIENT: "
          f"{sum(1 for r in table if r['verdict'] == 'INSUFFICIENT')}")

    gem_tokens_all = {t for t, s in stats_by_token.items()
                      if is_gem_band(s, cfg.get("max_token_age_days", 14),
                                     cfg.get("max_market_cap_usd", 5_000_000),
                                     cfg.get("min_liquidity_usd", 20_000))}
    print("\nGem-token convergence (max distinct wallets in one window) — "
          "informs the window_minutes decision:")
    for wm in (15, 30, 60):
        conv = convergence_windows(rows, gem_tokens_all, wm)
        best = max(conv.values(), default=0)
        multi = sum(1 for v in conv.values() if v >= 2)
        print(f"  {wm:>3}m window: best {best} wallets on one token; "
              f"{multi} tokens saw >=2 wallets")


if __name__ == "__main__":
    main()
