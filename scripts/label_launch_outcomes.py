"""Attach outcomes to the launch collector's films.

A film without an outcome teaches nothing: the collector records what a token
looked like in its first hours, and this says what became of it. Only both
together can answer "what do the ones that ran — and survived — have in common".

    .venv/bin/python scripts/label_launch_outcomes.py
    .venv/bin/python scripts/label_launch_outcomes.py --min-age-hours 72

Append-only by design. Re-labelling a token until --refresh-until-hours turns
`alive` from a single point into a survival curve, which matters because the
measured base rate is brutal: of 16 confirmed 4x+ winners mined in July 2026,
14 were at $0 liquidity days later, and several died on day 5 rather than day 3.

Runs against GeckoTerminal at ~20 req/min. The launch collector permanently
consumes ~3 req/min of the same 30 req/min budget, so do not run gem_report.py
or film_report.py at the same time as this.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gem_report import _read_jsonl                         # noqa: E402
# The collector releases a film at this same floor, so the two must not drift.
from src.agent.copy_trade.launch_collector import DEAD_LIQ_USD     # noqa: E402
from src.agent.copy_trade.prices import get_pair_stats             # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS_PATH = ROOT / "data" / "launch_collector" / "snapshots.jsonl"
FILMS_PATH = ROOT / "data" / "launch_collector" / "films.jsonl"
OUTCOMES_PATH = ROOT / "data" / "launch_collector" / "outcomes.jsonl"

_GT_OHLCV = "https://api.geckoterminal.com/api/v2/networks/bsc/pools/{pool}/ohlcv/hour"
_SLEEP_S = 3.0        # deliberately slower than find_recent_winners' 2.1s: the
                      # collector now holds ~3 req/min of the shared budget
_OHLCV_RETRIES = 3    # unlike the collector — where a dropped sample costs
                      # nothing — a dropped OHLCV read here means a permanently
                      # peak-less label, so this one does retry.
# Measured against the live OHLCV endpoint 2026-07-30, 6 calls per spacing:
# 2s -> 4/6, 4s -> 3/6, 8s -> 5/6, 15s -> 6/6. The documented 30 req/min does
# not hold for this endpoint while the collector shares the IP. 60 films at
# this pace is a ~20 min batch, which is fine for something run every few days.
_GT_MIN_GAP_S = 15.0
# GeckoTerminal answers a 429 with `Retry-After: 0`. Obeying it literally means
# retrying instantly into the same 429 and burning every attempt in
# milliseconds — that is exactly what happened on the first live run. Treat the
# header as a floor to raise, never one to lower.
HOUR = 3600.0


def _mult(price: float | None, base: float | None) -> float | None:
    if not base or price is None:
        return None
    return price / base


def load_films(path: Path) -> dict[str, list[dict]]:
    """token -> its 30s samples, oldest first."""
    out: dict[str, list[dict]] = {}
    for row in _read_jsonl(path):
        if row.get("event") == "sample" and row.get("token_address"):
            out.setdefault(row["token_address"], []).append(row)
    for samples in out.values():
        samples.sort(key=lambda s: s.get("ts") or 0.0)
    return out


def price_series(film: list[dict], ohlcv: list[list]) -> list[tuple[float, float]]:
    """(ts, price) at the best resolution available for each part of the life.

    The film samples every 30s; hourly candles are ~120x coarser. Measured
    2026-07-30: tokens that lived an hour have 110-122 film points against 2
    hourly candles, and most tokens now die inside the hour. Deriving a peak or
    an exit rule from 2 candles is not possible, so the film wins wherever it
    reaches and the candles only extend past its end.
    """
    series = [(s["ts"], s["price"]) for s in film
              if s.get("ts") is not None and s.get("price")]
    film_end = max((ts for ts, _ in series), default=None)
    for row in ohlcv or []:
        if film_end is None or row[0] >= film_end:
            series.append((row[0], row[2]))       # candle HIGH, not close
    series.sort()
    return series


def label_outcome(launch: dict, pair_stats: dict | None, ohlcv: list[list],
                  now: float, film: list[dict] | None = None) -> dict:
    """One labelled row. Pure — every network read is done by the caller."""
    armed_at = launch.get("ts")
    base = launch.get("arm_price_usd")
    liq_now = (pair_stats or {}).get("liquidity_usd")
    price_now = (pair_stats or {}).get("price_usd")

    film = film or []
    rows = sorted(ohlcv or [], key=lambda r: r[0])
    series = price_series(film, rows)
    # Both the peak and its timestamp must come from the SAME post-arm window.
    # Reading the peak from points after arming while taking its time from all
    # of them reported a negative time_to_peak_h against a null max_multiple —
    # seen on live data 2026-07-30, because the hour candle a token is armed in
    # opens before the arm.
    after_arm = [p for p in series if armed_at is None or p[0] >= armed_at]
    peak_ts, peak = max(after_arm, key=lambda p: p[1]) if after_arm else (None, None)
    traded = [r for r in rows if (r[5] or 0) > 0]

    # Time of death at 30s resolution: the first film sample whose liquidity
    # fell through the floor. This is the exit rule's target variable — "how
    # long did I have to get out" — and hourly candles cannot answer it.
    dead = next((s for s in film
                 if s.get("liq") is not None and s["liq"] < DEAD_LIQ_USD), None)

    def _mult_by_hour(h: float) -> float | None:
        window = [p for p in after_arm if p[0] <= armed_at + h * HOUR]
        high = max((p[1] for p in window), default=None)
        return _mult(high, base)

    return {
        "token_address": launch.get("token_address"),
        "symbol": launch.get("symbol"),
        "labelled_at": now,
        "armed_at": armed_at,
        "arm_price_usd": base,
        "hours_since_arm": round((now - armed_at) / HOUR, 2) if armed_at else None,
        # `alive` is the crux label: most runners are worth nothing days later.
        "alive": bool(pair_stats) and (liq_now or 0) >= DEAD_LIQ_USD,
        "liquidity_usd_now": liq_now,
        "price_usd_now": price_now,
        "multiple_now": _mult(price_now, base),
        "max_price_usd": peak,
        "max_multiple": _mult(peak, base),
        "time_to_peak_h": (round((peak_ts - armed_at) / HOUR, 4)
                           if peak_ts and armed_at else None),
        # Did a 4h film even cover the move? That is what decides whether the
        # collector's window is long enough to learn an exit rule from.
        "mult_1h": _mult_by_hour(1), "mult_4h": _mult_by_hour(4),
        "mult_24h": _mult_by_hour(24),
        "died_at_h": (round((dead["ts"] - armed_at) / HOUR, 4)
                      if dead and armed_at else None),
        # A token whose very first sample is already under the floor never lived
        # — it was armed off a GeckoTerminal reserve DexScreener contradicted.
        # Without this flag those rows read as "died in 0 minutes" and would drag
        # the time-to-death distribution the exit rule is fitted against.
        "born_dead": bool(dead) and dead is film[0] if film else False,
        "last_active_h": (round((traded[-1][0] - armed_at) / HOUR, 2)
                          if traded and armed_at else None),
        "film_samples": len(film),
        "ohlcv_candles": len(rows),
        "source": ("film_30s+geckoterminal_ohlcv_hour" if film
                   else "geckoterminal_ohlcv_hour"),
    }


def pending_tokens(snapshots: list[dict], outcomes: list[dict], now: float,
                   min_age_h: float, refresh_until_h: float) -> list[dict]:
    """Launch rows old enough to label and not yet final.

    Keyed on the FIRST launch row per token: a restart re-arms in-flight films
    and writes a second one, but the original arm is when the film really began.
    """
    first: dict[str, dict] = {}
    for row in snapshots:
        if row.get("event") != "launch":
            continue
        token = row.get("token_address")
        if token and token not in first:
            first[token] = row

    last_label = {}
    for row in outcomes:
        token = row.get("token_address")
        if token:
            last_label[token] = max(last_label.get(token, 0.0),
                                    row.get("labelled_at") or 0.0)

    out = []
    for token, launch in first.items():
        armed_at = launch.get("ts")
        if not armed_at or (now - armed_at) < min_age_h * HOUR:
            continue
        if (now - armed_at) > refresh_until_h * HOUR and token in last_label:
            continue                       # final: stop re-labelling
        out.append(launch)
    return out


def fetch_ohlcv(pool_address: str, limit: int = 336) -> list[list] | None:
    """Hourly candles for a pool, or None if the read FAILED.

    None and [] mean different things and the caller must not conflate them:
    [] is a pool that genuinely never traded, None is "we don't know yet".
    outcomes.jsonl is append-only, so writing a row off a failed read bakes a
    wrong `max_multiple: null` into the dataset permanently.
    """
    for attempt in range(_OHLCV_RETRIES):
        try:
            r = requests.get(_GT_OHLCV.format(pool=pool_address),
                             params={"aggregate": 1, "limit": limit},
                             headers={"Accept": "application/json;version=20230302"},
                             timeout=25)
            if r.status_code == 429 and attempt < _OHLCV_RETRIES - 1:
                wait = r.headers.get("Retry-After") or 0
                try:
                    wait = float(wait)
                except (TypeError, ValueError):
                    wait = 0.0
                time.sleep(max(wait, _GT_MIN_GAP_S))
                continue
            r.raise_for_status()
            return (r.json().get("data", {}).get("attributes", {})
                    .get("ohlcv_list") or [])
        except Exception as e:  # noqa: BLE001 — report and let the caller skip
            if attempt >= _OHLCV_RETRIES - 1:
                print(f"  !! ohlcv failed for {pool_address}: {type(e).__name__}",
                      file=sys.stderr)
                return None
            time.sleep(_GT_MIN_GAP_S)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Label launch-collector outcomes")
    ap.add_argument("--min-age-hours", type=float, default=72.0,
                    help="wait this long after arming before labelling")
    ap.add_argument("--refresh-until-hours", type=float, default=336.0,
                    help="keep re-labelling until a film is this old (14d)")
    ap.add_argument("--snapshots", default=str(SNAPSHOTS_PATH))
    ap.add_argument("--films", default=str(FILMS_PATH))
    ap.add_argument("--out", default=str(OUTCOMES_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    snapshots = _read_jsonl(Path(args.snapshots))
    outcomes = _read_jsonl(Path(args.out))
    films = load_films(Path(args.films))
    now = time.time()
    todo = pending_tokens(snapshots, outcomes, now, args.min_age_hours,
                          args.refresh_until_hours)
    print(f"{len(todo)} film(s) to label "
          f"(of {sum(1 for s in snapshots if s.get('event') == 'launch')} launched)")

    labelled = []
    skipped = 0
    for i, launch in enumerate(todo, 1):
        token = launch["token_address"]
        stats = get_pair_stats(token)
        time.sleep(_SLEEP_S)
        ohlcv = fetch_ohlcv(launch.get("pool_address") or "")
        time.sleep(_GT_MIN_GAP_S)      # spacing between GeckoTerminal calls
        if ohlcv is None:
            # Leave it unlabelled rather than write a peak-less row into an
            # append-only file — the next run picks it up again.
            skipped += 1
            print(f"[{i}/{len(todo)}] {str(launch.get('symbol'))[:12]:12s} "
                  f"SKIP  (ohlcv unavailable, will retry next run)")
            continue
        row = label_outcome(launch, stats, ohlcv, now, films.get(token))
        labelled.append(row)
        mult = row["max_multiple"]
        died = row["died_at_h"]
        print(f"[{i}/{len(todo)}] {str(row['symbol'])[:12]:12s} "
              f"{'ALIVE' if row['alive'] else 'DEAD ':5s} "
              f"peak={mult if mult is None else round(mult, 2)}x "
              f"@{row['time_to_peak_h']}h  "
              f"died@{'-' if died is None else f'{died * 60:.0f}min'}  "
              f"film={row['film_samples']}pts")

    if args.dry_run:
        print("--dry-run: not writing")
        return
    if labelled:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "a", encoding="utf-8") as f:
            for row in labelled:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    alive = sum(1 for r in labelled if r["alive"])
    if labelled:
        print(f"\nwrote {len(labelled)} row(s): {alive} alive, "
              f"{len(labelled) - alive} dead "
              f"({alive / len(labelled) * 100:.0f}% survival this batch)")
    if skipped:
        print(f"{skipped} skipped on a failed OHLCV read — re-run to pick them up")


if __name__ == "__main__":
    main()
