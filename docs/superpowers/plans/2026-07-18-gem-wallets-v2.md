# Gem-Hunter Wallet List v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This plan was written by a stronger model for execution by a smaller model.** Every design decision is already made — do not re-litigate them. Where a step says "verbatim", copy the code exactly. Where an external API's response shape must be probed, the step says so explicitly and shows how.

**Goal:** Replace the copy-trade bot's 24-wallet list (proven by live data to contain zero gem hunters: 4,197 observed buys across 152 tokens, only 0.7% in the gem band, best wallet flipped its one gem buy in 15 minutes) with wallets that habitually buy young/small/liquid tokens and hold — validated with self-collected live data BEFORE they get real-money voting power.

**Architecture:** Four stages, data-layer only — zero changes to the v3 bot code (`src/agent/copy_trade/` stays untouched). (A) Mine recent *catchable* winner tokens from GeckoTerminal. (B) Extract early-buyer-convergence candidate wallets via the existing RPC scan in `scripts/build_bsc_smart_wallets.py` (gmgn-cli source dropped — it produced the scalper contamination). (C) Audition candidates in the live bot as `observe_only` wallets (mechanism already built in v3) for 48-72h — watched, never voting, zero money at risk. (D) Score the audition from `wallet_events.jsonl` with a new `scripts/wallet_audition.py` and promote only wallets that pass a behavioral bar the old list catastrophically fails.

**Tech Stack:** Python 3 (existing venv), pytest + unittest.mock (existing idiom), GeckoTerminal free API (already used), DexScreener via existing `get_pair_stats`, existing `RpcPool` for chain scans. No new dependencies.

## Global Constraints

- **Real money is live.** `copy-trade.service` on the VPS runs with `shadow_mode: false`. Stages A-C must not change ANY voting behavior: candidates enter `wallets.json` with `"observe_only": true` ONLY (the v3 `_load_wallets` in `src/agent/copy_trade/monitor.py` excludes them from cluster voting — already built and tested). Voting-set changes happen only in Stage D, after the human reviews the audition report.
- **The behavioral filter cannot be skipped.** The July incident happened precisely because the wallet-quality filter was skipped when BscScan died. Stage D IS that filter, rebuilt on self-collected data. If audition data is thin (fewer than 3 gem-band buys observed for a wallet), the wallet is NOT promoted — "insufficient data" ≠ "pass".
- **Free-tier APIs only**: GeckoTerminal (keyless, ~30 calls/min → sleep ≥2s between calls), DexScreener via the existing cached `get_pair_stats`. No new dependencies.
- **Watched-wallet ceiling: 50 total** (24 current + ≤26 candidates). `chain_events.py`'s receipt-cache design is documented "fine for 50 wallets"; beyond that, RPC load risk. If Stage B yields more than 26 candidates, take the top 26 by convergence score.
- **Worktree/branch discipline**: all commits go to the feature branch in the CURRENT worktree. First command of every implementer session: `git branch --show-current` and verify it is NOT `main`. (A prior task in this project committed to `main` in the wrong checkout and had to be recovered — do not repeat.) VPS deploy happens only from merged `main` via `git pull`, plus the `wallets.json` data-file edits described in Stages C/D (that file is VPS-only, not in git).
- Full suite must stay green: `python -m pytest tests/ -q` → baseline 745 passed, 2 skipped going into this plan.
- Two human checkpoints, do not skip: (1) after Task 2's winners run — the human reviews/edits the winners list before wallet extraction; (2) at Task 5 — the human reviews the audition table before any voting change.
- Config knob names used by the audition script must be read from `data/copy_trade/config.json` `copy_settings` (`max_token_age_days`, `max_market_cap_usd`, `min_liquidity_usd`) so the audit bar always matches the live filter — fall back to 14 / 5000000 / 20000 only if a key is absent.

## Design rationale (read once — decisions already made, do not reopen)

1. **Why "catchable" winners, not just winners:** a token that 10x'd in 3 minutes was won by sniper bots; copying its early buyers copies snipers — useless at our 60s poll lag. We mine winners whose run took ≥6h and measure the multiple from the close of the FIRST full hourly candle (the price a follower could actually get), not the launch open. Defaults: multiple ≥4x, time-to-peak ≥6h, pool age ≤21 days, still-alive liquidity ≥$20k.
2. **Why drop gmgn-cli:** 14 of the old 25 wallets came from the GMGN leaderboard; the confirmed scalper (`0x7817dbf3...`, median observed hold 0 seconds) was one of them. The early-buyer-convergence source (our own mined edge) stays.
3. **Why audition instead of trusting extraction:** early buyers of winners still include pool-hoppers and bots that buy hundreds of tokens (they're "early" everywhere by volume). Only observed behavior separates them: gem-band buy share ≥25% of unique tokens bought, ≥3 gem-band buys observed, median hold ≥30 min.
4. **Why drop all 24 old wallets at promotion:** live data (8h, 4,197 buys) shows 0/24 touch the gem band habitually; the single toucher flipped in 15 min. Keeping them adds RPC load and zero signal.
5. **Convergence knobs (`min_wallets`, `window_minutes`) are NOT changed in this plan.** The audition report prints convergence statistics (max distinct candidates on one gem token within 15/30/60-min windows) so the human can decide at Task 5 whether `window_minutes: 15 → 30` is warranted. That would be a config-only edit.

---

### Task 1: `scripts/find_recent_winners.py` — mine catchable recent winners

**Files:**
- Create: `scripts/find_recent_winners.py`
- Test: `tests/test_find_recent_winners.py` (new file)

**Interfaces:**
- Produces: `data/copy_trade/recent_winners.json` — a JSON list of objects `{"token_address", "symbol", "pool_address", "age_days", "follower_multiple", "time_to_peak_h", "liquidity_usd"}` — consumed by Task 2's `--winners-file`.
- Pure functions (tested): `follower_stats(ohlcv: list[list], min_candles: int = 8) -> dict | None` and `is_catchable(stats: dict, min_multiple: float, min_peak_hours: float) -> bool`.
- OHLCV row shape (GeckoTerminal): `[ts_seconds, open, high, low, close, volume]`. The code MUST sort rows by `ts` ascending before computing — GeckoTerminal returns newest-first.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_find_recent_winners.py` verbatim:

```python
"""Pure-function tests for winner mining. The whole point of follower_stats is to
measure the multiple available to a 60s-lag follower (close of first hourly candle),
NOT the sniper-only launch price — a wrong baseline here silently mines the wrong
wallets, so these tests pin the math."""
from scripts.find_recent_winners import follower_stats, is_catchable


def _candle(ts_h, o, h, l, c):
    return [ts_h * 3600, o, h, l, c, 0]


def test_follower_stats_uses_first_candle_close_not_open():
    # open 0.001 (sniper price), first-candle close 0.01, later peak 0.05
    ohlcv = [_candle(0, 0.001, 0.012, 0.001, 0.01),
             _candle(1, 0.01, 0.02, 0.009, 0.018),
             _candle(9, 0.018, 0.05, 0.017, 0.04)] + [
             _candle(i, 0.04, 0.041, 0.039, 0.04) for i in range(10, 18)]
    s = follower_stats(ohlcv)
    assert s["entry_price"] == 0.01                  # close of candle 1, NOT 0.001
    assert s["multiple"] == 5.0                      # 0.05 / 0.01
    assert s["time_to_peak_h"] == 9.0                # candle at ts 9h holds the peak


def test_follower_stats_sorts_newest_first_input():
    rows = [_candle(9, 1, 5.0, 1, 4), _candle(0, 1, 1.2, 1, 1.0)] + [
        _candle(i, 4, 4, 4, 4) for i in range(1, 9)]
    s = follower_stats(rows)                         # deliberately unsorted input
    assert s["entry_price"] == 1.0 and s["multiple"] == 5.0


def test_follower_stats_insufficient_candles_returns_none():
    assert follower_stats([_candle(0, 1, 1, 1, 1)] * 7) is None
    assert follower_stats([]) is None


def test_follower_stats_zero_entry_returns_none():
    rows = [_candle(i, 0, 0, 0, 0) for i in range(10)]
    assert follower_stats(rows) is None


def test_is_catchable_thresholds():
    good = {"entry_price": 1, "multiple": 4.2, "time_to_peak_h": 7.0}
    assert is_catchable(good, min_multiple=4, min_peak_hours=6) is True
    assert is_catchable({**good, "multiple": 3.9}, 4, 6) is False
    assert is_catchable({**good, "time_to_peak_h": 2.0}, 4, 6) is False   # sniper pump
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_find_recent_winners.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.find_recent_winners'`.

- [ ] **Step 3: Implement `scripts/find_recent_winners.py`** (verbatim)

```python
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


def pool_ohlcv_hour(pool_address: str, limit: int = 504) -> list[list]:
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

    winners = []
    for p in young:
        ohlcv = pool_ohlcv_hour(p["pool_address"])
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_find_recent_winners.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Probe the live API shape once (network smoke, expected to work but verify)**

Run: `python scripts/find_recent_winners.py --pages-top 1 --pages-new 1 --dry-run`
Expected: prints "pools listed: N" with N in the dozens, then a (possibly empty) winners table — NO traceback. If the pool count is 0, the GeckoTerminal response shape differs from the code's assumptions: `curl -s "https://api.geckoterminal.com/api/v2/networks/bsc/pools?page=1" | head -c 2000`, inspect the JSON, and fix ONLY the key-path in `candidate_pools` to match (the structure to look for: `data[].attributes.address`, `data[].attributes.pool_created_at`, `data[].attributes.reserve_in_usd`, `data[].relationships.base_token.data.id`). Re-run the probe until the count is non-zero. Note what you changed in your report.

- [ ] **Step 6: Commit**

```bash
git add scripts/find_recent_winners.py tests/test_find_recent_winners.py
git commit -m "feat(copy_trade): find_recent_winners — mine catchable BSC winners from GeckoTerminal"
```

---

### Task 2: `build_bsc_smart_wallets.py` — winners-file input, gmgn opt-in, staging output

**Files:**
- Modify: `scripts/build_bsc_smart_wallets.py`
- Test: `tests/test_build_bsc_smart_wallets.py` (new, small)

**Interfaces:**
- Consumes: `data/copy_trade/recent_winners.json` (Task 1's output shape).
- Produces: candidate list written to `--out` (staging default: `data/copy_trade/wallet_candidates.json`), same entry shape as before (`address`, `label`, `score`, `sources`, `added_at`, `notes`).
- CLI changes: `--winners` becomes optional; new `--winners-file PATH` (mutually exclusive with `--winners`, one required); new `--with-gmgn` flag (default OFF — gmgn-cli produced the scalper contamination and stays out unless explicitly asked); new `--out PATH` (default `data/copy_trade/wallet_candidates.json` — NOT `wallets.json`, deploy happens in Task 4 after review).

- [ ] **Step 1: Write the failing test**

Create `tests/test_build_bsc_smart_wallets.py` verbatim:

```python
"""CLI-surface tests for the rebuilt wallet extractor: winners can come from Task 1's
JSON file, gmgn is opt-in (contamination source), output goes to a staging file."""
import json

from scripts.build_bsc_smart_wallets import load_winners_file, parse_args


def test_load_winners_file_extracts_token_addresses(tmp_path):
    f = tmp_path / "recent_winners.json"
    f.write_text(json.dumps([
        {"token_address": "0x" + "a" * 40, "symbol": "AAA", "follower_multiple": 5.0},
        {"token_address": "0x" + "b" * 40, "symbol": "BBB", "follower_multiple": 4.2},
    ]))
    assert load_winners_file(str(f)) == ["0x" + "a" * 40, "0x" + "b" * 40]


def test_parse_args_winners_file_and_defaults(tmp_path):
    f = tmp_path / "w.json"
    f.write_text("[]")
    args = parse_args(["--winners-file", str(f)])
    assert args.winners_file == str(f)
    assert args.with_gmgn is False                    # gmgn is opt-in now
    assert args.out.endswith("wallet_candidates.json")


def test_parse_args_requires_some_winner_source():
    import pytest
    with pytest.raises(SystemExit):
        parse_args([])                                # neither --winners nor --winners-file
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_build_bsc_smart_wallets.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_winners_file'`.

- [ ] **Step 3: Implement the modifications**

In `scripts/build_bsc_smart_wallets.py`:

3a. Add after the constants block (near `EARLY_WINDOW_BLOCKS`):

```python
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
```

3b. Rewrite `main()`'s argument handling and the two source blocks (keep everything else — `scan_winner`, `block_at_timestamp`, the contract-check loop, `build_ranked_list`, the printed table — exactly as it is):

```python
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
    ...rest of main() unchanged EXCEPT the final write goes to args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(wallets, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        print(f"\nWrote {len(wallets)} candidates to {out_path}")
```

(`...rest of main() unchanged...` = keep the current candidate assembly, sorting, contract-check filtering, ranking, and table-printing lines from the existing file verbatim; only the argument block at the top and the final output path change. Update the module docstring's usage line to show `--winners-file` and note gmgn is opt-in.)

- [ ] **Step 4: Run tests to verify they pass, then full-suite check**

Run: `python -m pytest tests/test_build_bsc_smart_wallets.py tests/test_wallet_discovery.py -v` → all PASS.
Run: `python -m pytest tests/ -q` → no new failures.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_bsc_smart_wallets.py tests/test_build_bsc_smart_wallets.py
git commit -m "feat(copy_trade): wallet extractor v2 — winners-file input, gmgn opt-in, staging output"
```

---

### Task 3: `scripts/wallet_audition.py` — behavioral scoring + promotion verdicts

**Files:**
- Create: `scripts/wallet_audition.py`
- Test: `tests/test_wallet_audition.py` (new)

**Interfaces:**
- Consumes: `data/copy_trade/wallet_events.jsonl` (v3 bot output: `{"ts", "wallet", "token_address", "direction", "block", "tx_hash"}`), `data/copy_trade/config.json` (gem-band knobs), `get_pair_stats` from `src.agent.copy_trade.prices`, `match_hold_times`/`classify` from `scripts.gem_report` (reuse — do not duplicate).
- Produces: console report. Pure functions (tested): `is_gem_band(stats: dict | None, max_age_days: float, max_mcap: float, min_liq: float) -> bool`, `audit_wallets(rows: list[dict], stats_by_token: dict, holds: dict, cfg: dict) -> list[dict]`, `convergence_windows(rows: list[dict], gem_tokens: set[str], window_minutes: int) -> dict[str, int]`.
- Promotion bar (constants at top of file): `MIN_GEM_BUYS = 3`, `MIN_GEM_PCT = 0.25` (unique-token basis), `MIN_MEDIAN_HOLD_S = 1800`. Verdicts: `PROMOTE` (all three met), `REJECT` (enough data, bar failed), `INSUFFICIENT` (< MIN_GEM_BUYS gem-band buys observed — NOT promotable).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_wallet_audition.py` verbatim:

```python
"""The promotion bar is the filter whose absence caused the July scalper incident —
these tests pin its three rules and the insufficient-data refusal."""
from scripts.wallet_audition import (MIN_GEM_BUYS, audit_wallets,
                                     convergence_windows, is_gem_band)

W1, W2, W3 = ("0x" + c * 40 for c in "123")
GEM, OLD = "0x" + "a" * 40, "0x" + "b" * 40
CFG = {"max_token_age_days": 14, "max_market_cap_usd": 5_000_000,
       "min_liquidity_usd": 20_000}
GEM_STATS = {"pair_created_at_ms": None, "market_cap_usd": 400_000.0,
             "liquidity_usd": 50_000.0}


def _row(ts, wallet, token, direction="in"):
    return {"ts": ts, "wallet": wallet, "token_address": token,
            "direction": direction, "block": 1, "tx_hash": "0x" + "f" * 64}


def test_is_gem_band_rules():
    import time
    young_ms = (time.time() - 2 * 86400) * 1000
    good = {"pair_created_at_ms": young_ms, "market_cap_usd": 1e6,
            "liquidity_usd": 30_000.0}
    assert is_gem_band(good, 14, 5e6, 2e4) is True
    assert is_gem_band({**good, "market_cap_usd": 9e6}, 14, 5e6, 2e4) is False
    assert is_gem_band({**good, "liquidity_usd": 5_000.0}, 14, 5e6, 2e4) is False
    assert is_gem_band({**good, "pair_created_at_ms": young_ms - 30 * 86400_000},
                       14, 5e6, 2e4) is False
    assert is_gem_band({**good, "pair_created_at_ms": None}, 14, 5e6, 2e4) is False
    assert is_gem_band(None, 14, 5e6, 2e4) is False


def test_audit_promote_reject_insufficient():
    import time
    young_ms = (time.time() - 2 * 86400) * 1000
    gem_stats = {**GEM_STATS, "pair_created_at_ms": young_ms}
    stats = {GEM: gem_stats, OLD: {"pair_created_at_ms": young_ms - 300 * 86400_000,
                                   "market_cap_usd": 5e7, "liquidity_usd": 1e6}}
    # W1: 3 distinct gem buys + 1 old buy (75% gem), holds 1h -> PROMOTE
    gems = [GEM, "0x" + "c" * 40, "0x" + "d" * 40]
    for g in gems[1:]:
        stats[g] = gem_stats
    rows = [_row(f"2026-07-18T0{i}:00:00+00:00", W1, t)
            for i, t in enumerate(gems)] + [_row("2026-07-18T03:00:00+00:00", W1, OLD)]
    # W2: only old buys -> REJECT ; W3: 1 gem buy -> INSUFFICIENT
    rows += [_row("2026-07-18T04:00:00+00:00", W2, OLD)] * 4
    rows += [_row("2026-07-18T05:00:00+00:00", W3, GEM)]
    holds = {W1: [3600.0, 4000.0], W2: [30.0], W3: []}
    out = {r["wallet"]: r for r in audit_wallets(rows, stats, holds, CFG)}
    assert out[W1]["verdict"] == "PROMOTE"
    assert out[W2]["verdict"] == "REJECT"
    assert out[W3]["verdict"] == "INSUFFICIENT" and MIN_GEM_BUYS == 3


def test_audit_scalper_hold_time_rejects_even_with_gem_buys():
    import time
    young_ms = (time.time() - 2 * 86400) * 1000
    gems = ["0x" + c * 40 for c in "cde"]
    stats = {g: {**GEM_STATS, "pair_created_at_ms": young_ms} for g in gems}
    rows = [_row(f"2026-07-18T0{i}:00:00+00:00", W1, g) for i, g in enumerate(gems)]
    holds = {W1: [30.0, 45.0, 20.0]}                 # gem buyer but 30s median hold
    out = audit_wallets(rows, stats, holds, CFG)
    assert out[0]["verdict"] == "REJECT"


def test_convergence_windows_counts_distinct_wallets_in_window():
    rows = [_row("2026-07-18T00:00:00+00:00", W1, GEM),
            _row("2026-07-18T00:10:00+00:00", W2, GEM),
            _row("2026-07-18T00:40:00+00:00", W3, GEM),      # outside 15m of first two
            _row("2026-07-18T00:00:00+00:00", W1, OLD)]      # non-gem: ignored
    assert convergence_windows(rows, {GEM}, window_minutes=15)[GEM] == 2
    assert convergence_windows(rows, {GEM}, window_minutes=60)[GEM] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_wallet_audition.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `scripts/wallet_audition.py`** (verbatim)

```python
"""Audition report: score observe-only candidate wallets from the bot's own
wallet_events.jsonl before they get real-money cluster votes. This is the wallet-
quality filter whose absence (BscScan died, filter skipped) caused the 2026-07
scalper contamination — self-built now, so it can never silently disappear again.

    .venv/bin/python scripts/wallet_audition.py            # all wallets in the log
    .venv/bin/python scripts/wallet_audition.py --days 3

Verdicts: PROMOTE (>= MIN_GEM_BUYS gem-band buys AND gem share >= MIN_GEM_PCT of
unique tokens AND median hold >= MIN_MEDIAN_HOLD_S), REJECT (enough data, bar
failed), INSUFFICIENT (< MIN_GEM_BUYS gem buys observed — never promotable).
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
        if len(gem_tokens) < MIN_GEM_BUYS:
            verdict = "INSUFFICIENT"
        elif (gem_pct >= MIN_GEM_PCT and median_hold is not None
              and median_hold >= MIN_MEDIAN_HOLD_S):
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
```

- [ ] **Step 4: Run tests to verify they pass, then full suite**

Run: `python -m pytest tests/test_wallet_audition.py -v` → 4 PASS.
Run: `python -m pytest tests/ -q` → no new failures.

- [ ] **Step 5: Commit**

```bash
git add scripts/wallet_audition.py tests/test_wallet_audition.py
git commit -m "feat(copy_trade): wallet_audition — behavioral promotion bar from self-collected data"
```

---

### Task 4: Run the pipeline + deploy the audition cohort (live VPS, observe-only)

No new code. This task runs Tasks 1-2's scripts, gets the human's winners sign-off, and deploys candidates as observe-only. **Nothing in this task changes voting behavior.**

- [ ] **Step 1: Merge the branch to main and push** (follow the repo's finishing-a-development-branch flow; full suite green first: `python -m pytest tests/ -q`).

- [ ] **Step 2: Deploy code to the VPS**

```bash
ssh -i "$env:USERPROFILE\.ssh\hostinger_openclaw" -o IdentitiesOnly=yes root@187.127.188.62 "sudo -u agent git -C /home/agent/Track1-trade-onchain pull"
```

- [ ] **Step 3: Mine winners ON THE VPS** (RPC-adjacent, venv ready):

```bash
ssh ... root@187.127.188.62 "cd /home/agent/Track1-trade-onchain && sudo -u agent .venv/bin/python scripts/find_recent_winners.py"
```

Expected: a winners table + `recent_winners.json` written. If 0 winners: re-run with `--max-age-days 30 --min-multiple 3` per the script's own hint. If still 0, STOP and report to the human (BSC gem drought — human decides whether to widen further or wait).

- [ ] **Step 4: HUMAN CHECKPOINT — present the winners table** to the user for review. They may strike tokens they distrust or add ones they know. Apply edits directly to `recent_winners.json` on the VPS if requested. Do not proceed without their go-ahead.

- [ ] **Step 5: Extract candidate wallets ON THE VPS**

```bash
ssh ... root@187.127.188.62 "cd /home/agent/Track1-trade-onchain && sudo -u agent .venv/bin/python scripts/build_bsc_smart_wallets.py --winners-file data/copy_trade/recent_winners.json --top 26"
```

Expected: candidate table + `wallet_candidates.json` (≤26 entries, contract wallets dropped). The early-buyer RPC scan takes minutes per winner (chunked getLogs) — be patient; a token skipped with `!! error scanning` is fine (logged, continue).

- [ ] **Step 6: Merge candidates into `wallets.json` as observe-only** (VPS-only data file, not in git):

```bash
ssh ... root@187.127.188.62 "sudo -u agent python3 - <<'PYEOF'
import json
base = '/home/agent/Track1-trade-onchain/data/copy_trade/'
wallets = json.load(open(base + 'wallets.json'))
cands = json.load(open(base + 'wallet_candidates.json'))
have = {w['address'].lower() for w in wallets}
added = 0
for c in cands:
    if c['address'].lower() in have:
        continue
    if len(wallets) >= 50:
        break
    wallets.append({'address': c['address'], 'label': c.get('label', ''),
                    'observe_only': True,
                    'note': 'audition candidate 2026-07 (gem-wallets-v2)'})
    added += 1
json.dump(wallets, open(base + 'wallets.json', 'w'), indent=2)
print(f'added {added} candidates; total watched {len(wallets)}; '
      f'voting {sum(1 for w in wallets if not w.get(\"observe_only\"))}')
PYEOF"
```

Expected: `voting` count UNCHANGED (24); total watched ≤ 50.

- [ ] **Step 7: Restart + verify live checklist**

```bash
ssh ... root@187.127.188.62 "systemctl restart copy-trade && sleep 90 && tail -5 /home/agent/Track1-trade-onchain/logs/copy_trade.log && systemctl is-active copy-trade"
```

All must hold: (1) `copy_trade_monitor_v2_started` shows `wallets=<24+added>`, `voting=24`, `mode=LIVE`; (2) service `active`; (3) after ~10 more minutes, re-check the log tail for `event_poll_failed` streaks — none tolerated. **If poll failures appear (RPC overload from the bigger topic set): trim the cohort** — re-run Step 6's script variant keeping only the top ~15 candidates, restart, re-verify. (4) `wallet_events.jsonl` keeps growing.

- [ ] **Step 8: Record state** — append to `.superpowers/sdd/progress.md`: audition start timestamp, number of candidates deployed, and that Task 5 must not run before start + 48h.

---

### Task 5: Audition report → promotion (T+48-72h, HUMAN-GATED)

**Precondition: at least 48h of audition data since Task 4 Step 7's restart.** This is the second human checkpoint — no voting change happens without the user's explicit go-ahead on the table.

- [ ] **Step 1: Run the audition report on the VPS**

```bash
ssh ... root@187.127.188.62 "cd /home/agent/Track1-trade-onchain && sudo -u agent .venv/bin/python scripts/wallet_audition.py --days 3"
```

- [ ] **Step 2: HUMAN CHECKPOINT — present the full table + convergence stats.** Decisions the human makes here (present each with the relevant numbers):
  1. Promote the `PROMOTE` wallets (expected: if fewer than 5 pass, recommend re-running Task 4 Steps 3-6 with `--max-age-days 30` for a second candidate batch rather than trading with a too-thin voting pool).
  2. Drop all 24 old wallets (plan's default — they are proven non-gem-hunters; only override if the audition table itself shows an old wallet passing the bar).
  3. `window_minutes` 15 → 30 IF the convergence stats show gem-token convergence happens at 30m but not 15m (config-only edit).
  4. Which `INSUFFICIENT` wallets keep observing another 48-72h cycle (default: keep ALL of them as observe_only, watched-cap permitting — gems don't launch hourly, so a patient genuine gem hunter can easily show <3 gem buys in one short window; dropping INSUFFICIENT wallets would systematically bias the list toward hyperactive wallets, the exact failure mode this rebuild exists to fix). Only `REJECT` (enough data, failed the bar) is dropped by default.

- [ ] **Step 3: Apply the approved promotion on the VPS** (adjust the keep-list to EXACTLY what the human approved):

```bash
ssh ... root@187.127.188.62 "sudo -u agent python3 - <<'PYEOF'
import json
base = '/home/agent/Track1-trade-onchain/data/copy_trade/'
wallets = json.load(open(base + 'wallets.json'))
promoted = {  # EXACT addresses the human approved, lowercase
    # '0x...',
}
keep_observing = {  # INSUFFICIENT wallets the human kept for another cycle, lowercase
    # '0x...',      # (default: ALL wallets the audition table marked INSUFFICIENT)
}
new = []
for w in wallets:
    a = w['address'].lower()
    if a in promoted:
        w['observe_only'] = False
        w['note'] = 'promoted 2026-07 gem-wallets-v2 audition'
        new.append(w)
    elif a in keep_observing:
        w['observe_only'] = True   # stays under observation, still no vote
        w['note'] = 'audition extended (INSUFFICIENT data, not rejected)'
        new.append(w)
    # else: dropped — REJECT candidates and old voting wallets (plan default)
json.dump(new, open(base + 'wallets.json', 'w'), indent=2)
print(f'final list: {len(new)} wallets, voting '
      f'{sum(1 for w in new if not w.get(\"observe_only\"))}, observing '
      f'{sum(1 for w in new if w.get(\"observe_only\"))}')
PYEOF"
```

Back up first: `cp wallets.json wallets.json.bak.pre-promotion` (as user `agent`). If the human approved a `window_minutes` change, edit `data/copy_trade/config.json` in the repo (commit + push + VPS pull — config IS in git).

- [ ] **Step 4: Restart + verify** (same checklist as Task 4 Step 7 — startup line must now show the new `wallets=`/`voting=` counts).

- [ ] **Step 5: Update the memory handoff** — the session executing this updates `copy-trade-gem-hunt-v3-deployed.md` (or writes a successor memory): audition results table summary, final wallet count, what to watch next (first `opened` signal under the new list; `gem_report.py --days 7` weekly).

---

## Self-Review (done at write time)

1. **Coverage**: winner mining (T1), extraction with contamination source removed (T2), behavioral filter (T3), safe live deployment with human gates (T4-5). The failure that motivated this plan — skipped wallet-quality filtering — is now a tested, self-contained script that needs no external API that can die.
2. **Placeholder scan**: `...rest of main() unchanged...` in Task 2 refers to keeping existing lines verbatim (file read in full by the implementer first) — intentional modify-in-place marker. Task 5 Step 3's `promoted = {}` set is deliberately empty: it MUST be filled with the human-approved addresses at execution time, never pre-filled.
3. **Type consistency**: `recent_winners.json` shape (T1 output) matches `load_winners_file` (T2); `wallet_events.jsonl` row shape matches `audit_wallets`/`convergence_windows` inputs and `gem_report.match_hold_times` (existing, reused); `get_pair_stats` keys (`pair_created_at_ms`, `market_cap_usd`, `liquidity_usd`) match `is_gem_band`'s reads; `observe_only` semantics match `monitor._load_wallets` (built and tested in v3).
